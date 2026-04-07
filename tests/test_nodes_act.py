from __future__ import annotations

from pathlib import Path

from app.graph.nodes import AgentNodes
from app.tools import ToolResult
from tests import FakeLLM, FakeStore, FakeTools, make_settings, make_state, make_workspace


def make_nodes(
    tmp_path: Path,
    *,
    llm: FakeLLM | None = None,
    tools: FakeTools | None = None,
    confirm_fn=None,
    settings_overrides=None,
):
    _llm = llm or FakeLLM()
    _tools = tools or FakeTools()
    store = FakeStore()
    settings = make_settings(tmp_path, **(settings_overrides or {}))
    return AgentNodes(_llm, store, _tools, settings, confirm_fn=confirm_fn), _llm, _tools


def test_act_risky_write_goes_to_human_confirm() -> None:
    tmp_path = make_workspace()
    llm = FakeLLM(json_responses=[{"tool": "write_file", "args": {"path": "pyproject.toml", "content": "x"}}])
    tools = FakeTools(risky_write=True)
    nodes, _, _ = make_nodes(tmp_path, llm=llm, tools=tools)
    state = make_state(tmp_path)

    out = nodes.act(state)

    assert out["status"] == "awaiting_human_confirm"
    assert out["pending_action"]["tool"] == "write_file"


def test_act_finish_validation_error_routes_to_reflecting() -> None:
    tmp_path = make_workspace()
    llm = FakeLLM(json_responses=[{"tool": "finish", "args": {"changed_files": [], "completion_reason": ""}}])
    nodes, _, _ = make_nodes(tmp_path, llm=llm)
    state = make_state(tmp_path)

    out = nodes.act(state)

    assert out["status"] == "reflecting"
    assert out["needs_more_action"] is True
    assert "finish requires args.changed_files" in out["error"]


def test_act_write_file_success_sets_verifying() -> None:
    tmp_path = make_workspace()
    llm = FakeLLM(json_responses=[{"tool": "write_file", "args": {"path": "a.py", "content": "x=1"}}])
    tools = FakeTools([ToolResult(ok=True, stdout="ok")])
    nodes, _, _ = make_nodes(tmp_path, llm=llm, tools=tools, settings_overrides={"auto_confirm_risky_writes": True})
    state = make_state(tmp_path)

    out = nodes.act(state)

    assert out["status"] == "verifying"
    assert out["write_count"] == 1
    assert "a.py" in out["changed_files"]
    assert out["turn_progress"] == "modified"


def test_act_exploration_streak_threshold_triggers_reflect() -> None:
    tmp_path = make_workspace()
    llm = FakeLLM(json_responses=[{"tool": "list_files", "args": {"pattern": "*"}}])
    tools = FakeTools([ToolResult(ok=True, stdout="files")])
    nodes, _, _ = make_nodes(tmp_path, llm=llm, tools=tools, settings_overrides={"max_explore_steps_before_write": 0})
    state = make_state(tmp_path)

    out = nodes.act(state)

    assert out["status"] == "reflecting"
    assert "no_code_change_yet" in out["error"]


def test_act_tool_failure_sets_diagnosing() -> None:
    tmp_path = make_workspace()
    llm = FakeLLM(json_responses=[{"tool": "patch_file", "args": {"path": "a.py", "old_str": "x", "new_str": "y"}}])
    tools = FakeTools([ToolResult(ok=False, stderr="patch failed", exit_code=1)])
    nodes, _, _ = make_nodes(tmp_path, llm=llm, tools=tools)
    state = make_state(tmp_path)

    out = nodes.act(state)

    assert out["status"] == "diagnosing"
    assert out["error"] == "patch failed"


def test_act_blocks_when_required_next_action_not_satisfied() -> None:
    tmp_path = make_workspace()
    llm = FakeLLM(json_responses=[{"tool": "write_file", "args": {"path": "a.py", "content": "x=1"}}])
    nodes, _, _ = make_nodes(tmp_path, llm=llm)
    state = make_state(tmp_path, required_next_action="python_probe")

    out = nodes.act(state)

    assert out["status"] == "reflecting"
    assert "diagnostic_gate" in out["error"]


def test_human_confirm_without_pending_fails() -> None:
    tmp_path = make_workspace()
    nodes, _, _ = make_nodes(tmp_path)
    state = make_state(tmp_path, pending_action={})

    out = nodes.human_confirm(state)

    assert out["status"] == "failed"
    assert out["error"] == "No pending action for human_confirm."


def test_human_confirm_declined_routes_to_reflecting() -> None:
    tmp_path = make_workspace()
    nodes, _, _ = make_nodes(tmp_path, confirm_fn=lambda _tool, _args: False)
    state = make_state(
        tmp_path,
        pending_action={"tool": "write_file", "args": {"path": "a.py", "content": "x"}},
    )

    out = nodes.human_confirm(state)

    assert out["status"] == "reflecting"
    assert out["pending_action"] == {}
    assert out["needs_more_action"] is True


def test_human_confirm_approved_write_file_goes_to_verify() -> None:
    tmp_path = make_workspace()
    tools = FakeTools([ToolResult(ok=True, stdout="written")])
    nodes, _, _ = make_nodes(tmp_path, tools=tools, confirm_fn=lambda _tool, _args: True)
    state = make_state(
        tmp_path,
        pending_action={"tool": "write_file", "args": {"path": "a.py", "content": "x"}},
    )

    out = nodes.human_confirm(state)

    assert out["status"] == "verifying"
    assert out["write_count"] == 1
    assert "a.py" in out["changed_files"]


def test_human_confirm_approved_but_tool_fails() -> None:
    tmp_path = make_workspace()
    tools = FakeTools([ToolResult(ok=False, stderr="boom", exit_code=2)])
    nodes, _, _ = make_nodes(tmp_path, tools=tools, confirm_fn=lambda _tool, _args: True)
    state = make_state(
        tmp_path,
        pending_action={"tool": "write_file", "args": {"path": "a.py", "content": "x"}},
    )

    out = nodes.human_confirm(state)

    assert out["status"] == "diagnosing"
    assert out["error"] == "boom"
