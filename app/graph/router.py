from __future__ import annotations

from app.graph.state import AgentState


def route_from_status(state: AgentState) -> str:
    status = state.get("status", "failed")
    if status == "awaiting_context":
        return "pause"
    if status in {"finished", "failed"}:
        return "finish"
    if status == "intake":
        return "intake"
    if status == "preflighting":
        return "preflight"
    # Old persisted sessions may still be waiting at the removed planning stage.
    if status in {"planning", "acting"}:
        return "act"
    if status == "executing":
        return "execute"
    if status == "awaiting_human_confirm":
        return "human_confirm"
    if status == "verifying":
        return "verify"
    if status in {"diagnosing", "reflecting"}:
        return "act"
    return "finish"
