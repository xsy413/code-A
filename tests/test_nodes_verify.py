from __future__ import annotations

from pathlib import Path

from app.graph.nodes import AgentNodes
from app.tools import ToolResult
from tests import FakeLLM, FakeStore, FakeTools, make_settings, make_state, make_workspace


def make_nodes(tmp_path: Path, *, tools: FakeTools | None = None, settings_overrides=None):
    llm = FakeLLM()
    store = FakeStore()
    _tools = tools or FakeTools()
    settings = make_settings(tmp_path, **(settings_overrides or {}))
    return AgentNodes(llm, store, _tools, settings), _tools


def test_verify_without_code_change_goes_to_diagnosing() -> None:
    tmp_path = make_workspace()
    nodes, _ = make_nodes(tmp_path)
    state = make_state(tmp_path, turn_progress="none", write_count=0)

    out = nodes.verify(state)

    assert out["status"] == "diagnosing"
    assert "no_code_change_yet" in out["error"]


def test_verify_auto_no_tests_allowed_finishes() -> None:
    tmp_path = make_workspace()
    nodes, _ = make_nodes(tmp_path, settings_overrides={"allow_finish_without_tests": True, "verify_mode": "auto"})
    state = make_state(tmp_path, turn_progress="modified", write_count=1)

    out = nodes.verify(state)

    assert out["status"] == "finished"
    assert "No test entry found" in out["verification_note"]


def test_verify_auto_no_tests_not_allowed_goes_to_diagnosing() -> None:
    tmp_path = make_workspace()
    nodes, _ = make_nodes(tmp_path, settings_overrides={"allow_finish_without_tests": False, "verify_mode": "auto"})
    state = make_state(tmp_path, turn_progress="modified", write_count=1)

    out = nodes.verify(state)

    assert out["status"] == "diagnosing"
    assert "no_tests_available" in out["error"]


def test_verify_run_tests_success() -> None:
    tmp_path = make_workspace()
    (tmp_path / "tests").mkdir()
    tools = FakeTools([ToolResult(ok=True, stdout="passed")])
    nodes, _ = make_nodes(tmp_path, tools=tools, settings_overrides={"verify_mode": "required"})
    state = make_state(tmp_path, turn_progress="modified", write_count=1)

    out = nodes.verify(state)

    assert out["status"] == "finished"
    assert out["verification_note"] == "Tests passed."
    assert out["tool_calls"][-1]["name"] == "run_tests"


def test_verify_uses_run_test_target_when_target_exists() -> None:
    tmp_path = make_workspace()
    (tmp_path / "tests").mkdir()
    tools = FakeTools([ToolResult(ok=False, stderr="assert failed", exit_code=1)])
    nodes, _ = make_nodes(tmp_path, tools=tools, settings_overrides={"verify_mode": "required"})
    state = make_state(tmp_path, turn_progress="modified", write_count=1, target_test="tests/test_x.py::test_y")

    out = nodes.verify(state)

    assert out["tool_calls"][-1]["name"] == "run_test_target"
    assert out["status"] == "diagnosing"


def test_verify_no_tests_collected_exit_code_5_allowed() -> None:
    tmp_path = make_workspace()
    (tmp_path / "tests").mkdir()
    tools = FakeTools([ToolResult(ok=False, stderr="no tests collected", exit_code=5)])
    nodes, _ = make_nodes(tmp_path, tools=tools, settings_overrides={"verify_mode": "auto", "allow_finish_without_tests": True})
    state = make_state(tmp_path, turn_progress="modified", write_count=1)

    out = nodes.verify(state)

    assert out["status"] == "finished"
    assert "No tests collected" in out["verification_note"]


def test_verify_run_tests_failure_goes_to_diagnosing() -> None:
    tmp_path = make_workspace()
    (tmp_path / "tests").mkdir()
    tools = FakeTools([ToolResult(ok=False, stderr="assert failed", exit_code=1)])
    nodes, _ = make_nodes(tmp_path, tools=tools, settings_overrides={"verify_mode": "required"})
    state = make_state(tmp_path, turn_progress="modified", write_count=1)

    out = nodes.verify(state)

    assert out["status"] == "diagnosing"
    assert out["error"] == "assert failed"


def test_verify_budget_exceeded_marks_failed() -> None:
    tmp_path = make_workspace()
    (tmp_path / "tests").mkdir()
    nodes, _ = make_nodes(tmp_path, settings_overrides={"verify_mode": "required", "max_tool_calls": 0})
    state = make_state(tmp_path, turn_progress="modified", write_count=1)

    out = nodes.verify(state)

    assert out["status"] == "failed"
    assert "MAX_TOOL_CALLS=0" in out["error"]


def test_verify_exception_routes_to_diagnosing() -> None:
    tmp_path = make_workspace()
    (tmp_path / "tests").mkdir()
    tools = FakeTools([RuntimeError("crash")])
    nodes, _ = make_nodes(tmp_path, tools=tools, settings_overrides={"verify_mode": "required"})
    state = make_state(tmp_path, turn_progress="modified", write_count=1)

    out = nodes.verify(state)

    assert out["status"] == "diagnosing"
    assert out["error"].startswith("verify failed:")
