from __future__ import annotations

import time
from pathlib import Path

from app.graph.state import AgentState
from app.prompts import PLAN_PROMPT, SUMMARY_PROMPT, SYSTEM_PROMPT


class _LifecycleNode:
    def intake(self, state: AgentState) -> AgentState:
        started = time.time()
        state["status"] = "preflighting"
        self._record(state, "intake", started, "Accepted task and moved to preflight.")
        return state

    def preflight(self, state: AgentState) -> AgentState:
        started = time.time()
        try:
            workspace = Path(state["workspace"]).resolve()
            tests_dir = workspace / "tests"
            has_pyproject = (workspace / "pyproject.toml").exists()
            has_requirements = (workspace / "requirements.txt").exists()

            source_roots: list[str] = []
            for candidate in ("src", "app"):
                p = workspace / candidate
                if p.exists() and p.is_dir():
                    source_roots.append(candidate)

            top_entries = sorted([p.name for p in workspace.iterdir() if not p.name.startswith(".")])[:50]
            state["workspace_snapshot"] = {
                "has_tests": tests_dir.exists(),
                "has_pyproject": has_pyproject,
                "has_requirements": has_requirements,
                "source_roots": source_roots,
                "top_entries": top_entries,
            }
            state["status"] = "planning"
            self._record(state, "preflight", started, "Collected workspace snapshot.")
            return state
        except Exception as exc:
            state["status"] = "failed"
            state["error"] = f"preflight failed: {exc}"
            self._record(state, "preflight", started, "Preflight failed.", error=state["error"])
            return state

    def plan(self, state: AgentState) -> AgentState:
        started = time.time()
        try:
            prompt = PLAN_PROMPT.format(
                current_request=self._current_request(state),
                workspace=state["workspace"],
                workspace_snapshot=state.get("workspace_snapshot", {}),
                conversation_summary=self._conversation_summary(state),
                recent_turns=self._recent_turns_text(state),
            )
            if self.stream_fn:
                self.stream_fn("\n\U0001f4cb Planning...\n")
            plan_text = self._stream_or_complete_text(SYSTEM_PROMPT, prompt)
            if self.stream_fn:
                self.stream_fn("\n")
            self._accumulate_usage(state, "plan")
            state["plan"] = plan_text
            state["status"] = "acting"
            self._record(state, "plan", started, "Plan generated.")
            return state
        except Exception as exc:
            state["status"] = "failed"
            state["error"] = f"planning failed: {exc}"
            self._record(state, "plan", started, "Plan failed.", error=state["error"])
            return state

    def finish(self, state: AgentState) -> AgentState:
        started = time.time()
        try:
            summary_prompt = SUMMARY_PROMPT.format(
                current_request=self._current_request(state),
                plan=state.get("plan", ""),
                conversation_summary=self._conversation_summary(state),
                recent_turns=self._recent_turns_text(state),
                tool_calls=state.get("tool_calls", []),
                status=state.get("status", ""),
                verification_note=state.get("verification_note", ""),
                error=state.get("error", ""),
            )
            if self.stream_fn:
                self.stream_fn("\n\U0001f4dd Summary...\n")
            state["summary"] = self._stream_or_complete_text(SYSTEM_PROMPT, summary_prompt)
            if self.stream_fn:
                self.stream_fn("\n")
            self._accumulate_usage(state, "finish")
        except Exception:
            state["summary"] = (
                f"status={state.get('status')} retry_attempts={state.get('retry_attempts')} "
                f"tool_calls={state.get('tool_call_count')} verification_note={state.get('verification_note', '')}"
            )
        self._record(state, "finish", started, "Run finished.")
        return state
