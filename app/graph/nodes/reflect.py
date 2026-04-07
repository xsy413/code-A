from __future__ import annotations

import time

from app.graph.state import AgentState
from app.prompts import REFLECT_PROMPT, SYSTEM_PROMPT


class _ReflectNode:
    def reflect(self, state: AgentState) -> AgentState:
        started = time.time()
        if not self._track_failure(state, state.get("error", "")):
            self._record(state, "reflect", started, "Repeated failure short-circuit.", error=state["error"])
            return state

        retry_attempts = int(state.get("retry_attempts", 0)) + 1
        state["retry_attempts"] = retry_attempts
        if retry_attempts >= self.settings.max_retry_steps:
            state["status"] = "failed"
            state["error"] = f"Reached MAX_RETRY_STEPS={self.settings.max_retry_steps}."
            self._record(state, "reflect", started, "Max retry steps reached.", error=state["error"])
            return state

        try:
            prompt = REFLECT_PROMPT.format(
                current_request=self._current_request(state),
                plan=state.get("plan", ""),
                conversation_summary=self._conversation_summary(state),
                recent_turns=self._recent_turns_text(state),
                failure=state.get("error", ""),
                failure_type=state.get("failure_type", ""),
                root_cause_hypothesis=state.get("root_cause_hypothesis", ""),
                required_next_action=state.get("required_next_action", ""),
            )
            if self.stream_fn:
                self.stream_fn("\n\U0001f504 Reflecting...\n")
            reflection = self._stream_or_complete_text(SYSTEM_PROMPT, prompt, temperature=0.4)
            if self.stream_fn:
                self.stream_fn("\n")
            self._accumulate_usage(state, "reflect")
            state["last_reflection"] = reflection
            state["status"] = "acting"
            state["needs_more_action"] = True
            self._record(state, "reflect", started, "Reflected and retrying.")
            return state
        except Exception as exc:
            state["status"] = "failed"
            state["error"] = f"reflection failed: {exc}"
            self._record(state, "reflect", started, "Reflection failed.", error=state["error"])
            return state
