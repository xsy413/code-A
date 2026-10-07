from __future__ import annotations

import json
import warnings

import pytest

from app.cli import _print_result
from app.config import Settings
from app.permissions import PermissionEngine
from app.graph.state import ensure_state_defaults
from app.tools import ToolResult
from tests import make_state, make_workspace
from tests.test_agent_turns import make_agent
from tests.test_tool_batches import batch, final, write


def chosen_test(tools, command="pytest -q tests/test_selected.py"):
    return tools.default_shell, {"command": command}


def test_same_named_non_test_is_not_imported_and_test_reference_is_not_executed():
    workspace = make_workspace()
    content = "from pathlib import Path\nPath('imported.txt').write_text('bad')\n"
    agent, llm, tools = make_agent(workspace, [batch(write("tests/test_selected.py", content)), final("Not tested.")],
                                   verify_mode="required", allow_finish_without_tests=False,
                                   test_command="python -c \"open('ran.txt', 'w').write('bad')\"")
    state = agent.run("Create a file; do not run tests", workspace)
    assert state["static_check"]["status"] == "passed"
    assert state["test_results"] == []
    assert not (workspace / "imported.txt").exists()
    assert not (workspace / "ran.txt").exists()
    assert [call.name for call in tools.executed] == ["write_file"]
    assert "Test command reference (NOT automatically executed)" in llm.action_calls[-1]["user_prompt"]
    assert "not in the same batch" in llm.action_calls[-1]["user_prompt"]


def test_selected_test_runs_once_and_later_change_marks_it_stale_in_history_and_context():
    workspace = make_workspace()
    agent, llm, tools = make_agent(workspace, [])
    llm.action_responses = [batch(write("a.py")), batch(chosen_test(tools)), batch(write("README.txt", "notes")), final()]
    state = agent.run("Implement, test, then update notes", workspace)
    result = state["test_results"][0]
    assert result["status"] == "passed"
    assert result["freshness"] == "stale"
    assert result["scope"]["targets"] == ["tests/test_selected.py"]
    assert result["cwd"] == str(workspace.resolve())
    assert result["executables"]
    assert state["change_revision"] == 2
    assert sum(call.name == tools.default_shell for call in tools.executed) == 1
    assert '"freshness": "stale"' in llm.action_calls[-1]["user_prompt"]
    histories = [json.loads(m["content"]) for m in llm.action_calls[-1]["history"] if m["role"] == "tool"]
    assert next(r["test_result"] for r in histories if r["test_result"])["freshness"] == "stale"


def test_pause_after_test_then_write_resume_updates_pending_history_freshness():
    workspace = make_workspace()
    agent, llm, tools = make_agent(workspace, [])
    llm.action_responses = [batch(chosen_test(tools), write("a.py")), final()]
    answers = iter(["approve_once", "unavailable"])
    agent.nodes.confirm_fn = lambda *_: next(answers)
    paused = agent.run("Test then edit", workspace)
    assert paused["test_results"][0]["freshness"] == "current"
    assert paused["pending_batch"]["index"] == 1
    agent.nodes.confirm_fn = lambda *_: "approve_once"
    state = agent.resume(paused["session_id"])
    assert state["test_results"][0]["freshness"] == "stale"
    history = llm.action_calls[-1]["history"]
    assert json.loads(next(m for m in history if m["role"] == "tool")["content"])["test_result"]["freshness"] == "stale"
    assert len(tools.executed) == state["tool_call_count"] == 2


def test_test_generated_files_are_checked_without_recursive_test():
    workspace = make_workspace()
    agent, llm, tools = make_agent(workspace, [])
    llm.action_responses = [batch(chosen_test(tools)), final()]
    def generates(*_):
        (workspace / "generated.py").write_text("value = 1\n")
        return ToolResult(True, stdout="1 passed")
    tools._run_shell = generates
    state = agent.run("Run selected tests", workspace)
    assert len(tools.executed) == 1
    assert state["static_check"]["status"] == "passed"
    assert state["static_check"]["checked_files"] == ["generated.py"]
    assert state["test_results"][0]["freshness"] == "stale"
    assert state["retry_attempts"] == 0


