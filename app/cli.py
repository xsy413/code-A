from __future__ import annotations

import shutil
import sys
from pathlib import Path

import typer

from app.agent import CodingAgent
from app.config import Settings

app = typer.Typer(help="LangGraph coding-agent CLI")


def _make_confirm_fn(cwd: Path):
    """返回一个在终端向用户展示操作详情并请求确认的回调函数。"""

    def _preview_content(text: str, max_lines: int = 12) -> str:
        lines = text.splitlines()
        snippet = "\n".join(lines[:max_lines])
        if len(lines) > max_lines:
            snippet += f"\n  … ({len(lines) - max_lines} more lines)"
        return snippet

    def confirm_fn(tool_name: str, args: dict) -> bool:
        path = args.get("path", "?")
        workspace = Path(cwd).resolve()
        abs_path = (workspace / path).resolve()

        typer.echo("")
        typer.echo("─" * 60)
        typer.echo(f"  ⚠️  Agent wants to perform a risky write:")
        typer.echo(f"     tool : {tool_name}")
        typer.echo(f"     path : {path}")

        # 显示 diff-style 内容预览
        if tool_name == "write_file":
            content = str(args.get("content", ""))
            if abs_path.exists():
                typer.echo(f"     mode : OVERWRITE existing file")
                try:
                    old = abs_path.read_text(encoding="utf-8", errors="replace")
                    typer.echo(f"  ── current content (first 8 lines) ──")
                    typer.echo(_preview_content(old, 8))
                except Exception:
                    pass
                typer.echo(f"  ── new content (first 12 lines) ──")
            else:
                typer.echo(f"     mode : CREATE new file")
                typer.echo(f"  ── content (first 12 lines) ──")
            typer.echo(_preview_content(content, 12))

        elif tool_name == "patch_file":
            old_str = str(args.get("old_str", ""))
            new_str = str(args.get("new_str", ""))
            typer.echo(f"  ── replacing ──")
            for line in old_str.splitlines()[:6]:
                typer.echo(f"  - {line}")
            typer.echo(f"  ── with ──")
            for line in new_str.splitlines()[:6]:
                typer.echo(f"  + {line}")

        elif tool_name == "delete_file":
            typer.echo(f"     mode : DELETE file permanently")

        typer.echo("─" * 60)

        try:
            return typer.confirm("Allow this operation?", default=False)
        except (KeyboardInterrupt, EOFError):
            typer.echo("\nAborted.")
            return False

    return confirm_fn


def _make_stream_fn():
    """返回逐 chunk 写入 stdout 的流式回调，flush=True 确保实时显示。"""
    import sys

    def stream_fn(chunk: str) -> None:
        sys.stdout.write(chunk)
        sys.stdout.flush()

    return stream_fn


def _build_agent(cwd: Path, yes: bool) -> CodingAgent:
    settings = Settings.from_env(workspace=cwd.resolve(), auto_confirm_risky_writes=yes)
    confirm_fn = None if yes else _make_confirm_fn(cwd)
    stream_fn = _make_stream_fn()
    return CodingAgent(settings, confirm_fn=confirm_fn, stream_fn=stream_fn)


def _print_result(result: dict) -> None:
    typer.echo(f"session_id: {result.get('session_id')}")
    typer.echo(f"status: {result.get('status')}")
    if result.get("error"):
        typer.echo(f"error: {result.get('error')}")
    if result.get("verification_note"):
        typer.echo(f"verification_note: {result.get('verification_note')}")
    typer.echo("summary:")
    typer.echo(result.get("summary", ""))


@app.command()
def run(
    task: str = typer.Argument(..., help="Task for the coding agent."),
    cwd: Path = typer.Option(Path("."), "--cwd", help="Workspace path."),
    session_id: str | None = typer.Option(None, "--session-id", help="Optional session id."),
    yes: bool = typer.Option(False, "--yes", help="Allow risky writes without manual stop."),
) -> None:
    """Run a new session (or add first turn to an explicit session)."""
    agent = _build_agent(cwd, yes)
    result = agent.run(task=task, cwd=cwd, session_id=session_id)
    _print_result(result)


