from __future__ import annotations

import copy
import uuid
import sqlite3
from pathlib import Path
from typing import Any, Callable, Iterator

from app.config import Settings
from app.graph import AgentNodes, build_graph, ensure_state_defaults, new_state
from app.llm import OpenAICompatClient
from app.permissions import ApprovalAnswer, PermissionEngine
from app.sandbox import SandboxPolicy
from app.store import SQLiteStore
from app.tools import ToolExecutor


class CodingAgent:
    def __init__(
        self,
        settings: Settings,
        confirm_fn: Callable[[str, dict], ApprovalAnswer | bool] | None = None,
        stream_fn: Callable[[str], None] | None = None,
    ) -> None:
        self.settings = settings
        self.store = SQLiteStore(settings.db_path)
        self.llm = OpenAICompatClient(
            api_key=settings.openai_api_key,
            model=settings.openai_model,
            base_url=settings.base_url,
            max_output_tokens=settings.context.output_reserve,
        )
        self.sandbox = SandboxPolicy(settings.workspace, allowed_commands=set(settings.allowed_commands))
        self.permissions = PermissionEngine(settings.workspace, config_path=settings.permissions_path)
        self.permissions.register_secret(settings.openai_api_key)
        self.permissions.register_secret(settings.context.compact_api_key)
        for tokenizer in (settings.context.tokenizer, settings.context.compact_tokenizer):
            if tokenizer:
                self.permissions.protected_context_paths.add(Path(tokenizer).expanduser().resolve())
        self.store.sanitize = self.permissions.redact
        self.tools = ToolExecutor(self.sandbox, test_command=settings.test_command,
                                  permissions=self.permissions, audit=self.store.add_permission_event)
        self.tools.store = self.store
        self.tools.context = settings.context
        self.store.result_bytes = settings.context.result_bytes
        self.store.session_bytes = settings.context.session_bytes
        self.nodes = AgentNodes(
            self.llm, self.store, self.tools, settings,
            confirm_fn=confirm_fn,
            stream_fn=stream_fn,
        )
        self.graph = build_graph(self.nodes)

    def _ensure_state(self, state: dict[str, Any] | None, session_id: str, cwd: Path) -> dict[str, Any]:
        if state is None:
            state = dict(new_state(session_id=session_id, task="", workspace=str(cwd.resolve())))
            state["status"] = "finished"
        elif state.get("context_version", 0) == 0:
            # Recover old results absent from the already-clipped state as observations, never fake pairs.
            import json
            retained = state.get("action_history", [])
            known = {json.dumps(call, sort_keys=True, ensure_ascii=False) for group in retained for call in group.get("results", [])}
            known.update(json.dumps(call, sort_keys=True, ensure_ascii=False) for call in state.get("tool_calls", []))
            missing = [call for call in self.store.get_tool_records(session_id)
                       if json.dumps(call, sort_keys=True, ensure_ascii=False) not in known]
            if missing:
                state["action_history"] = [{"type": "tool_observation", "results": [call]} for call in missing] + retained
        return ensure_state_defaults(state, session_id=session_id, workspace=str(cwd.resolve()))

    def _update_active_turn(self, state: dict[str, Any]) -> None:
        active_turn_id = str(state.get("active_turn_id", ""))
        if not active_turn_id:
            return

        turns = list(state.get("turns", []))
        for turn in turns:
            if str(turn.get("turn_id", "")) == active_turn_id:
                turn["status"] = state.get("status", "")
                turn["summary"] = state.get("summary", "")
                turn["error"] = state.get("error", "")
                turn["verification_note"] = state.get("verification_note", "")
                turn["changed_files"] = list(state.get("changed_files", []))
                turn["finish_reason"] = state.get("finish_reason", "")
                turn["tool_calls"] = len(state.get("tool_calls", []))
                turn["static_check"] = copy.deepcopy(state.get("static_check", {}))
                turn["test_results"] = copy.deepcopy(state.get("test_results", []))
                break
        else:
            turns.append(
                {
                    "turn_id": active_turn_id,
                    "user_request": state.get("last_user_request", ""),
                    "status": state.get("status", ""),
                    "summary": state.get("summary", ""),
                    "error": state.get("error", ""),
                    "verification_note": state.get("verification_note", ""),
                    "changed_files": list(state.get("changed_files", [])),
                    "finish_reason": state.get("finish_reason", ""),
                    "tool_calls": len(state.get("tool_calls", [])),
                    "static_check": copy.deepcopy(state.get("static_check", {})),
                    "test_results": copy.deepcopy(state.get("test_results", [])),
                }
            )

        state["turns"] = turns

    def _reset_turn_runtime(self, state: dict[str, Any], user_request: str, cwd: Path, turn_id: str) -> None:
        state["workspace"] = str(cwd.resolve())
        state["task"] = user_request
        state["last_user_request"] = user_request
        state["active_turn_id"] = turn_id
        old_check = state.get("session_facts", {}).get("latest_static_check")
        if old_check:
            old_check["historical"] = True
        from app.context.memory import append_group
        append_group(state, {"type": "message", "turn_id": turn_id, "message": {"role": "user", "content": user_request}})

        turns = list(state.get("turns", []))
        turns.append(
            {
                "turn_id": turn_id,
                "user_request": user_request,
                "status": "running",
                "summary": "",
                "error": "",
                "verification_note": "",
                "changed_files": [],
                "finish_reason": "",
                "tool_calls": 0,
            }
        )
        state["turns"] = turns

        state["plan"] = ""
        state["messages"] = []
        state["tool_calls"] = []
        state["artifacts"] = []
        state["changed_files"] = []
        state["deleted_files"] = []
        state["status"] = "intake"
        state["error"] = ""
        state["summary"] = ""
        state["pending_action"] = {}
        state["pending_batch"] = {}
        state["action_history"] = []
        state["stop_reason"] = ""
        state["approval_unavailable"] = False
        state["snapshot_complete"] = True
        state["authorized_changed_files"] = []
        state["execution_errors"] = []
        state["last_tool_output"] = ""
        state["last_reflection"] = ""
        state["retry_attempts"] = 0
        state["tool_call_count"] = 0
        state["write_count"] = 0
        state["verification_mode"] = ""
        state["verification_note"] = ""
        state["legacy_verification_note"] = ""
        state["static_check"] = {"status": "not_run", "checked_files": [], "checks": [], "errors": [], "snapshot_complete": True}
        state["test_results"] = []
        state["change_revision"] = 0
        state["finish_reason"] = ""
        state["recent_failures"] = []
        state["turn_progress"] = "none"
        state["needs_more_action"] = False
        state["explore_streak"] = 0
        state["workspace_snapshot"] = {}
        state["failure_type"] = ""
        state["failure_signature"] = ""
        state["root_cause_hypothesis"] = ""
        state["required_next_action"] = ""
        state["target_test"] = ""
        state["diagnostic_budget_used"] = 0
        state["token_usage"] = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "llm_calls": 0}

    def start_session(self, cwd: Path) -> str:
        sid = str(uuid.uuid4())
        state = dict(new_state(session_id=sid, task="", workspace=str(cwd.resolve())))
        state["status"] = "finished"
        state["summary"] = "Session initialized."
        self.store.upsert_state(sid, state)
        return sid

    def run_turn(self, session_id: str, user_request: str, cwd: Path) -> dict:
        loaded = self.store.load_state(session_id)
        state = self._ensure_state(loaded, session_id=session_id, cwd=cwd)
        if state.get("pending_action") or state.get("pending_batch") or state.get("status") == "awaiting_context":
            raise ValueError("Session has a pending approval; resume it or start a new session.")

        turn_id = str(uuid.uuid4())
        self._reset_turn_runtime(state, user_request=user_request, cwd=cwd, turn_id=turn_id)
        self.store.upsert_state(session_id, state)

        final_state = self._invoke_graph(state)
        self._update_active_turn(final_state)
        self._save_final_state(final_state)
        return final_state

    def run(self, task: str, cwd: Path, session_id: str | None = None) -> dict:
        sid = session_id or self.start_session(cwd)
        return self.run_turn(session_id=sid, user_request=task, cwd=cwd)

    def resume(self, session_id: str) -> dict:
        state = self.store.load_state(session_id)
        if not state:
            raise ValueError(f"Session not found: {session_id}")

        state = self._ensure_state(state, session_id=session_id, cwd=Path(str(state.get("workspace", self.settings.workspace))))
        current_status = str(state.get("status", ""))
        legacy_verify = (state.get("pending_batch", {}).get("origin") == "verify"
                         or state.get("pending_action", {}).get("origin") == "verify")
        if legacy_verify:
            state["status"] = "executing"
        elif current_status == "failed":
            if int(state.get("tool_call_count", 0)) >= self.settings.max_tool_calls:
                raise ValueError("Tool budget exhausted; start a new turn or session.")
            state["status"] = "executing" if state.get("pending_batch") else "acting"
            state["summary"] = ""
            state["stop_reason"] = ""
            state["retry_attempts"] = 0
            state["recent_failures"] = []
        elif current_status == "finished":
            return dict(state)
        elif current_status == "awaiting_context":
            state["status"] = "executing" if state.get("pending_batch") else "acting"
            state["context_error"] = ""

        final_state = self._invoke_graph(state)
        self._update_active_turn(final_state)
        self._save_final_state(final_state)
        return final_state

    def _save_final_state(self, state: dict) -> None:
        try:
            self.store.upsert_state(state["session_id"], state)
        except (sqlite3.Error, OSError):
            state["status"] = "awaiting_context"
            state["context_error"] = "State persistence unavailable. Repair storage before resuming; executed tools must not be replayed."

    def _invoke_graph(self, state: dict) -> dict:
        # Dispatch, verification and recovery each add graph steps to one action.
        recursion_limit = max(25, 8 * (self.settings.max_tool_calls + self.settings.max_retry_steps + 1) + 16)
        from app.context.manager import ContextPaused
        try:
            return dict(self.graph.invoke(state, config={"recursion_limit": recursion_limit}))
        except (ContextPaused, sqlite3.Error, OSError) as exc:
            try:
                checkpoint = self.store.load_state(state["session_id"]) or getattr(exc, "checkpoint", state)
            except (sqlite3.Error, OSError):
                checkpoint = getattr(exc, "checkpoint", state)
            checkpoint["status"] = "awaiting_context"
            checkpoint["context_error"] = self.permissions.redact(str(exc))
            return checkpoint

    def get_logs(self, session_id: str):
        return self.store.get_events(session_id)

    def last_session_id(self) -> str | None:
        return self.store.last_session_id()
