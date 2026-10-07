from __future__ import annotations

import json

import pytest

from app.graph.nodes import AgentNodes
from tests import FakeLLM, FakeStore, FakeTools, make_settings, make_state, make_workspace


def make_nodes(workspace, **settings):
    (workspace / "a.py").write_text("value = 1\n")
    llm, store, tools = FakeLLM(), FakeStore(), FakeTools()
    return AgentNodes(llm, store, tools, make_settings(workspace, **settings)), llm, tools


@pytest.mark.parametrize("mode,allowed", [("auto", True), ("required", False), ("required", True)])
def test_static_check_never_runs_tests_or_requires_them(mode, allowed):
    workspace = make_workspace()
    (workspace / "tests").mkdir()
    nodes, llm, tools = make_nodes(workspace, verify_mode=mode, allow_finish_without_tests=allowed, max_tool_calls=0)
    state = make_state(workspace, status="verifying", changed_files=["a.py"], error="Previous execution failed")
    nodes.verify(state)
    assert state["status"] == "acting"
    assert state["static_check"]["status"] == "passed"
    assert state["error"] == "Previous execution failed"
    assert not tools.executed
    assert state["tool_call_count"] == state["retry_attempts"] == 0
    assert not state["pending_batch"]
    nodes.act(state)
    assert '"status": "passed"' in llm.action_calls[-1]["user_prompt"]
    assert json.loads(llm.action_calls[-1]["history"][-1]["content"].split(": ", 1)[1])["kind"] == "static_check"


@pytest.mark.parametrize("status", ["passed", "failed", "incomplete"])
def test_every_static_outcome_reaches_next_act_and_survives_history_clipping(status):
    workspace = make_workspace()
    nodes, llm, tools = make_nodes(workspace)
    if status == "failed":
        (workspace / "a.py").write_text("def broken(:\n")
    state = make_state(workspace, status="verifying", changed_files=["a.py"], snapshot_complete=status != "incomplete")
    nodes.verify(state)
    assert state["static_check"]["status"] == status
    assert state["status"] == "acting"
    observation = json.loads(state["action_history"][-1]["content"])
    assert observation["status"] == status
    for i in range(7):
        nodes._append_observation(state, {"kind": "later", "index": i})
    nodes.act(state)
    assert f'"status": "{status}"' in llm.action_calls[-1]["user_prompt"]
    assert "Latest static check (not a test result)" in llm.action_calls[-1]["user_prompt"]
    assert len(state["action_history"]) == 8
    assert state["retry_attempts"] == (1 if status == "failed" else 0)
    assert tools.executed == []


def test_failed_static_check_reports_all_files_and_counts_once_for_same_revision():
    workspace = make_workspace()
    nodes, _, tools = make_nodes(workspace)
    (workspace / "a.py").write_text("def bad(:\n")
    state = make_state(workspace, changed_files=["a.py", "missing.py"])
    nodes.verify(state)
    assert state["static_check"]["status"] == "failed"
    assert len(state["static_check"]["errors"]) == 2
    assert state["retry_attempts"] == 1
    nodes.verify(state)
    assert state["retry_attempts"] == 1
    assert tools.executed == []


def test_deleted_file_records_are_checked_without_importing_code():
    workspace = make_workspace()
    nodes, _, tools = make_nodes(workspace)
    (workspace / "test_side_effect.py").write_text("from pathlib import Path\nPath('side_effect.txt').write_text('bad')\n")
    state = make_state(workspace, changed_files=["test_side_effect.py", "removed.py"], deleted_files=["removed.py"])
    nodes.verify(state)
    assert state["static_check"]["status"] == "passed"
    assert not (workspace / "side_effect.txt").exists()
    assert "deletion_record" in state["static_check"]["checks"][1]["checks"]
    assert tools.executed == []
    (workspace / "removed.py").write_text("still here")
    nodes.verify(state)
    assert "deleted file still exists" in state["static_check"]["errors"][0]


def test_no_changed_files_returns_static_error_to_model():
    workspace = make_workspace()
    nodes, _, _ = make_nodes(workspace)
    state = make_state(workspace)
    nodes.verify(state)
    assert state["status"] == "acting"
    assert "requires recorded changed_files" in state["static_check"]["errors"][0]


def test_stop_condition_checks_changes_without_model_or_tests():
    workspace = make_workspace()
    nodes, llm, tools = make_nodes(workspace)
    state = make_state(workspace, changed_files=["a.py"], stop_reason="Budget exceeded", error="Original execution failure")
    nodes.verify(state)
    assert state["status"] == "failed"
    assert state["static_check"]["status"] == "passed"
    assert state["error"] == "Original execution failure"
    assert llm.action_calls == tools.executed == []


def test_static_check_exception_is_observed():
    workspace = make_workspace()
    nodes, llm, _ = make_nodes(workspace)
    def fail(*_):
        raise OSError("Unable to inspect")
    nodes._collect_static_check = fail
    state = make_state(workspace, changed_files=["a.py"])
    nodes.verify(state)
    nodes.act(state)
    assert state["static_check"]["status"] == "failed"
    assert "Unable to inspect" in llm.action_calls[-1]["user_prompt"]
