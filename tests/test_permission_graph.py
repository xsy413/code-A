from __future__ import annotations

import json
from pathlib import Path

from app.llm import ActionResponse
from app.permissions import PermissionEngine
from app.tools import ToolInput, ToolResult
from tests import make_workspace, tool_response
from tests.test_agent_turns import make_agent


def test_noninteractive_pause_and_resume_executes_once_with_native_pair():
    workspace = make_workspace()
    agent, llm, tools = make_agent(workspace, [
        tool_response("write_file", {"path": "a.py", "content": "value = 1\n"}, call_id="write"),
        ActionResponse("Done exactly.", (), "stop"),
    ])
    agent.nodes.confirm_fn = None
    first = agent.run("Create a file", workspace)
    assert first["status"] == "awaiting_human_confirm"
    assert first["summary"] == ""
    assert first["pending_action"]["origin"] == "act"
    assert first["tool_call_count"] == 1
    assert tools.executed == []
    assert not (workspace / "a.py").exists()
    agent.nodes.confirm_fn = lambda *_: "approve_session"
    final = agent.resume(first["session_id"])
    assert final["summary"] == "Done exactly."
    assert final["tool_call_count"] == 1
    assert len(tools.executed) == 1
    history = llm.action_calls[-1]["history"]
    history = [m for m in history if m["role"] in {"assistant", "tool"}]
    assert history[0]["tool_calls"][0]["id"] == history[1]["tool_call_id"] == "write"


def test_model_selected_test_pause_resumes_without_duplicate_tests():
    workspace = make_workspace()
    (workspace / "tests").mkdir()
    agent, llm, tools = make_agent(workspace, [
        tool_response("write_file", {"path": "a.py", "content": "value = 1\n"}),
        ActionResponse("Tests passed.", (), "stop"),
    ])
    shell, args = tools.test_action()
    llm.action_responses.insert(1, tool_response(shell, args, call_id="chosen-test"))
    answers = iter(["approve_session", "unavailable"])
    agent.nodes.confirm_fn = lambda *_: next(answers)
    state = agent.run("Implement", workspace)
    assert state["status"] == "awaiting_human_confirm"
    assert state["pending_action"]["origin"] == "act"
    assert len(tools.executed) == 1
    agent.nodes.confirm_fn = lambda *_: "approve_once"
    result = agent.resume(state["session_id"])
    assert result["summary"] == "Tests passed."
    assert len(tools.executed) == 2
    assert result["tool_call_count"] == 2
    assert result["test_results"][-1]["status"] == "passed"
    assert llm.action_calls[-1]["history"][-1]["role"] == "tool"


def test_verification_rejection_keeps_failure_and_does_not_diagnose_permissions():
    workspace = make_workspace()
    (workspace / "tests").mkdir()
    agent, llm, tools = make_agent(workspace, [
        tool_response("write_file", {"path": "a.py", "content": "value = 1\n"}),
        ActionResponse("Testing was declined; verification remains incomplete.", (), "stop"),
    ])
    shell, args = tools.test_action()
    llm.action_responses.insert(1, tool_response(shell, args, call_id="declined-test"))
    agent.nodes.confirm_fn = lambda name, _: "approve_once" if name == "write_file" else "reject"
    result = agent.run("Implement", workspace)
    assert result["status"] == "finished"
    assert result["test_results"][-1]["status"] == "not_run"
    assert result["static_check"]["status"] == "passed"
    assert result["diagnostic_budget_used"] == 0
    assert len(tools.executed) == 1
    assert result["tool_calls"][-1]["execution_status"] == "rejected"


def test_current_process_file_grant_spans_turns_but_not_new_session():
    workspace = make_workspace()
    agent, llm, tools = make_agent(workspace, [
        tool_response("write_file", {"path": "a.py", "content": "a=1"}), ActionResponse("First.", (), "stop"),
        tool_response("write_file", {"path": "b.py", "content": "b=2"}), ActionResponse("Second.", (), "stop"),
        tool_response("write_file", {"path": "c.py", "content": "c=3"}), ActionResponse("Third.", (), "stop"),
    ])
    confirmations = []
    def confirm(name, args):
        confirmations.append(name)
        return "approve_session"
    agent.nodes.confirm_fn = confirm
    first = agent.run("First", workspace)
    second = agent.run_turn(first["session_id"], "Second", workspace)
    assert second["summary"] == "Second."
    assert len(confirmations) == 1
    agent.run("Third", workspace)
    assert len(confirmations) == 2


def test_plain_answer_after_deny_has_a_paired_result():
    workspace = make_workspace()
    agent, llm, tools = make_agent(workspace, [
        tool_response("read_file", {"path": ".env"}, call_id="secret"),
        ActionResponse("Credential access was blocked.", (), "stop"),
    ])
    result = agent.run("Read config", workspace)
    assert result["summary"] == "Credential access was blocked."
    assert result["diagnostic_budget_used"] == 0
    history = llm.action_calls[-1]["history"]
    history = [m for m in history if m["role"] in {"assistant", "tool"}]
    assert history[1]["tool_call_id"] == "secret"
    assert json.loads(history[1]["content"])["execution_status"] == "denied"
    assert agent.store.get_permission_events(result["session_id"])


def test_legacy_pending_command_is_reapproved_not_auto_executed():
    workspace = make_workspace()
    agent, llm, tools = make_agent(workspace, [ActionResponse("Stopped.", (), "stop")])
    from tests import make_state
    state = make_state(workspace, status="awaiting_human_confirm", pending_action={
        "tool": "run_command", "args": {"command": "python -c 'print(1)'"},
    })
    agent.store.upsert_state("s1", state)
    agent.nodes.confirm_fn = None
    result = agent.resume("s1")
    assert result["status"] == "awaiting_human_confirm"
    assert not tools.executed
    agent.nodes.confirm_fn = lambda *_: "reject"
    result = agent.resume("s1")
    assert result["summary"] == "Stopped."
    assert result["tool_calls"][0]["execution_status"] == "rejected"


def test_failed_shell_changes_are_verified_and_failure_is_preserved():
    workspace = make_workspace()
    agent, llm, tools = make_agent(workspace, [
        tool_response("powershell", {"command": "Write-Output placeholder"}),
        ActionResponse("Changes are valid but the original command failed.", (), "stop"),
    ])
    def partial_write(action, command, timeout):
        (workspace / "partial.py").write_text("value = 1\n")
        return ToolResult(False, stderr="original shell failure", exit_code=2)
    tools._run_shell = partial_write
    result = agent.run("Change file", workspace)
    assert result["changed_files"] == ["partial.py"]
    assert result["error"] == "original shell failure"
    assert "static checks passed" in result["verification_note"].lower()
    assert any(e.node_name == "verify" for e in agent.store.get_events(result["session_id"]))
