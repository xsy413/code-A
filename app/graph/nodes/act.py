from __future__ import annotations

import time
import json

from app.graph.state import AgentState
from app.prompts import ACTION_PROMPT, SYSTEM_PROMPT
from app.context.manager import ContextPaused
from app.context.memory import append_group, test_facts


class _ActNode:
    def action_prompt(self, state: AgentState, history: list[dict] | None) -> str:
        context = []
        test_summaries = test_facts(state.get("test_results", []))
        for label, value in (
            ("Workspace observations", state.get("workspace_snapshot")),
            ("Verification note", state.get("verification_note")),
            ("Latest static check (not a test result)", json.dumps(state.get("static_check", {}), ensure_ascii=False)),
            ("Test results (stale results do not validate current files)", json.dumps(test_summaries, ensure_ascii=False)),
            ("Historical verification note (freshness unknown)", state.get("legacy_verification_note")),
            ("Test command reference (NOT automatically executed)", self.settings.test_command),
            ("Last recorded failure (may be historical; compare latest results)", state.get("error")),
            ("Current file versions and historical reading evidence", json.dumps(state.get("file_read_index", []), ensure_ascii=False)),
            ("Execution budgets and changes", json.dumps({k: state.get(k) for k in ("active_turn_id", "tool_call_count", "retry_attempts", "changed_files", "change_revision", "execution_errors")}, ensure_ascii=False)),
            ("Program-maintained session facts (take precedence over summaries)", json.dumps(state.get("session_facts", {}), ensure_ascii=False)),
        ):
            if value:
                context.append(f"{label}: {value}")
        if not history and state.get("last_tool_output"):
            context.append(f"Last tool output: {state['last_tool_output']}")
        request = self._current_request(state)
        if any(g.get("turn_id") == state.get("active_turn_id") and g.get("message", {}).get("role") == "user"
               and g["message"].get("content") == request for g in state.get("session_history", [])):
            request = "(the current turn's full user message in session history; do not revive older tasks)"
        return self._redact(ACTION_PROMPT.format(
            current_request=request, workspace=state["workspace"],
            conversation_summary="(see session history)", recent_turns="(see session history)",
            runtime_context="\n".join(context)))

    def act(self, state: AgentState) -> AgentState:
        started = time.time()
        if state.get("pending_batch"):
            state["status"] = "executing"
            return state
        try:
            history = self._build_action_history(state)
            prompt = self.action_prompt(state, history)
            history = self.context_manager.prepare(state, SYSTEM_PROMPT, prompt, self.tools.schemas, prompt_factory=self.action_prompt)
            prompt = self.action_prompt(state, history)
            state["context_stats"] = self.context_manager.count(state, SYSTEM_PROMPT, prompt, history, self.tools.schemas)
            if state["context_stats"]["tokens"] > self.settings.context.input_limit:
                raise ContextPaused("Rebuilt current facts exceed the input limit; no model request was sent.")
            if hasattr(self.llm, "max_output_tokens"):
                self.llm.max_output_tokens = self.settings.context.output_reserve
            if self.stream_fn:
                self.stream_fn(f"\n[act; tool requests={state.get('tool_call_count', 0)}] deciding...\n")
            try:
                response = self.llm.complete_action(SYSTEM_PROMPT, prompt, tools=self.tools.schemas, history=history)
            finally:
                self._accumulate_usage(state, "act")
            if not response.tool_calls:
                if response.finish_reason != "stop" or not response.content.strip():
                    raise RuntimeError("Invalid action response: expected complete nonempty final text.")
                state["summary"] = response.content
                state["finish_reason"] = "assistant_response"
                state["needs_more_action"] = False
                state["status"] = "finished"
                append_group(state, {"type": "message", "message": response.to_message()})
                self._record(state, "act", started, "Received final assistant answer.")
                return state
            if response.finish_reason != "tool_calls":
                raise RuntimeError("Invalid action response: incomplete tool calls.")
            ids = [call.id for call in response.tool_calls]
            if any(not isinstance(value, str) or not value.strip() for value in ids) or len(set(ids)) != len(ids):
                raise RuntimeError("Invalid action response: tool IDs must be nonempty and unique.")
            if any(not isinstance(call.name, str) or not call.name.strip() for call in response.tool_calls):
                raise RuntimeError("Invalid action response: tool name is required.")
        except ContextPaused as exc:
            state["status"] = "awaiting_context"
            state["context_error"] = self._redact(str(exc))
            self._record(state, "act", started, "Paused before model request: context unavailable.")
            return state
        except Exception as exc:
            if getattr(exc, "capacity_error", False):
                state["status"] = "awaiting_context"
                state["context_error"] = "Model service rejected the configured context capacity. Adjust CONTEXT_WINDOW/tokenizer/estimation settings and resume."
                self._record(state, "act", started, "Paused due to explicit service capacity error.")
                return state
            self._observe_error(state, "protocol_error", f"Action response error: {exc}")
            self._record(state, "act", started, "Response error returned as an observation.", error=state["error"])
            return state

        calls = [{"tool": call.name, "args": call.args, "tool_call_id": call.id,
                  "raw_arguments": call.raw_arguments, "argument_error": call.argument_error}
                 for call in response.tool_calls]
        self._accept_batch(state, calls, "act", response.to_message())
        self._record(state, "act", started, f"Accepted {len(calls)} tool requests.")
        return state
