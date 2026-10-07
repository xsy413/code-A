from __future__ import annotations

import shutil
import sys
from pathlib import Path

import typer

from app.agent import CodingAgent
from app.config import Settings

app = typer.Typer(help="LangGraph coding-agent CLI")


@app.command()
def permissions(
    cwd: Path = typer.Option(Path("."), "--cwd", help="Workspace path."),
    command: str | None = typer.Option(None, "--command", help="Analyze a command without executing it."),
    shell: str = typer.Option("powershell", "--shell", help="bash or powershell."),
) -> None:
    """Inspect effective permissions or analyze one shell command without execution."""
    from app.permissions import PermissionEngine
    engine = PermissionEngine(cwd)
    if engine.config_error:
        raise typer.BadParameter(engine.config_error)
    typer.echo(f"User configuration: {engine.config_path}")
    typer.echo("No OS filesystem/network isolation. Unmatched commands require approval.")
    if command is not None:
        if shell not in {"bash", "powershell"}:
            raise typer.BadParameter("--shell must be bash or powershell")
        result = engine.evaluate(shell, {"command": command}, cwd)
        typer.echo(f"{result.decision}: {result.rule_id}: {result.reason}")
        return
    for rule in engine.rules.values():
        typer.echo(f"{rule.id:8s} {rule.decision:5s} {rule.reason}")


def _make_confirm_fn(cwd: Path):
    from app.permissions import PermissionEngine
    sanitizer = PermissionEngine(cwd).redact

    def confirm_fn(tool_name: str, args: dict) -> str:
        if not sys.stdin.isatty():
            return "unavailable"
        permission = args.get("_permission", {})
        display = sanitizer(args)
        typer.echo("\nPermission approval")
        typer.echo(f"  tool: {tool_name}")
        typer.echo(f"  cwd: {display.get('cwd', display.get('_workspace', str(cwd.resolve())))}")
        typer.echo(f"  rule: {permission.get('rule_id')}: {sanitizer(permission.get('reason', ''))}")
        if "command" in display:
            typer.echo(f"  command: {display['command']}")
            typer.echo("  Runs as your current user, including child processes; no OS filesystem/network isolation.")
        else:
            typer.echo(f"  file: {display.get('path', '?')}")
            if permission.get("rule_id") != "Q-O13":
                for key in ("content", "old_str", "new_str"):
                    if key in display:
                        typer.echo(f"  {key}: {display[key][:1200]}")
        for effect in display.get("_permission", {}).get("effects", []):
            typer.echo(f"  target: {effect}")
        for program in display.get("_permission", {}).get("executables", []):
            typer.echo(f"  program: {program}")
        reusable = permission.get("reusable", False)
        if reusable:
            if permission.get("rule_id") == "Q-S09":
                typer.echo("  Session scope: ordinary file creation/edits throughout this workspace; excludes deletion, sensitive configuration and shell writes.")
            else:
                typer.echo("  Session scope: this exact shell, entry and arguments, including later source/test edits. Entry/config/dependency changes require approval again.")
            typer.echo("  Session approval expires when this CLI process exits.")
        choices = "once / session / reject" if reusable else "once / reject"
        try:
            answer = typer.prompt(f"Allow? ({choices})", default="reject").strip().lower()
        except (KeyboardInterrupt, EOFError):
            return "reject"
        return {"once": "approve_once", "session": "approve_session" if reusable else "reject"}.get(answer, "reject")

    return confirm_fn


def _make_stream_fn():
    """返回逐 chunk 写入 stdout 的流式回调，flush=True 确保实时显示。"""
    import sys

    def stream_fn(chunk: str) -> None:
        sys.stdout.write(chunk)
        sys.stdout.flush()

    return stream_fn


def _build_agent(cwd: Path, yes: bool) -> CodingAgent:
    if yes:
        typer.echo("Warning: --yes is deprecated and does not bypass permission approval.", err=True)
    settings = Settings.from_env(workspace=cwd.resolve(), auto_confirm_risky_writes=False)
    confirm_fn = _make_confirm_fn(cwd)
    stream_fn = _make_stream_fn()
    return CodingAgent(settings, confirm_fn=confirm_fn, stream_fn=stream_fn)


def _print_result(result: dict) -> None:
    typer.echo(f"session_id: {result.get('session_id')}")
    typer.echo(f"status: {result.get('status')}")
    if result.get("error"):
        typer.echo(f"error: {result.get('error')}")
    if result.get("verification_note"):
        typer.echo(f"verification_note: {result.get('verification_note')}")
    _print_verification(result)
    if result.get("status") == "awaiting_human_confirm":
        typer.echo("Approval required; nothing pending was executed. Resume from an interactive terminal.")
    if result.get("status") == "awaiting_context":
        typer.echo(f"Context paused: {result.get('context_error', '')}")
        typer.echo("Adjust context configuration if necessary, then resume; this is not task completion.")
        return
    typer.echo("summary:")
    typer.echo(result.get("summary", ""))