@app.command()
def resume(
    session_id: str = typer.Argument(..., help="Session id to resume."),
    cwd: Path = typer.Option(Path("."), "--cwd", help="Workspace path."),
    yes: bool = typer.Option(False, "--yes", help="Allow risky writes without manual stop."),
) -> None:
    """Resume an existing session from SQLite state."""
    agent = _build_agent(cwd, yes)
    result = agent.resume(session_id=session_id)
    _print_result(result)


@app.command()
def chat(
    cwd: Path = typer.Option(Path("."), "--cwd", help="Workspace path."),
    session_id: str | None = typer.Option(None, "--session-id", help="Existing session id."),
    new: bool = typer.Option(False, "--new", help="Start a fresh session."),
    yes: bool = typer.Option(False, "--yes", help="Allow risky writes without manual stop."),
) -> None:
    """Interactive multi-turn chat in the same session context."""
    agent = _build_agent(cwd, yes)

    if new:
        sid = agent.start_session(cwd)
    elif session_id:
        sid = session_id
    else:
        sid = agent.last_session_id() or agent.start_session(cwd)

    typer.echo(f"session_id: {sid}")
    typer.echo("Commands: /exit, /new, /session, /session <id>")

    while True:
        try:
            user_input = typer.prompt("you").strip()
        except (EOFError, KeyboardInterrupt):
            typer.echo("\nchat ended")
            break

        if not user_input:
            continue

        if user_input == "/exit":
            typer.echo("chat ended")
            break

        if user_input == "/new":
            sid = agent.start_session(cwd)
            typer.echo(f"session_id: {sid}")
            continue

        if user_input.startswith("/session"):
            parts = user_input.split(maxsplit=1)
            if len(parts) == 1:
                typer.echo(f"session_id: {sid}")
            else:
                sid = parts[1].strip()
                typer.echo(f"switched session_id: {sid}")
            continue

        result = agent.run_turn(session_id=sid, user_request=user_input, cwd=cwd)
        _print_result(result)


@app.command()
def logs(
    session_id: str | None = typer.Argument(None, help="Session id (default latest)."),
    cwd: Path = typer.Option(Path("."), "--cwd", help="Workspace path."),
    verbose: bool = typer.Option(False, "--verbose", help="Show full summaries."),
) -> None:
    """Show node execution trail for a session."""
    agent = _build_agent(cwd, yes=False)

    sid = session_id or agent.last_session_id()
    if not sid:
        raise typer.BadParameter("No session id found.")

    events = agent.get_logs(sid)
    state = agent.store.load_state(sid) or {}

    typer.echo(f"session_id: {sid}")
    if verbose:
        typer.echo(
            "budgets: "
            f"retry_attempts={state.get('retry_attempts', 0)} "
            f"tool_call_count={state.get('tool_call_count', 0)} "
            f"write_count={state.get('write_count', 0)} "
            f"explore_streak={state.get('explore_streak', 0)}"
        )
        typer.echo(
            "progress: "
            f"turn_progress={state.get('turn_progress', 'none')} "
            f"needs_more_action={state.get('needs_more_action', False)}"
        )
        if state.get("verification_mode"):
            typer.echo(f"verification_mode: {state.get('verification_mode')}")
        if state.get("verification_note"):
            typer.echo(f"verification_note: {state.get('verification_note')}")

        # ── Token 用量统计 ──────────────────────────────────────────────────
        try:
            token_summary = agent.store.get_token_summary(sid)
            if token_summary["llm_calls"] > 0:
                typer.echo(
                    f"token_usage (this turn): "
                    f"prompt={token_summary['prompt_tokens']:,}  "
                    f"completion={token_summary['completion_tokens']:,}  "
                    f"total={token_summary['total_tokens']:,}  "
                    f"llm_calls={token_summary['llm_calls']}"
                )
                if token_summary["by_node"]:
                    typer.echo("  by node:")
                    for entry in token_summary["by_node"]:
                        typer.echo(
                            f"    {entry['node_name']:10s}  total={entry['total_tokens']:>6,}  "
                            f"calls={entry['calls']}  model={entry['model']}"
                        )
        except Exception:
            pass  # 旧库无 token_usage 表时静默跳过

        turns = state.get("turns", [])
        if turns:
            typer.echo("turns:")
            for idx, turn in enumerate(turns, start=1):
                req = str(turn.get("user_request", "")).replace("\n", " ")
                if len(req) > 90:
                    req = req[:87] + "..."
                typer.echo(
                    f"  [{idx}] id={turn.get('turn_id')} "
                    f"status={turn.get('status')} tool_calls={turn.get('tool_calls', 0)}"
                )
                typer.echo(f"      request: {req}")
                if turn.get("error"):
                    typer.echo(f"      error: {turn.get('error')}")


    for event in events:
        typer.echo(f"[{event.ts}] {event.node_name} ({event.duration_ms}ms)")
        if verbose:
            typer.echo(f"  in : {event.input_summary}")
            typer.echo(f"  out: {event.output_summary}")
            if event.error:
                typer.echo(f"  err: {event.error}")
        elif event.error:
            typer.echo(f"  err: {event.error}")


