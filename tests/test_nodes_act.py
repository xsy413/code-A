from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.graph.nodes import AgentNodes
from app.llm import ActionResponse
from app.tools import ToolResult
from tests import act_and_execute, FakeLLM, FakeStore, FakeTools, make_settings, make_state, make_workspace, set_usage, tool_response


def make_nodes(tmp_path: Path, *, llm=None, tools=None, confirm_fn=None, settings_overrides=None):
    llm = llm or FakeLLM()
    tools = tools or FakeTools()
    store = FakeStore()
    settings = make_settings(tmp_path, **(settings_overrides or {}))
    return AgentNodes(llm, store, tools, settings, confirm_fn=confirm_fn), llm, tools


def assert_native_result(nodes, state, call_id: str, *, ok: bool) -> None:
    history = nodes._build_action_history(state)
    assert history is not None
    assert history[-2]["tool_calls"][0]["id"] == call_id
    assert history[-1]["role"] == "tool"
    assert history[-1]["tool_call_id"] == call_id
    assert json.loads(history[-1]["content"])["ok"] is ok


def test_act_final_text_finishes_without_tools_or_file_changes() -> None:
    tmp_path = make_workspace()
    answer = "Explanation.\n\nExact final text.  "
    llm = FakeLLM(action_responses=[ActionResponse(answer, (), "stop")])
    set_usage(llm, prompt=8, completion=5)
    nodes, _, tools = make_nodes(tmp_path, llm=llm, settings_overrides={"max_tool_calls": 0})
    state = make_state(tmp_path)

    out = nodes.finish(act_and_execute(nodes, state))

    assert out["status"] == "finished"
    assert out["summary"] == answer
    assert out["finish_reason"] == "assistant_response"
    assert out["needs_more_action"] is False
    assert out["tool_call_count"] == 0
    assert out["tool_calls"] == []
    assert out["changed_files"] == []
    assert out["token_usage"]["total_tokens"] == 13
    assert tools.executed == []
    assert nodes.store.tool_calls == []
    assert llm.text_calls == []
    assert llm.json_calls == []


def test_action_prompt_omits_plan_and_empty_development_context():
    tmp_path = make_workspace()
    nodes, llm, _ = make_nodes(tmp_path)
    state = make_state(tmp_path, plan="Legacy seven-step implementation plan")

    act_and_execute(nodes, state)

    prompt = llm.action_calls[0]["user_prompt"]
    assert f"Workspace: {tmp_path}" in prompt
    assert "Legacy seven-step" not in prompt
    for label in ("Plan:", "Workspace observations:", "Required next action from diagnosis:",
                  "Diagnosed failure type:", "Recent reflection:", "Verification note:", "Unresolved error:"):
        assert label not in prompt
    assert "queries need results, not an implementation report" in prompt


def test_act_final_text_can_explain_unresolved_failure() -> None:
    tmp_path = make_workspace()
    llm = FakeLLM(action_responses=[ActionResponse("Tests still fail; here is the evidence.", (), "stop")])
    nodes, _, _ = make_nodes(tmp_path, llm=llm)
    state = make_state(tmp_path, error="assert failed", verification_note="Tests failed.",
                       required_next_action="run_test_target")

    out = act_and_execute(nodes, state)

    assert out["status"] == "finished"
    assert out["error"] == "assert failed"
    assert out["verification_note"] == "Tests failed."
    assert "assert failed" in llm.action_calls[0]["user_prompt"]


def test_act_text_accompanying_tool_call_does_not_finish() -> None:
    tmp_path = make_workspace()
    llm = FakeLLM(action_responses=[tool_response("read_file", {"path": "a.py"},
                                                content="I will inspect this.", call_id="call_read")])
    nodes, _, tools = make_nodes(tmp_path, llm=llm)
    state = make_state(tmp_path)

    out = act_and_execute(nodes, state)

    assert out["status"] == "acting"
    assert out["summary"] == ""
    assert tools.executed[0].name == "read_file"
    assert_native_result(nodes, state, "call_read", ok=True)


@pytest.mark.parametrize("response", [
    ActionResponse("", (), "stop"),
    ActionResponse("   ", (), "stop"),
    ActionResponse("Incomplete", (), "length"),
    ActionResponse("Filtered", (), "content_filter"),
    ActionResponse("", (), "tool_calls"),
    ActionResponse("text", tool_response("read_file", {"path": "a.py"}).tool_calls, "length"),
    RuntimeError("connection failed"),
])
def test_act_abnormal_response_does_not_finish_or_execute(response) -> None:
    tmp_path = make_workspace()
    nodes, _, tools = make_nodes(tmp_path, llm=FakeLLM(action_responses=[response]))

    out = act_and_execute(nodes, make_state(tmp_path))

    assert out["status"] == "acting"
    assert out["needs_more_action"] is True
    assert out["summary"] == ""
    assert tools.executed == []


@pytest.mark.parametrize("name,args", [
    ("write_file", {"content": "x=1"}),
    ("delete_file", {"path": 123}),
    ("read_file", {"path": "a.py", "line_start": True}),
    ("run_command", {"command": "python -V", "unexpected": True}),
    ("finish", {}),
])
def test_act_invalid_arguments_return_tool_error_without_execution(name, args) -> None:
    tmp_path = make_workspace()
    nodes, _, tools = make_nodes(tmp_path, llm=FakeLLM(action_responses=[tool_response(name, args, call_id="bad")]))
    state = make_state(tmp_path)

    out = act_and_execute(nodes, state)

    assert out["status"] == "acting"
    assert out["tool_calls"][-1]["error_kind"] == "invalid_tool_args"
    assert tools.executed == []
    assert state["tool_call_count"] == 1
    assert_native_result(nodes, state, "bad", ok=False)