def _print_verification(state: dict, *, verbose: bool = False) -> None:
    check = state.get("static_check", {})
    if check:
        pending = check.get("revision") != state.get("change_revision", 0) and check.get("status") != "not_run"
        typer.echo(f"static_check: {check.get('status', 'not_run')}; snapshot_complete={check.get('snapshot_complete', True)}"
                   + ("; newer changes pending checks" if pending else ""))
        if verbose:
            for error in check.get("errors", []):
                typer.echo(f"  static error: {error}")
    results = state.get("test_results", [])
    if not results:
        typer.echo("tests: not_run (no recorded test execution this turn)")
    for result in results if verbose else results[-1:]:
        scope = result.get("scope", {})
        typer.echo(f"tests: {result.get('status', 'unknown')}; freshness={result.get('freshness', 'unknown')}; "
                   f"scope={scope.get('kind', 'unknown')}; targets={scope.get('targets', [])}; selectors={scope.get('selectors', {})}")
        typer.echo(f"  command: {result.get('command', '')}; cwd: {result.get('cwd', '')}; exit_code={result.get('exit_code')}")
        if result.get("reason"):
            typer.echo(f"  reason: {result['reason'][:400]}")
        if verbose:
            typer.echo(f"  executables: {result.get('executables', [])}")
            typer.echo(f"  scope_note: {result.get('scope_note', 'Actual coverage unknown.')}")
    if state.get("legacy_verification_note"):
        typer.echo(f"historical_verification (freshness unknown): {state['legacy_verification_note']}")


@app.command()
def run(
    task: str = typer.Argument(..., help="Task for the coding agent."),
    cwd: Path = typer.Option(Path("."), "--cwd", help="Workspace path."),
    session_id: str | None = typer.Option(None, "--session-id", help="Optional session id."),
    yes: bool = typer.Option(False, "--yes", help="Deprecated; does not bypass permissions."),
) -> None:
    """Run a new session (or add first turn to an explicit session)."""
    agent = _build_agent(cwd, yes)
    result = agent.run(task=task, cwd=cwd, session_id=session_id)
    _print_result(result)


@app.command()
def resume(
    session_id: str = typer.Argument(..., help="Session id to resume."),
    cwd: Path = typer.Option(Path("."), "--cwd", help="Workspace path."),
    yes: bool = typer.Option(False, "--yes", help="Deprecated; does not bypass permissions."),
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
    yes: bool = typer.Option(False, "--yes", help="Deprecated; does not bypass permissions."),
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
    typer.echo("Commands: /exit, /new, /session, /session <id>, /resume, /revoke, /context")

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
        if user_input == "/revoke":
            agent.permissions.grants = {g for g in agent.permissions.grants if g[0] != sid}
            typer.echo("Current session approvals revoked.")
            continue
        if user_input == "/resume":
            _print_result(agent.resume(sid))
            continue
        if user_input == "/context":
            _print_context(agent, sid)
            continue

        if user_input.startswith("/session"):
            parts = user_input.split(maxsplit=1)
            if len(parts) == 1:
                typer.echo(f"session_id: {sid}")
            else:
                sid = parts[1].strip()
                typer.echo(f"switched session_id: {sid}")
            continue

        try:
            result = agent.run_turn(session_id=sid, user_request=user_input, cwd=cwd)
            _print_result(result)
        except ValueError as exc:
            typer.echo(str(exc), err=True)


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
            typer.echo(f"legacy_verification_mode (ignored): {state.get('verification_mode')}")
        if state.get("verification_note"):
            typer.echo(f"verification_note: {state.get('verification_note')}")
        _print_verification(state, verbose=True)

        # ── Token 用量统计 ──────────────────────────────────────────────────
        try:
            token_summary = agent.store.get_token_summary(sid)
            if token_summary["llm_calls"] > 0:
                typer.echo(
                    f"token_usage (session): "
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
                typer.echo(f"token_usage (this turn, act): {state.get('token_usage', {})}")
                typer.echo(f"token_usage (session, compact): {state.get('compact_usage', {})}")
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
    if verbose:
        for event in agent.store.get_permission_events(sid):
            decision = event["decision"]
            typer.echo(f"[permission] {event['tool']}: {decision['decision']} {decision['rule_id']} -> {event['outcome']}")


def _print_context(agent: CodingAgent, sid: str) -> None:
    import json
    state = agent.store.load_state(sid)
    if state is None:
        raise typer.BadParameter("Session not found.")
    typer.echo(f"session_id: {sid}")
    stats = state.get("context_stats", {})
    typer.echo("last_request: " + json.dumps(stats, ensure_ascii=False))
    try:
        from app.context.memory import messages, migrate
        from app.prompts import SYSTEM_PROMPT
        migrate(state)
        history = messages(state["session_history"], state.get("context_summary", ""))
        estimate = agent.nodes.context_manager.count(state, SYSTEM_PROMPT, agent.nodes.action_prompt(state, history), history, agent.tools.schemas)
        typer.echo("current_request_estimate: " + json.dumps(estimate, ensure_ascii=False))
    except Exception as exc:
        typer.echo(f"current_request_estimate: unavailable ({type(exc).__name__})")
    typer.echo(f"configured_input_limit: {agent.settings.context.input_limit}")
    typer.echo(f"retained_groups: {len(state.get('session_history', []))}; summary_present: {bool(state.get('context_summary'))}")
    details = agent.store.get_context_details(sid)
    details["compactions"] = [{**{key: item.get(key) for key in ("status", "before_tokens", "after_tokens", "target_met", "model", "calls", "downgraded")},
                               "source_count": len(item.get("source_records", []))} for item in details["compactions"]]
    typer.echo("archive: " + json.dumps(details, ensure_ascii=False))
    if state.get("context_error"):
        typer.echo("context_error: " + state["context_error"])


@app.command()
def context(session_id: str | None = typer.Argument(None), cwd: Path = typer.Option(Path("."), "--cwd")) -> None:
    """Show stored input estimates, budgets, compactions and archive coverage without a model call."""
    agent = _build_agent(cwd, False)
    sid = session_id or agent.last_session_id()
    if not sid:
        raise typer.BadParameter("No session found.")
    _print_context(agent, sid)


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
