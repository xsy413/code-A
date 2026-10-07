from __future__ import annotations

import os
import time
from pathlib import Path

from app.graph.state import AgentState


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
            if not workspace.is_dir():
                raise NotADirectoryError(f"Workspace is not an existing directory: {workspace}")
            with os.scandir(workspace):
                pass
            state["workspace"] = str(workspace)
            state["workspace_snapshot"] = {}
            state["status"] = "acting"
            self._record(state, "preflight", started, "Workspace available; moved to acting.")
            return state
        except Exception as exc:
            state["status"] = "failed"
            state["error"] = f"preflight failed: {exc}"
            self._record(state, "preflight", started, "Preflight failed.", error=state["error"])
            return state

    def finish(self, state: AgentState) -> AgentState:
        started = time.time()
        if state.get("stop_reason"):
            state["status"] = "failed"
        if not state.get("summary"):
            state["summary"] = (
                f"status={state.get('status')} retry_attempts={state.get('retry_attempts')} "
                f"tool_calls={state.get('tool_call_count')} "
                f"verification_note={state.get('verification_note', '')} error={state.get('error', '')}"
            )
            if state.get("stop_reason"):
                state["summary"] = state["stop_reason"] + "\n" + state["summary"]
        state["needs_more_action"] = False
        self._record(state, "finish", started, "Run finished.")
        return state
