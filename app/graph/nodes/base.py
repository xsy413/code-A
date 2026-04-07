from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable

from app.config import Settings
from app.graph.state import AgentState
from app.llm import LLMClient
from app.store import SQLiteStore
from app.tools import ToolExecutor, ToolInput


class _NodeBase:
    def __init__(
        self,
        llm: LLMClient,
        store: SQLiteStore,
        tools: ToolExecutor,
        settings: Settings,
        confirm_fn: Callable[[str, dict], bool] | None = None,
        stream_fn: Callable[[str], None] | None = None,
    ) -> None:
        self.llm = llm
        self.store = store
        self.tools = tools
        self.settings = settings
        self.confirm_fn = confirm_fn
        self.stream_fn = stream_fn

    def _record(self, state: AgentState, node_name: str, started: float, out: str, error: str = "") -> None:
        session_id = state["session_id"]
        duration_ms = int((time.time() - started) * 1000)
        self.store.add_event(
            session_id=session_id,
            node_name=node_name,
            duration_ms=duration_ms,
            input_summary=state.get("status", ""),
            output_summary=out[:500],
            error=error[:500],
        )
        self.store.upsert_state(session_id, state)

    def _normalize_failure(self, text: str) -> str:
        lowered = (text or "").lower().strip()
        if "invalid_write_path" in lowered:
            return "real_failure:invalid_path"
        if "no_code_change_yet" in lowered:
            return "no_code_change_yet"
        if "no tests collected" in lowered or "no tests ran" in lowered:
            return "verification_unavailable:no_tests"
        if "error at setup" in lowered or "eeee" in lowered:
            return "real_failure:test_setup"
        if "syntaxerror" in lowered or "traceback" in lowered:
            return "real_failure:runtime"
        if "permission" in lowered:
            return "real_failure:permission"
        return lowered[:200]

    def _conversation_summary(self, state: AgentState) -> str:
        summary = str(state.get("conversation_summary", "")).strip()
        return summary if summary else "(none)"

    def _recent_turns_text(self, state: AgentState) -> str:
        turns = list(state.get("turns", []))
        if not turns:
            return "(none)"

        active_turn_id = str(state.get("active_turn_id", ""))
        previous_turns = [t for t in turns if str(t.get("turn_id", "")) != active_turn_id]
        if not previous_turns:
            return "(none)"

        max_turns = max(1, self.settings.max_context_turns)
        recent = previous_turns[-max_turns:]
        lines: list[str] = []
        for turn in recent:
            req = str(turn.get("user_request", "")).replace("\n", " ").strip()
            status = str(turn.get("status", ""))
            summary = str(turn.get("summary", "")).replace("\n", " ").strip()
            if len(req) > 120:
                req = req[:117] + "..."
            if len(summary) > 120:
                summary = summary[:117] + "..."
            lines.append(f"- [{status}] user: {req} | summary: {summary}")
        return "\n".join(lines) if lines else "(none)"

    def _current_request(self, state: AgentState) -> str:
        return str(state.get("last_user_request") or state.get("task") or "")

    def _has_test_entry(self, workspace: Path) -> bool:
        if (workspace / "tests").exists():
            return True
        for p in workspace.rglob("test_*.py"):
            if p.is_file():
                return True
        for p in workspace.rglob("*_test.py"):
            if p.is_file():
                return True
        return False

    def _validate_finish(self, state: AgentState, changed_files: list[str], reason: str) -> str | None:
        if not changed_files:
            return "finish requires args.changed_files."
        if not reason.strip():
            return "finish requires args.completion_reason."

        workspace = Path(state["workspace"]).resolve()
        deleted = set(state.get("deleted_files", []))

        for rel_path in changed_files:
            path = (workspace / rel_path).resolve()
            try:
                path.relative_to(workspace)
            except ValueError:
                return f"finish changed file outside workspace: {rel_path}"

            if rel_path in deleted:
                continue

            if not path.exists() or not path.is_file():
                return f"finish changed file not found: {rel_path}"
            if path.suffix == ".py":
                try:
                    source = path.read_text(encoding="utf-8")
                    compile(source, str(path), "exec")
                except Exception as exc:
                    return f"py_compile check failed for {rel_path}: {exc}"
        return None

    def _bump_tool_budget(self, state: AgentState) -> bool:
        count = int(state.get("tool_call_count", 0)) + 1
        state["tool_call_count"] = count
        if count > self.settings.max_tool_calls:
            state["status"] = "failed"
            state["error"] = f"Reached MAX_TOOL_CALLS={self.settings.max_tool_calls}."
            return False
        return True

    def _track_failure(self, state: AgentState, failure: str) -> bool:
        signature = self._normalize_failure(failure)
        recent = list(state.get("recent_failures", []))
        recent.append(signature)
        state["recent_failures"] = recent[-3:]
        if len(state["recent_failures"]) >= 2 and state["recent_failures"][-1] == state["recent_failures"][-2]:
            state["status"] = "failed"
            state["error"] = (
                "Detected repeated identical failure twice; short-circuiting. "
                f"latest={state['recent_failures'][-1]}"
            )
            return False
        return True

    def _is_exploration_tool(self, tool_name: str) -> bool:
        return tool_name in {
            "list_files",
            "read_file",
            "search_text",
            "inspect_workspace",
            "python_probe",
            "run_test_target",
        }

    def _is_write_like_tool(self, tool_name: str) -> bool:
        return tool_name in {"write_file", "patch_file", "delete_file"}

    _MAX_ACTION_HISTORY = 6

    def _build_action_history(self, state: AgentState) -> list[dict] | None:
        tool_calls = list(state.get("tool_calls", []))
        if not tool_calls:
            return None

        recent = tool_calls[-self._MAX_ACTION_HISTORY:]
        history: list[dict] = []

        for call in recent:
            name = call.get("name", "")
            ok = call.get("ok", True)

            llm_decision = call.get("llm_decision")
            if llm_decision:
                assistant_content = json.dumps(llm_decision, ensure_ascii=False)
            else:
                assistant_content = json.dumps(
                    {"tool": name, "args": call.get("args", {})},
                    ensure_ascii=False,
                )
            history.append({"role": "assistant", "content": assistant_content})

            stdout = (call.get("stdout") or "")[:800]
            stderr = (call.get("stderr") or "")[:400]
            output = stdout if ok else (stderr or stdout)
            status_icon = "\u2705" if ok else "\u274c"
            history.append({"role": "user", "content": f"[{status_icon} {name}]\n{output}"})

        return history if history else None

    def _session_backup_dir(self, state: AgentState) -> str:
        workspace = Path(state["workspace"]).resolve()
        session_id = str(state.get("session_id") or "")
        if not session_id:
            return ""
        return str(workspace / ".agent" / "backups" / session_id)

    def _make_payload(self, state: AgentState, name: str, args: dict) -> ToolInput:
        return ToolInput(
            name=name,
            args=args,
            cwd=state["workspace"],
            backup_dir=self._session_backup_dir(state),
        )

    def _accumulate_usage(self, state: AgentState, node_name: str) -> None:
        usage = getattr(self.llm, "last_usage", None)
        if usage is None:
            return

        tu = dict(state.get("token_usage") or {})
        tu["prompt_tokens"] = tu.get("prompt_tokens", 0) + usage.prompt_tokens
        tu["completion_tokens"] = tu.get("completion_tokens", 0) + usage.completion_tokens
        tu["total_tokens"] = tu.get("total_tokens", 0) + usage.total_tokens
        tu["llm_calls"] = tu.get("llm_calls", 0) + 1
        state["token_usage"] = tu

        try:
            self.store.record_token_usage(
                session_id=state["session_id"],
                node_name=node_name,
                model=usage.model,
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
                total_tokens=usage.total_tokens,
            )
        except Exception:
            pass

    def _stream_or_complete_text(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        temperature: float = 0,
        history: list[dict] | None = None,
    ) -> str:
        _stream = getattr(self.llm, "stream_text", None)
        if self.stream_fn is not None and callable(_stream):
            chunks: list[str] = []
            for chunk in _stream(
                system_prompt,
                user_prompt,
                temperature=temperature,
                history=history,
            ):
                self.stream_fn(chunk)
                chunks.append(chunk)
            return "".join(chunks)
        return self.llm.complete_text(
            system_prompt, user_prompt, temperature=temperature, history=history
        )