@app.command()
def backups(
    session_id: str | None = typer.Argument(None, help="Session id (default latest)."),
    cwd: Path = typer.Option(Path("."), "--cwd", help="Workspace path."),
) -> None:
    """List all backed-up files for a session."""
    agent = _build_agent(cwd, yes=False)
    sid = session_id or agent.last_session_id()
    if not sid:
        raise typer.BadParameter("No session id found.")

    workspace = Path(cwd).resolve()
    backup_root = workspace / ".agent" / "backups" / sid

    if not backup_root.exists():
        typer.echo(f"No backups found for session {sid[:8]}… (dir: {backup_root})")
        raise typer.Exit()

    bak_files = sorted(backup_root.rglob("*.bak"))
    if not bak_files:
        typer.echo(f"No backup files found under {backup_root}")
        raise typer.Exit()

    typer.echo(f"Backups for session {sid[:8]}… ({len(bak_files)} file(s))")
    typer.echo(f"  backup root: {backup_root}")
    for bak in bak_files:
        rel = bak.relative_to(backup_root)
        # 还原路径：去掉 .bak 后缀
        original = str(rel.with_suffix("")) if rel.suffix == ".bak" else str(rel)
        size = bak.stat().st_size
        typer.echo(f"  {original:50s}  ({size:,} bytes)")


@app.command()
def restore(
    file_path: str | None = typer.Argument(None, help="Relative file to restore (default: all)."),
    session_id: str | None = typer.Option(None, "--session", "-s", help="Session id (default latest)."),
    cwd: Path = typer.Option(Path("."), "--cwd", help="Workspace path."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview without writing."),
) -> None:
    """Restore files from a session backup.

    Examples::

        python -m app.cli restore                     # restore all files (latest session)
        python -m app.cli restore src/foo.py          # restore one file
        python -m app.cli restore --session <id>      # specific session
        python -m app.cli restore --dry-run           # preview only
    """
    agent = _build_agent(cwd, yes=False)
    sid = session_id or agent.last_session_id()
    if not sid:
        raise typer.BadParameter("No session id found.")

    workspace = Path(cwd).resolve()
    backup_root = workspace / ".agent" / "backups" / sid

    if not backup_root.exists():
        typer.echo(f"[error] No backups found for session {sid[:8]}…")
        raise typer.Exit(1)

    # 收集要还原的 .bak 文件
    if file_path:
        p = Path(file_path)
        expected_bak = backup_root / p.with_suffix(p.suffix + ".bak")
        candidates = [expected_bak] if expected_bak.exists() else []
        if not candidates:
            typer.echo(f"[error] No backup found for: {file_path}")
            raise typer.Exit(1)
    else:
        candidates = sorted(backup_root.rglob("*.bak"))

    if not candidates:
        typer.echo("No backup files to restore.")
        raise typer.Exit()

    prefix = "[dry-run] " if dry_run else ""
    restored = 0
    for bak in candidates:
        rel_bak = bak.relative_to(backup_root)
        original_rel = rel_bak.with_suffix("") if rel_bak.suffix == ".bak" else rel_bak
        dest = workspace / original_rel
        typer.echo(f"{prefix}restore {original_rel}  ← {bak.name}")
        if not dry_run:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(bak), str(dest))
            restored += 1

    if dry_run:
        typer.echo(f"\n[dry-run] Would restore {len(candidates)} file(s). Run without --dry-run to apply.")
    else:
        typer.echo(f"\nRestored {restored} file(s) from session {sid[:8]}…")


if __name__ == "__main__":
    app()
