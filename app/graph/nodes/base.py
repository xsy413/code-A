from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Callable

from app.config import Settings
from app.graph.state import AgentState
from app.llm import LLMClient
from app.permissions import ApprovalAnswer
from app.store import SQLiteStore
from app.tools import ToolExecutor, ToolInput, ToolResult
from app.context.memory import append_group, messages, migrate
from app.context.manager import ContextManager


class _NodeBase:
    def __init__(
        self,
        llm: LLMClient,
        store: SQLiteStore,
        tools: ToolExecutor,
        settings: Settings,
        confirm_fn: Callable[[str, dict], ApprovalAnswer | bool] | None = None,
        stream_fn: Callable[[str], None] | None = None,
    ) -> None:
        self.llm = llm
        self.store = store
        self.tools = tools
        self.settings = settings
        self.confirm_fn = confirm_fn
        self.stream_fn = stream_fn
        self.context_manager = ContextManager(settings, store)

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

    def _redact(self, value):
        return self.tools.permissions.redact(value) if hasattr(self.tools, "permissions") else value

    def _request_stop(self, state: AgentState, reason: str) -> None:
        if not state.get("stop_reason"):
            state["stop_reason"] = reason

    def _return_to_model(self, state: AgentState) -> None:
        state["status"] = "failed" if state.get("stop_reason") else "acting"
        state["needs_more_action"] = not bool(state.get("stop_reason"))

    def _register_failure(self, state: AgentState, identity: dict, errors: list[dict]) -> None:
        evidence = [{k: error.get(k) for k in ("error_kind", "exit_code", "stderr", "stdout")} for error in errors]
        signature = hashlib.sha256(json.dumps(self._redact({"identity": identity, "errors": evidence}),
                                             sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        state["recent_failures"] = [*state.get("recent_failures", []), signature][-3:]
        state["retry_attempts"] = int(state.get("retry_attempts", 0)) + 1
        if len(state["recent_failures"]) == 3 and len(set(state["recent_failures"])) == 1:
            self._request_stop(state, "Detected repeated identical failure three times; stopped without blind retries.")
        if state["retry_attempts"] >= self.settings.max_retry_steps:
            self._request_stop(state, f"Reached MAX_RETRY_STEPS={self.settings.max_retry_steps}.")

    def _observe_error(self, state: AgentState, kind: str, message: str, identity: dict | None = None,
                       result: ToolResult | None = None) -> None:
        message = self._redact(message)
        state["error"] = message
        observation = {"error_kind": kind, "stderr": message, "exit_code": None, "ok": False}
        if result is not None:
            observation.update({"exit_code": result.exit_code, "stdout": self._redact(result.stdout),
                                "execution_status": result.execution_status, "permission": result.permission})
        self._append_observation(state, observation)
        self._register_failure(state, identity or {"kind": kind}, [observation])
        self._return_to_model(state)

    def _append_observation(self, state: AgentState, observation: dict) -> None:
        self._ensure_history(state)
        migrate(state)
        group = {"type": "observation", "content": json.dumps(self._redact(observation), ensure_ascii=False)}
        state["action_history"].append(group)
        append_group(state, group)

    def _conversation_summary(self, state: AgentState) -> str:
        summary = str(state.get("context_summary", "")).strip()
        return summary if summary else "(none)"

    def _recent_turns_text(self, state: AgentState) -> str:
        return "(session messages are retained in history)"

    def _current_request(self, state: AgentState) -> str:
        return str(state.get("last_user_request") or state.get("task") or "")

    def _validate_changed_files(self, state: AgentState) -> str | None:
        errors = self._collect_static_check(state)["errors"]
        return errors[0] if errors else None

    def _collect_static_check(self, state: AgentState) -> dict:
        changed_files = state.get("changed_files", [])
        workspace = Path(state["workspace"]).resolve()
        deleted = set(state.get("deleted_files", []))
        errors = [] if changed_files else ["verification requires recorded changed_files."]
        checks = []
        for rel_path in changed_files:
            path = (workspace / rel_path).resolve()
            entry = {"path": rel_path, "checks": ["path"], "status": "passed"}
            checks.append(entry)
            try:
                path.relative_to(workspace)
            except ValueError:
                if str(path) not in state.get("authorized_changed_files", []):
                    errors.append(f"changed file outside workspace: {rel_path}")
                    entry["status"] = "failed"
                    continue
            if rel_path in deleted:
                entry["checks"].append("deletion_record")
                if path.exists():
                    errors.append(f"deleted file still exists: {rel_path}")
                    entry["status"] = "failed"
                continue
            entry["checks"].append("file_exists")
            if not path.exists() or not path.is_file():
                errors.append(f"changed file not found: {rel_path}")
                entry["status"] = "failed"
                continue
            if path.suffix == ".py":
                entry["checks"].append("python_compile")
                try:
                    source = path.read_text(encoding="utf-8")
                    compile(source, str(path), "exec")
                except Exception as exc:
                    errors.append(f"py_compile check failed for {rel_path}: {exc}")
                    entry["status"] = "failed"
        complete = state.get("snapshot_complete", True)
        return {"status": "failed" if errors else "passed" if complete else "incomplete",
                "checked_files": list(changed_files), "checks": checks, "errors": errors,
                "snapshot_complete": complete, "revision": state.get("change_revision", 0)}

    def _reserve_tool_budget(self, state: AgentState, amount: int) -> bool:
        count = int(state.get("tool_call_count", 0)) + amount
        if count > self.settings.max_tool_calls:
            self._request_stop(state, f"Reached MAX_TOOL_CALLS={self.settings.max_tool_calls}; batch not executed.")
            return False
        state["tool_call_count"] = count
        return True

    def _is_write_like_tool(self, tool_name: str) -> bool:
        return tool_name in {"write_file", "patch_file", "delete_file"}

    def _ensure_history(self, state: AgentState) -> None:
        if state.get("action_history") or state.get("pending_batch"):
            return
        groups = []
        for call in state.get("tool_calls", []):
            assistant = call.get("assistant_message")
            if assistant and call.get("tool_call_id"):
                groups.append({"type": "tool_batch", "assistant_message": assistant, "results": [call]})
            else:
                groups.append({"type": "tool_observation", "results": [call]})
        state["action_history"] = groups

    def _build_action_history(self, state: AgentState) -> list[dict] | None:
        self._ensure_history(state)
        migrate(state)
        return self._redact(messages(state["session_history"], state.get("context_summary", ""))) or None

    def _tool_record(self, name: str, args: dict, result: ToolResult) -> dict:
        call = {
            "name": name,
            "args": args,
            "ok": result.ok,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.exit_code,
            "execution_status": result.execution_status,
            "error_kind": result.error_kind,
            "permission": result.permission,
            "changed_files": result.changed_files,
            "deleted_files": result.deleted_files,
            "snapshot_complete": result.snapshot_complete,
            "is_test": result.is_test,
            "test_info": result.test_info,
            "artifacts": result.artifacts,
            "result_id": result.result_id,
            "output_meta": result.output_meta,
        }
        return self._redact(call)

    def _session_backup_dir(self, state: AgentState) -> str:
        workspace = Path(state["workspace"]).resolve()
        session_id = str(state.get("session_id") or "")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", session_id):
            return ""
        return str(workspace / ".agent" / "backups" / session_id)

    def _make_payload(self, state: AgentState, name: str, args: dict) -> ToolInput:
        return ToolInput(
            name=name,
            args=args,
            cwd=state["workspace"],
            backup_dir=self._session_backup_dir(state),
            session_id=state["session_id"],
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
        if node_name == "act":
            self.context_manager.calibrate(state, usage.prompt_tokens)

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