def test_act_write_file_success_sets_verifying() -> None:
    tmp_path = make_workspace()
    llm = FakeLLM(action_responses=[tool_response("write_file", {"path": "a.py", "content": "x=1"})])
    nodes, _, _ = make_nodes(tmp_path, llm=llm)

    out = act_and_execute(nodes, make_state(tmp_path, deleted_files=["a.py"]))

    assert out["status"] == "verifying"
    assert out["write_count"] == 1
    assert out["changed_files"] == ["a.py"]
    assert out["deleted_files"] == []
    assert out["turn_progress"] == "modified"


def test_act_read_only_exploration_can_finish_after_old_threshold() -> None:
    tmp_path = make_workspace()
    llm = FakeLLM(action_responses=[
        tool_response("list_files", {}), tool_response("read_file", {"path": "a.py"}),
        ActionResponse("Here is the architecture.", (), "stop"),
    ])
    nodes, _, tools = make_nodes(tmp_path, llm=llm, settings_overrides={"max_explore_steps_before_write": 0})
    state = make_state(tmp_path)

    assert act_and_execute(nodes, state)["status"] == "acting"
    assert act_and_execute(nodes, state)["status"] == "acting"
    assert act_and_execute(nodes, state)["status"] == "finished"
    assert state["write_count"] == 0
    assert len(tools.executed) == 2
    assert [message["role"] for message in llm.action_calls[-1]["history"] if message["role"] in {"assistant", "tool"}] == ["assistant", "tool"] * 2


@pytest.mark.parametrize("failure", [ToolResult(ok=False, stderr="patch failed", exit_code=1), RuntimeError("crash")])
def test_tool_failure_returns_to_model_and_preserves_native_pair(failure) -> None:
    tmp_path = make_workspace()
    llm = FakeLLM(action_responses=[tool_response("patch_file", {"path": "a.py", "old_str": "x", "new_str": "y"},
                                                call_id="patch")])
    nodes, _, _ = make_nodes(tmp_path, llm=llm, tools=FakeTools([failure]))
    state = make_state(tmp_path)

    assert act_and_execute(nodes, state)["status"] == "acting"
    assert_native_result(nodes, state, "patch", ok=False)


def test_legacy_diagnostic_gate_does_not_block_execution() -> None:
    tmp_path = make_workspace()
    llm = FakeLLM(action_responses=[tool_response("write_file", {"path": "a.py", "content": "x=1"}, call_id="blocked")])
    nodes, _, tools = make_nodes(tmp_path, llm=llm)
    state = make_state(tmp_path, required_next_action="python_probe")

    assert act_and_execute(nodes, state)["status"] == "verifying"
    assert state["error"] == ""
    assert len(tools.executed) == 1
    assert_native_result(nodes, state, "blocked", ok=True)


def test_act_tool_budget_stops_execution_and_keeps_native_pair() -> None:
    tmp_path = make_workspace()
    llm = FakeLLM(action_responses=[tool_response("read_file", {"path": "a.py"}, call_id="budget")])
    nodes, _, tools = make_nodes(tmp_path, llm=llm, settings_overrides={"max_tool_calls": 0})
    state = make_state(tmp_path)

    assert act_and_execute(nodes, state)["status"] == "failed"
    assert tools.executed == []
    assert_native_result(nodes, state, "budget", ok=False)


def test_human_confirm_without_pending_fails() -> None:
    tmp_path = make_workspace()
    nodes, _, _ = make_nodes(tmp_path)
    out = nodes.human_confirm(make_state(tmp_path))
    assert out["status"] == "acting"
    assert out["error"] == "No pending action for human_confirm."


@pytest.mark.parametrize("approved", [True, False])
def test_risky_write_confirmation_preserves_tool_id_and_result(approved: bool) -> None:
    tmp_path = make_workspace()
    llm = FakeLLM(action_responses=[tool_response("write_file", {"path": "pyproject.toml", "content": "x"},
                                                call_id="confirm")])
    nodes, _, tools = make_nodes(tmp_path, llm=llm, tools=FakeTools(risky_write=True),
                                 confirm_fn=lambda name, args: approved)
    state = make_state(tmp_path)

    assert act_and_execute(nodes, state)["status"] == "awaiting_human_confirm"
    assert state["pending_action"]["tool_call_id"] == "confirm"
    assert tools.executed == []
    assert nodes.human_confirm(state)["status"] == ("verifying" if approved else "acting")
    assert state["pending_action"] == {}
    assert len(tools.executed) == int(approved)
    assert_native_result(nodes, state, "confirm", ok=approved)


def test_human_confirm_legacy_pending_write_still_executes() -> None:
    tmp_path = make_workspace()
    nodes, _, tools = make_nodes(tmp_path, confirm_fn=lambda name, args: True)
    state = make_state(tmp_path, pending_action={"tool": "write_file", "args": {"path": "a.py", "content": "x=1"}})

    assert nodes.human_confirm(state)["status"] == "verifying"
    assert tools.executed[0].name == "write_file"
    assert nodes._build_action_history(state)[0]["role"] == "user"
