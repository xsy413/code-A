from __future__ import annotations

from app.graph.state import AgentState


def route_from_status(state: AgentState) -> str:
    status = state.get("status", "failed")
    if status in {"finished", "failed"}:
        return "finish"
    if status == "intake":
        return "intake"
    if status == "preflighting":
        return "preflight"
    if status == "planning":
        return "plan"
    if status == "acting":
        return "act"
    if status == "awaiting_human_confirm":
        return "human_confirm"
    if status == "verifying":
        return "verify"
    if status == "diagnosing":
        return "diagnose"
    if status == "reflecting":
        return "reflect"
    return "finish"
