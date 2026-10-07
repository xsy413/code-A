from __future__ import annotations

from typing import Any, Literal, TypedDict

AgentStatus = Literal[
    "intake",
    "preflighting",
    "planning",
    "acting",
    "executing",
    "awaiting_human_confirm",
    "awaiting_context",
    "verifying",
    "diagnosing",
    "reflecting",
    "finished",
    "failed",
]

TurnProgress = Literal["none", "explored", "modified"]


class TurnRecord(TypedDict, total=False):
    turn_id: str
    user_request: str
    status: str
    summary: str
    error: str
    verification_note: str
    changed_files: list[str]
    finish_reason: str
    tool_calls: int
    static_check: dict[str, Any]
    test_results: list[dict[str, Any]]


class AgentState(TypedDict, total=False):
    context_version: int
    session_history: list[dict[str, Any]]
    context_summary: str
    context_stats: dict[str, Any]
    context_calibration: dict[str, float]
    context_error: str
    compaction_history: list[dict[str, Any]]
    compact_usage: dict[str, int]
    file_read_index: list[dict[str, Any]]
    session_facts: dict[str, Any]
    session_id: str
    task: str
    plan: str  # Legacy persisted field; not used by the active workflow.
    messages: list[dict[str, str]]
    workspace: str
    tool_calls: list[dict[str, Any]]
    artifacts: list[str]
    changed_files: list[str]
    deleted_files: list[str]
    status: AgentStatus
    error: str
    summary: str
    pending_action: dict[str, Any]
    pending_batch: dict[str, Any]
    action_history: list[dict[str, Any]]
    stop_reason: str
    approval_unavailable: bool
    snapshot_complete: bool
    authorized_changed_files: list[str]
    execution_errors: list[str]
    last_tool_output: str
    last_reflection: str
    retry_attempts: int
    tool_call_count: int
    write_count: int
    verification_mode: str
    verification_note: str
    legacy_verification_note: str
    static_check: dict[str, Any]
    test_results: list[dict[str, Any]]
    change_revision: int
    finish_reason: str
    recent_failures: list[str]
    conversation_summary: str
    turns: list[TurnRecord]
    active_turn_id: str
    last_user_request: str
    turn_progress: TurnProgress
    needs_more_action: bool
    explore_streak: int
    workspace_snapshot: dict[str, Any]
    failure_type: str
    failure_signature: str
    root_cause_hypothesis: str
    required_next_action: str
    target_test: str
    diagnostic_budget_used: int
    token_usage: dict  # {"prompt_tokens": int, "completion_tokens": int, "total_tokens": int, "llm_calls": int}


def new_state(session_id: str, task: str, workspace: str) -> AgentState:
    return AgentState(
        session_id=session_id,
        task=task,
        plan="",
        messages=[],
        workspace=workspace,
        tool_calls=[],
        artifacts=[],
        changed_files=[],
        deleted_files=[],
        status="intake",
        error="",
        summary="",
        pending_action={},
        pending_batch={},
        action_history=[],
        context_version=0,
        session_history=[],
        context_summary="",
        context_stats={},
        context_calibration={},
        context_error="",
        compaction_history=[],
        compact_usage={},
        file_read_index=[],
        session_facts={"file_versions": {}, "errors": [], "denials": []},
        stop_reason="",
        approval_unavailable=False,
        snapshot_complete=True,
        authorized_changed_files=[],
        execution_errors=[],
        last_tool_output="",
        last_reflection="",
        retry_attempts=0,
        tool_call_count=0,
        write_count=0,
        verification_mode="",
        verification_note="",
        legacy_verification_note="",
        static_check={"status": "not_run", "checked_files": [], "checks": [], "errors": [], "snapshot_complete": True},
        test_results=[],
        change_revision=0,
        finish_reason="",
        recent_failures=[],
        conversation_summary="",
        turns=[],
        active_turn_id="",
        last_user_request=task,
        turn_progress="none",
        needs_more_action=False,
        explore_streak=0,
        workspace_snapshot={},
        failure_type="",
        failure_signature="",
        root_cause_hypothesis="",
        required_next_action="",
        target_test="",
        diagnostic_budget_used=0,
        token_usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "llm_calls": 0},
    )


def ensure_state_defaults(state: AgentState, session_id: str, workspace: str) -> AgentState:
    from app.context.memory import migrate
    migrate(state)
    for key, value in {"context_stats": {}, "context_calibration": {}, "context_error": "",
                       "compaction_history": [], "compact_usage": {}, "file_read_index": [],
                       "session_facts": {"file_versions": {}, "errors": [], "denials": []}}.items():
        state.setdefault(key, value)
    if "static_check" not in state:
        state.setdefault("legacy_verification_note", state.get("verification_note", ""))
        state["verification_note"] = ""
    state.setdefault("session_id", session_id)
    state.setdefault("workspace", workspace)
    state.setdefault("task", "")
    state.setdefault("plan", "")
    state.setdefault("messages", [])
    state.setdefault("tool_calls", [])
    state.setdefault("artifacts", [])
    state.setdefault("changed_files", [])
    state.setdefault("deleted_files", [])
    state.setdefault("status", "finished")
    state.setdefault("error", "")
    state.setdefault("summary", "")
    state.setdefault("pending_action", {})
    state.setdefault("pending_batch", {})
    state.setdefault("action_history", [])
    state.setdefault("stop_reason", "")
    state.setdefault("approval_unavailable", False)
    state.setdefault("snapshot_complete", True)
    state.setdefault("authorized_changed_files", [])
    state.setdefault("execution_errors", [])
    state.setdefault("last_tool_output", "")
    state.setdefault("last_reflection", "")
    state.setdefault("retry_attempts", 0)
    state.setdefault("tool_call_count", 0)
    state.setdefault("write_count", 0)
    state.setdefault("verification_mode", "")
    state.setdefault("verification_note", "")
    state.setdefault("legacy_verification_note", "")
    state.setdefault("static_check", {"status": "not_run", "checked_files": [], "checks": [], "errors": [], "snapshot_complete": True})
    state.setdefault("test_results", [])
    state.setdefault("change_revision", 0)
    state.setdefault("finish_reason", "")
    state.setdefault("recent_failures", [])
    state.setdefault("conversation_summary", "")
    state.setdefault("turns", [])
    state.setdefault("active_turn_id", "")
    state.setdefault("last_user_request", state.get("task", ""))
    state.setdefault("turn_progress", "none")
    state.setdefault("needs_more_action", False)
    state.setdefault("explore_streak", 0)
    state.setdefault("workspace_snapshot", {})
    state.setdefault("failure_type", "")
    state.setdefault("failure_signature", "")
    state.setdefault("root_cause_hypothesis", "")
    state.setdefault("required_next_action", "")
    state.setdefault("target_test", "")
    state.setdefault("diagnostic_budget_used", 0)
    state.setdefault("token_usage", {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "llm_calls": 0})
    if state["status"] == "planning":
        state["status"] = "acting"
        state["workspace_snapshot"] = {}
    if state["status"] in {"diagnosing", "reflecting"}:
        state["status"] = "acting"
    if state["pending_batch"] and state["status"] not in {"failed", "awaiting_human_confirm", "awaiting_context"}:
        state["status"] = "executing"
    return state