@pytest.mark.parametrize("result,status", [
    (ToolResult(False, stderr="No module named pytest", exit_code=1), "failed"),
    (ToolResult(False, stdout="arbitrary wording", exit_code=5), "no_tests"),
    (ToolResult(False, stderr="Timeout", exit_code=124, error_kind="timeout"), "interrupted"),
    (ToolResult(False, stderr="Cancelled", exit_code=1, error_kind="cancelled"), "interrupted"),
])
def test_failed_test_retains_fact_and_allows_next_read_or_final_answer(result, status):
    workspace = make_workspace()
    (workspace / "a.py").write_text("value = 1\n")
    agent, llm, tools = make_agent(workspace, [], test_results=[result])
    llm.action_responses = [batch(chosen_test(tools)), batch(("read_file", {"path": "a.py"})), final("Explained limitation.")]
    state = agent.run("Check implementation", workspace)
    assert state["status"] == "finished"
    assert state["test_results"][0]["status"] == status
    assert state["retry_attempts"] == 1
    assert tools.executed[-1].name == "read_file"
    assert state["required_next_action"] == ""
    assert '"status": "' + status + '"' in llm.action_calls[-1]["user_prompt"]


def test_success_after_failed_test_preserves_both_evidence_records():
    workspace = make_workspace()
    agent, llm, tools = make_agent(workspace, [], test_results=[
        ToolResult(False, stderr="missing dependency", exit_code=1), ToolResult(True, stdout="1 passed"),
    ])
    llm.action_responses = [batch(chosen_test(tools)), batch(chosen_test(tools)), final()]
    state = agent.run("Check twice", workspace)
    assert [r["status"] for r in state["test_results"]] == ["failed", "passed"]
    assert state["error"] == "missing dependency"
    assert state["execution_errors"] == ["missing dependency"]
    assert state["retry_attempts"] == 1


@pytest.mark.parametrize("command,status", [("Write-Output hello", None), ("npm test", "unknown"),
                                            ("python custom.py", "unknown"), ("pytest --collect-only", None),
                                            ("pytest || true", "unknown")])
def test_command_success_is_not_an_unearned_test_pass(command, status):
    workspace = make_workspace()
    agent, llm, tools = make_agent(workspace, [])
    llm.action_responses = [batch(chosen_test(tools, command)), final()]
    tools._run_shell = lambda *_: ToolResult(True, stdout="all tests passed")
    state = agent.run("Run command", workspace)
    assert len(state["test_results"]) == (0 if status is None else 1)
    if status:
        assert state["test_results"][0]["status"] == status
        assert state["test_results"][0]["scope"]["kind"] == "unknown"


def test_rejected_denied_and_skipped_test_attempts_are_recorded_without_extra_failure_counts():
    for reason in ("rejected", "denied", "skipped", "budget"):
        workspace = make_workspace()
        agent, llm, tools = make_agent(workspace, [], max_tool_calls=0 if reason == "budget" else 20)
        call = chosen_test(tools)
        llm.action_responses = [batch(*([("read_file", {"path": "missing"})] if reason == "skipped" else []), call), final()]
        if reason == "rejected":
            agent.nodes.confirm_fn = lambda *_: "reject"
        if reason == "denied":
            policy = workspace / ".user" / "permissions.json"
            policy.parent.mkdir()
            policy.write_text(json.dumps({"version": 1, "rules": [
                {"id": "no-tests", "tool": tools.default_shell, "argv_prefix": ["pytest"], "decision": "deny"},
            ]}))
            tools.permissions.config_path = policy
        state = agent.run("Run test", workspace)
        assert state["test_results"][0]["status"] == "not_run"
        assert state["retry_attempts"] == 1
        assert state["test_results"][0]["stderr"]
        assert all(tool.name != tools.default_shell for tool in tools.executed)


