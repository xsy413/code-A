from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, Callable, Iterator

from app.config import Settings
from app.graph import AgentNodes, build_graph, ensure_state_defaults, new_state
from app.llm import OpenAICompatClient
from app.sandbox import SandboxPolicy
from app.store import SQLiteStore
from app.tools import ToolExecutor


class CodingAgent:
    def __init__(
        self,
        settings: Settings,
        confirm_fn: Callable[[str, dict], bool] | None = None,
        stream_fn: Callable[[str], None] | None = None,
    ) -> None:
        self.settings = settings
        self.store = SQLiteStore(settings.db_path)
        self.llm = OpenAICompatClient(
            api_key=settings.openai_api_key,
            model=settings.openai_model,
            base_url=settings.base_url,
        )
        self.sandbox = SandboxPolicy(settings.workspace, allowed_commands=set(settings.allowed_commands))
        self.tools = ToolExecutor(self.sandbox, test_command=settings.test_command)
        self.nodes = AgentNodes(
            self.llm, self.store, self.tools, settings,
            confirm_fn=confirm_fn,
            stream_fn=stream_fn,
        )
        self.graph = build_graph(self.nodes)

    def _truncate(self, text: str, max_chars: int) -> str:
        if len(text) <= max_chars:
            return text
        return text[: max_chars - 3] + "..."

    def _ensure_state(self, state: dict[str, Any] | None, session_id: str, cwd: Path) -> dict[str, Any]:
        if state is None:
            state = dict(new_state(session_id=session_id, task="", workspace=str(cwd.resolve())))
            state["status"] = "finished"
        return ensure_state_defaults(state, session_id=session_id, workspace=str(cwd.resolve()))

    def _compact_history(self, state: dict[str, Any]) -> None:
        turns = list(state.get("turns", []))
        max_turns = max(1, self.settings.max_context_turns)
        if len(turns) <= max_turns:
            return

        overflow = turns[:-max_turns]
        kept = turns[-max_turns:]
        existing_summary = str(state.get("conversation_summary", "")).strip()

        overflow_lines: list[str] = []
        for turn in overflow:
            req = self._truncate(str(turn.get("user_request", "")).replace("\n", " ").strip(), 120)
            status = str(turn.get("status", ""))
            conclusion = str(turn.get("summary") or turn.get("error") or "").replace("\n", " ").strip()
            conclusion = self._truncate(conclusion, 160)
            overflow_lines.append(f"[{status}] {req} => {conclusion}")

        merged = "\n".join([part for part in [existing_summary, *overflow_lines] if part])
        limit = max(200, self.settings.context_summary_max_chars)
        if len(merged) > limit:
            merged = merged[-limit:]

        state["conversation_summary"] = merged
        state["turns"] = kept

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
                }
            )

        state["turns"] = turns

    def _reset_turn_runtime(self, state: dict[str, Any], user_request: str, cwd: Path, turn_id: str) -> None:
        state["workspace"] = str(cwd.resolve())
        state["task"] = user_request
        state["last_user_request"] = user_request
        state["active_turn_id"] = turn_id

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
        state["last_tool_output"] = ""
        state["last_reflection"] = ""
        state["retry_attempts"] = 0
        state["tool_call_count"] = 0
        state["write_count"] = 0
        state["verification_mode"] = ""
        state["verification_note"] = ""
        state["finish_reason"] = ""
        state["recent_failures"] = []
        state["turn_progress"] = "none"
        state["needs_more_action"] = False
        state["explore_streak"] = 0
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

        turn_id = str(uuid.uuid4())
        self._reset_turn_runtime(state, user_request=user_request, cwd=cwd, turn_id=turn_id)
        self._compact_history(state)
        self.store.upsert_state(session_id, state)

        final_state = dict(self.graph.invoke(state))
        self._update_active_turn(final_state)
        self._compact_history(final_state)
        self.store.upsert_state(session_id, final_state)
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
        if current_status == "failed":
            state["status"] = "reflecting"
        elif current_status == "finished":
            return dict(state)

        final_state = dict(self.graph.invoke(state))
        self._update_active_turn(final_state)
        self._compact_history(final_state)
        self.store.upsert_state(session_id, final_state)
        return final_state

    def get_logs(self, session_id: str):
        return self.store.get_events(session_id)

    def last_session_id(self) -> str | None:
        return self.store.last_session_id()