def legacy_queue(agent, workspace, phase, count=1):
    name, args = chosen_test(agent.tools)
    call = {"tool": name, "args": args, "state": phase}
    record = {"name": name, "args": args, "ok": True, "stdout": "old evidence: 7 passed", "stderr": "", "exit_code": 0,
              "execution_status": "executed", "is_test": True, "origin": "verify"}
    state = make_state(workspace, status="executing", tool_call_count=count,
                       pending_batch={"id": "old-batch", "origin": "verify", "assistant_message": None,
                                      "calls": [call], "index": 1 if phase == "completed" else 0,
                                      "results": [record] if phase == "completed" else [], "changed": False, "baseline": None})
    if phase == "completed":
        state["tool_calls"] = [record]
    return state


@pytest.mark.parametrize("phase", ["pending", "awaiting_approval", "running", "completed"])
def test_legacy_auto_test_queue_is_closed_without_execution_or_approval(phase):
    workspace = make_workspace()
    agent, llm, tools = make_agent(workspace, [final()])
    state = legacy_queue(agent, workspace, phase)
    if phase == "awaiting_approval":
        state["status"] = "awaiting_human_confirm"
        state["pending_action"] = {**state["pending_batch"]["calls"][0], "origin": "verify"}
    agent.store.upsert_state("s1", state)
    agent.nodes.confirm_fn = lambda *_: pytest.fail("Legacy automatic tests must not request approval")
    result = agent.resume("s1")
    assert result["pending_batch"] == result["pending_action"] == {}
    assert result["tool_call_count"] == 1
    assert tools.executed == []
    assert result["retry_attempts"] == (1 if phase == "running" else 0)
    expected = "interrupted" if phase == "running" else "passed" if phase == "completed" else "not_run"
    assert result["test_results"][0]["status"] == expected
    assert result["test_results"][0]["freshness"] == "unknown"
    if phase == "completed":
        assert "old evidence" in result["test_results"][0]["stdout"]
    elif phase != "running":
        assert result["tool_calls"][0]["error_kind"] == "auto_test_cancelled"


def test_legacy_queue_closes_before_exhausted_budget_and_checks_existing_changes():
    workspace = make_workspace()
    (workspace / "a.py").write_text("value = 1\n")
    agent, llm, tools = make_agent(workspace, [], max_tool_calls=1)
    state = legacy_queue(agent, workspace, "pending")
    state.update(status="failed", changed_files=["a.py"], stop_reason="Reached old budget", summary="")
    agent.store.upsert_state("s1", state)
    result = agent.resume("s1")
    assert result["status"] == "failed"
    assert result["static_check"]["status"] == "passed"
    assert result["pending_batch"] == {}
    assert result["tool_call_count"] == 1
    assert result["retry_attempts"] == 0
    assert llm.action_calls == tools.executed == []


def test_legacy_single_pending_auto_test_does_not_reserve_budget_or_ask():
    workspace = make_workspace()
    agent, llm, tools = make_agent(workspace, [final()])
    state = make_state(workspace, status="awaiting_human_confirm", tool_call_count=2,
                       pending_action={"tool": "run_tests", "args": {}, "origin": "verify"})
    agent.store.upsert_state("s1", state)
    agent.nodes.confirm_fn = None
    result = agent.resume("s1")
    assert result["tool_call_count"] == 2
    assert result["retry_attempts"] == 0
    assert result["test_results"][0]["status"] == "not_run"
    assert tools.executed == []


def test_legacy_queue_closes_before_exhausted_failure_budget_without_another_model_call():
    workspace = make_workspace()
    agent, llm, tools = make_agent(workspace, [], max_retry_steps=2)
    state = legacy_queue(agent, workspace, "pending")
    state["retry_attempts"] = 2
    agent.store.upsert_state("s1", state)
    result = agent.resume("s1")
    assert result["status"] == "failed"
    assert result["pending_batch"] == {}
    assert result["retry_attempts"] == 2
    assert "MAX_RETRY_STEPS=2" in result["summary"]
    assert llm.action_calls == tools.executed == []


def test_old_text_evidence_is_not_migrated_to_current_test_pass():
    workspace = make_workspace()
    state = {"status": "acting", "verification_note": "Tests passed."}
    ensure_state_defaults(state, "s1", str(workspace))
    assert state["legacy_verification_note"] == "Tests passed."
    assert state["verification_note"] == ""
    assert state["test_results"] == []
    assert state["static_check"]["status"] == "not_run"


def test_deprecated_settings_warn_once_when_explicit_and_keep_command_reference(monkeypatch):
    import app.config as config
    workspace = make_workspace()
    monkeypatch.setattr(config, "_DEPRECATED_WARNED", set())
    monkeypatch.setenv("VERIFY_MODE", "required")
    monkeypatch.setenv("ALLOW_FINISH_WITHOUT_TESTS", "false")
    monkeypatch.setenv("TEST_COMMAND", "pytest tests/test_selected.py")
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        first = Settings.from_env(workspace)
        Settings.from_env(workspace)
    assert len(captured) == 2
    assert all("deprecated and ignored" in str(w.message) for w in captured)
    assert first.verify_mode == "required"
    assert first.allow_finish_without_tests is False
    assert first.test_command == "pytest tests/test_selected.py"


@pytest.mark.parametrize("shell", ["bash", "powershell"])
def test_installed_backend_ast_distinguishes_direct_tests_from_conditional_execution(shell):
    workspace = make_workspace()
    engine = PermissionEngine(workspace)
    if shell not in engine.shells:
        pytest.skip("Backend absent")
    from app.tools.testing import describe_test
    direct = engine._analyze(shell, "pytest tests/test_selected.py -k small")
    assert describe_test(direct, "pytest", str(workspace), [engine.shells[shell]])["direct"]
    conditional = "if ($false) { pytest tests/test_selected.py }" if shell == "powershell" else "if false; then pytest tests/test_selected.py; fi"
    analysis = engine._analyze(shell, conditional)
    info = describe_test(analysis, conditional, str(workspace), [])
    assert not info["direct"]
    assert info["scope"]["kind"] == "unknown"


@pytest.mark.parametrize("origin", ["act", "verify"])
def test_running_test_recovery_records_partial_changes_and_never_replays(origin):
    workspace = make_workspace()
    agent, llm, tools = make_agent(workspace, [final()])
    state = make_state(workspace, tool_call_count=1)
    name, args = chosen_test(tools)
    response = batch((name, args))
    agent.nodes._accept_batch(state, [{"tool": name, "args": args, "tool_call_id": "old"}], origin,
                              response.to_message() if origin == "act" else None, reserved=True)
    queue = state["pending_batch"]
    queue["baseline"] = agent.nodes._baseline(state, queue["calls"][0])
    queue["calls"][0].update(state="running", start_revision=0)
    agent.store.upsert_state("s1", state)
    (workspace / "partial.py").write_text("value = 1\n")
    result = agent.resume("s1")
    assert tools.executed == []
    assert result["changed_files"] == ["partial.py"]
    assert result["static_check"]["status"] == "passed"
    assert result["test_results"][0]["status"] == "interrupted"
    assert result["test_results"][0]["freshness"] == "stale"
    assert result["tool_call_count"] == 1


def test_cli_displays_static_and_test_scope_freshness_separately(capsys):
    workspace = make_workspace()
    agent, llm, tools = make_agent(workspace, [])
    llm.action_responses = [batch(chosen_test(tools)), batch(write("a.py")), final()]
    state = agent.run("Run then edit", workspace)
    _print_result(state)
    output = capsys.readouterr().out
    assert "static_check: passed" in output
    assert "tests: passed; freshness=stale; scope=targets" in output
    assert "tests/test_selected.py" in output
    assert "entire project passed" not in output
