from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from app.llm import ActionResponse, ToolCall
from app.store import SQLiteStore
from app.tools import ToolExecutor, ToolResult
from tests import make_state, make_workspace
from tests.test_agent_turns import make_agent


def batch(*calls, content="Working."):
    return ActionResponse(content, tuple(ToolCall(f"call_{i}", name, args) for i, (name, args) in enumerate(calls)), "tool_calls")


def write(path, content="value = 1\n"):
    return "write_file", {"path": path, "content": content}


def final(answer="Done."):
    return ActionResponse(answer, (), "stop")


def assert_pairs(history, count):
    assistant = next(message for message in history if message["role"] == "assistant")
    ids = [call["id"] for call in assistant["tool_calls"]]
    results = [message["tool_call_id"] for message in history if message["role"] == "tool"]
    assert ids == results
    assert len(ids) == count
    assert sum(message["role"] == "assistant" for message in history) == 1


def test_two_writes_verify_once_and_use_one_native_batch():
    workspace = make_workspace()
    (workspace / "tests").mkdir()
    agent, llm, tools = make_agent(workspace, [batch(write("a.py"), write("b.py")), final("Both done.")])
    state = agent.run("Implement two files", workspace)
    assert state["summary"] == "Both done."
    assert state["tool_call_count"] == 2
    assert [call.name for call in tools.executed] == ["write_file", "write_file"]
    assert_pairs(llm.action_calls[-1]["history"], 2)
    assert llm.text_calls == llm.stream_calls == []
    assert not {"diagnose", "reflect"}.intersection(agent.graph.get_graph().nodes)
    assert not hasattr(agent.nodes, "diagnose")
    assert not hasattr(agent.nodes, "reflect")
    assert state["action_history"][0]["type"] == "tool_batch"
    assert state["action_history"][1]["type"] == "observation"
    assert json.loads(state["action_history"][1]["content"])["status"] == "passed"


def test_same_file_operations_are_sequential():
    workspace = make_workspace()
    agent, llm, tools = make_agent(workspace, [batch(
        write("a.py"), ("patch_file", {"path": "a.py", "old_str": "value = 1", "new_str": "value = 2"}),
        ("read_file", {"path": "a.py"}),
    ), final()])
    state = agent.run("Update and inspect", workspace)
    assert (workspace / "a.py").read_text() == "value = 2\n"
    assert "value = 2" in state["tool_calls"][2]["stdout"]
    assert [call.name for call in tools.executed] == ["write_file", "patch_file", "read_file"]
    assert_pairs(llm.action_calls[-1]["history"], 3)


@pytest.mark.parametrize("bad", [
    ("write_file", {"content": "missing path"}), ("read_file", {"path": 12}), ("finish", {}),
])
def test_schema_errors_reject_entire_batch_without_valid_prefix(bad):
    workspace = make_workspace()
    agent, llm, tools = make_agent(workspace, [batch(write("first.py"), bad), final("Bad input.")])
    state = agent.run("Write files", workspace)
    assert not (workspace / "first.py").exists()
    assert not tools.executed
    assert state["tool_call_count"] == 2
    assert state["retry_attempts"] == 1
    assert [call["error_kind"] for call in state["tool_calls"]] == ["batch_skipped", "invalid_tool_args"]
    assert_pairs(llm.action_calls[-1]["history"], 2)


def test_raw_argument_error_keeps_native_pair_without_executing_siblings():
    workspace = make_workspace()
    response = batch(write("first.py"))
    response = ActionResponse("", (*response.tool_calls, ToolCall("bad", "read_file", None, "not json", "Invalid JSON")), "tool_calls")
    agent, llm, tools = make_agent(workspace, [response, final()])
    state = agent.run("Inspect", workspace)
    assert not tools.executed
    assert state["tool_call_count"] == 2
    assert_pairs(llm.action_calls[-1]["history"], 2)
    assert next(m for m in llm.action_calls[-1]["history"] if m.get("tool_calls"))["tool_calls"][1]["function"]["arguments"] == "not json"


def test_unavailable_shell_rejects_entire_batch():
    workspace = make_workspace()
    agent, llm, tools = make_agent(workspace, [batch(write("first.py"), ("bash", {"command": "pwd"})), final()])
    tools.permissions.shells.pop("bash", None)
    state = agent.run("Write then inspect", workspace)
    assert not tools.executed
    assert not (workspace / "first.py").exists()
    assert state["tool_calls"][1]["error_kind"] == "invalid_tool_args"
    assert_pairs(llm.action_calls[-1]["history"], 2)


@pytest.mark.parametrize("amount", [8, 9])
def test_batch_size_limit(amount):
    workspace = make_workspace()
    agent, llm, tools = make_agent(workspace, [batch(*[("list_files", {})] * amount), final()])
    state = agent.run("Inspect", workspace)
    assert len(tools.executed) == (8 if amount == 8 else 0)
    assert state["tool_call_count"] == amount
    assert_pairs(llm.action_calls[-1]["history"], amount)


def test_insufficient_budget_rejects_batch_without_consuming_prefix():
    workspace = make_workspace()
    agent, llm, tools = make_agent(workspace, [batch(write("a.py"), write("b.py"), write("c.py"))], max_tool_calls=2)
    state = agent.run("Write files", workspace)
    assert state["status"] == "failed"
    assert "MAX_TOOL_CALLS=2" in state["summary"]
    assert state["tool_call_count"] == 0
    assert not tools.executed
    assert_pairs(agent.nodes._build_action_history(state), 3)


@pytest.mark.parametrize("reason", ["deny", "reject", "failure", "timeout", "cancelled"])
def test_failure_or_refusal_stops_suffix_and_keeps_prefix_changes(reason):
    workspace = make_workspace()
    middle = ("read_file", {"path": ".env"}) if reason == "deny" else write("pyproject.toml", "[project]\nname='x'\n")
    agent, llm, tools = make_agent(workspace, [batch(write("first.py"), middle, write("last.py")), final("Stopped.")])
    if reason == "reject":
        agent.nodes.confirm_fn = lambda name, args: "reject" if args.get("path") == "pyproject.toml" else "approve_once"
    elif reason in {"failure", "timeout", "cancelled"}:
        original = tools._execute_raw
        def fail_second(action):
            if action.args.get("path") == "pyproject.toml":
                return ToolResult(False, stderr="middle request failed", exit_code=124,
                                  error_kind=reason if reason != "failure" else "execution_error")
            return original(action)
        tools._execute_raw = fail_second
    state = agent.run("Write files", workspace)
    assert (workspace / "first.py").exists()
    assert not (workspace / "last.py").exists()
    assert state["changed_files"] == ["first.py"]
    assert state["tool_calls"][2]["error_kind"] == "batch_skipped"
    assert state["retry_attempts"] == 1
    assert any(event.node_name == "verify" for event in agent.get_logs(state["session_id"]))
    assert_pairs(llm.action_calls[-1]["history"], 3)


def test_second_approval_pause_resumes_in_new_process_without_replaying_first():
    workspace = make_workspace()
    agent, llm, tools = make_agent(workspace, [batch(write("first.py"), write("second.py"), write("third.py"))])
    answers = iter(["approve_once", "unavailable"])
    agent.nodes.confirm_fn = lambda *_: next(answers)
    state = agent.run("Write files", workspace)
    assert state["status"] == "awaiting_human_confirm"
    assert state["pending_batch"]["index"] == 1
    assert state["tool_call_count"] == 3
    assert state["retry_attempts"] == 0
    assert len(tools.executed) == 1
    with pytest.raises(ValueError, match="pending"):
        agent.run_turn(state["session_id"], "Another request", workspace)
    resumed, llm2, tools2 = make_agent(workspace, [final()])
    result = resumed.resume(state["session_id"])
    assert [call.args["path"] for call in tools2.executed] == ["second.py", "third.py"]
    assert len(result["tool_calls"]) == 3
    assert result["tool_call_count"] == 3
    assert_pairs(llm2.action_calls[-1]["history"], 3)


def test_model_test_pause_does_not_replay_modification_batch():
    workspace = make_workspace()
    (workspace / "tests").mkdir()
    agent, llm, tools = make_agent(workspace, [batch(write("a.py"), write("b.py")), final()])
    shell, args = tools.test_action()
    llm.action_responses.insert(1, batch((shell, args)))
    answers = iter(["approve_once", "approve_once", "unavailable"])
    agent.nodes.confirm_fn = lambda *_: next(answers)
    state = agent.run("Write files", workspace)
    assert state["pending_batch"]["origin"] == "act"
    assert len(tools.executed) == 2
    agent.nodes.confirm_fn = lambda *_: "approve_once"
    result = agent.resume(state["session_id"])
    assert len(tools.executed) == 3
    assert result["tool_call_count"] == 3
    assert result["test_results"][-1]["status"] == "passed"


def test_policy_change_during_second_approval_denies_call_and_suffix():
    workspace = make_workspace()
    agent, llm, tools = make_agent(workspace, [batch(write("first.py"), write("second.py"), write("last.py")), final()])
    policy = workspace / ".user" / "permissions.json"
    policy.parent.mkdir()
    tools.permissions.config_path = policy
    def approve(name, args):
        if args.get("path") == "second.py":
            policy.write_text(json.dumps({"version": 1, "rules": [
                {"id": "user-deny", "tool": "write_file", "path": "*", "decision": "deny"},
            ]}))
        return "approve_once"
    agent.nodes.confirm_fn = approve
    state = agent.run("Write three files", workspace)
    assert [call.args["path"] for call in tools.executed] == ["first.py"]
    assert (workspace / "first.py").exists()
    assert not (workspace / "second.py").exists()
    assert not (workspace / "last.py").exists()
    assert state["tool_calls"][1]["execution_status"] == "denied"
    assert state["tool_calls"][2]["error_kind"] == "batch_skipped"
    assert state["retry_attempts"] == 1


class SimulatedCrash(BaseException):
    pass


@pytest.mark.parametrize("committed", [False, True])
def test_crash_at_result_checkpoint_never_replays_first_request(committed):
    workspace = make_workspace()
    agent, llm, tools = make_agent(workspace, [batch(write("first.py"), write("last.py")), final()])
    save = agent.store.save_action_result
    def crash(session_id, state, node_name, call):
        if committed:
            save(session_id, state, node_name, call)
        raise SimulatedCrash()
    agent.store.save_action_result = crash
    with pytest.raises(SimulatedCrash):
        agent.run("Write files", workspace)
    sid = agent.last_session_id()
    assert (workspace / "first.py").exists()
    agent.store.save_action_result = save
    result = agent.resume(sid)
    assert sum(call.args.get("path") == "first.py" for call in tools.executed) == 1
    if committed:
        assert (workspace / "last.py").exists()
        assert result["tool_calls"][0]["execution_status"] == "executed"
    else:
        assert not (workspace / "last.py").exists()
        assert result["tool_calls"][0]["execution_status"] == "indeterminate"
        assert result["tool_calls"][1]["error_kind"] == "batch_skipped"
        assert "first.py" in result["changed_files"]
    assert_pairs(llm.action_calls[-1]["history"], 2)


def test_crash_after_failed_call_does_not_execute_pending_suffix():
    workspace = make_workspace()
    agent, llm, tools = make_agent(workspace, [batch(("read_file", {"path": "missing.py"}), write("last.py")), final()])
    save = agent.store.save_action_result
    def crash(*args):
        save(*args)
        raise SimulatedCrash()
    agent.store.save_action_result = crash
    with pytest.raises(SimulatedCrash):
        agent.run("Inspect then write", workspace)
    agent.store.save_action_result = save
    result = agent.resume(agent.last_session_id())
    assert not (workspace / "last.py").exists()
    assert len(tools.executed) == 1
    assert result["tool_calls"][1]["error_kind"] == "batch_skipped"


def test_interrupted_admission_rejection_does_not_execute_valid_suffix():
    workspace = make_workspace()
    agent, llm, tools = make_agent(workspace, [batch(("read_file", {"path": 1}), write("last.py")), final()])
    save = agent.store.save_action_result
    def crash(*args):
        save(*args)
        raise SimulatedCrash()
    agent.store.save_action_result = crash
    with pytest.raises(SimulatedCrash):
        agent.run("Inspect then write", workspace)
    agent.store.save_action_result = save
    result = agent.resume(agent.last_session_id())
    assert not tools.executed
    assert not (workspace / "last.py").exists()
    assert result["tool_call_count"] == 2
    assert result["retry_attempts"] == 1
    assert [call["error_kind"] for call in result["tool_calls"]] == ["invalid_tool_args", "batch_skipped"]
    assert_pairs(llm.action_calls[-1]["history"], 2)


@pytest.mark.parametrize("missing", [False, True])
def test_interrupted_execution_with_incomplete_scan_does_not_invent_changes(missing):
    workspace = make_workspace()
    (workspace / "existing.py").write_text("value = 1\n")
    agent, llm, tools = make_agent(workspace, [final()])
    state = make_state(workspace)
    response = batch(write("unknown.py"), write("last.py"))
    calls = [{"tool": call.name, "args": call.args, "tool_call_id": call.id} for call in response.tool_calls]
    agent.nodes._accept_batch(state, calls, "act", response.to_message())
    state["pending_batch"]["calls"][0]["state"] = "running"
    state["pending_batch"]["baseline"] = None if missing else {"files": {}, "complete": False}
    if not missing:
        def scan_error(*_):
            raise OSError("Cannot scan workspace")
        agent.nodes._baseline = scan_error
    agent.store.upsert_state("s1", state)
    result = agent.resume("s1")
    assert not tools.executed
    assert not result["changed_files"]
    assert not result["snapshot_complete"]
    assert result["tool_calls"][0]["execution_status"] == "indeterminate"
    assert result["tool_calls"][1]["error_kind"] == "batch_skipped"


def test_atomic_result_transaction_rolls_back_tool_log_and_state_together():
    workspace = make_workspace()
    store = SQLiteStore(workspace / ".agent" / "agent.db")
    state = make_state(workspace)
    store.upsert_state("s1", state)
    call = {"name": "read_file", "args": {}, "ok": True}
    with pytest.raises(TypeError):
        store.save_action_result("s1", {**state, "not_json": object()}, "execute", call)
    with sqlite3.connect(store.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0] == 0
    assert "not_json" not in store.load_state("s1")


def test_repeat_failure_ignores_random_ids_and_stops_after_three():
    workspace = make_workspace()
    responses = [ActionResponse("", (ToolCall(f"random_{i}", "read_file", {"path": "missing.py"}),), "tool_calls") for i in range(3)]
    agent, llm, tools = make_agent(workspace, responses)
    state = agent.run("Inspect", workspace)
    assert state["status"] == "failed"
    assert state["retry_attempts"] == 3
    assert "three times" in state["summary"]
    assert len(tools.executed) == 3
    assert llm.text_calls == []


def test_total_failure_budget_and_successful_batch_reset_of_streak():
    workspace = make_workspace()
    agent, llm, tools = make_agent(workspace, [
        batch(("read_file", {"path": "missing1"})), batch(("list_files", {})),
        batch(("read_file", {"path": "missing2"})),
    ], max_retry_steps=2)
    state = agent.run("Inspect", workspace)
    assert state["status"] == "failed"
    assert state["retry_attempts"] == 2
    assert len(state["recent_failures"]) == 1
    assert "MAX_RETRY_STEPS=2" in state["summary"]


def test_failed_file_tool_partial_change_gets_static_checked_before_stopping():
    workspace = make_workspace()
    agent, llm, tools = make_agent(workspace, [batch(write("partial.py"), write("last.py"))], max_retry_steps=1)
    def partial_write(action):
        (workspace / action.args["path"]).write_text("def broken(:\n")
        return ToolResult(False, stderr="write stopped after partial change", exit_code=1)
    tools._execute_raw = partial_write
    state = agent.run("Write files", workspace)
    assert state["status"] == "failed"
    assert state["changed_files"] == ["partial.py"]
    assert "Static validation failed" in state["verification_note"]
    assert "write stopped after partial change" in state["execution_errors"][0]
    assert not (workspace / "last.py").exists()
    assert len(tools.executed) == 1
    assert len(llm.action_calls) == 1


def test_stop_after_failed_batch_preserves_static_checks_and_skips_tests():
    workspace = make_workspace()
    (workspace / "tests").mkdir()
    agent, llm, tools = make_agent(workspace, [batch(write("first.py"), ("read_file", {"path": "missing.py"}))], max_retry_steps=1)
    state = agent.run("Write then inspect", workspace)
    assert state["status"] == "failed"
    assert "Static checks passed" in state["verification_note"]
    assert state["test_results"] == []
    assert all(call.name not in {"bash", "powershell"} for call in tools.executed)
    assert state["execution_errors"]
    assert len(llm.action_calls) == 1


def test_failed_resume_resets_recovery_window_but_keeps_consumed_budget():
    workspace = make_workspace()
    agent, llm, tools = make_agent(workspace, [batch(("list_files", {})), final()], max_tool_calls=5)
    state = make_state(workspace, status="failed", tool_call_count=3, retry_attempts=5,
                       recent_failures=["old"], stop_reason="Old stop", summary="Old summary",
                       execution_errors=["Old evidence"])
    agent.store.upsert_state("s1", state)
    result = agent.resume("s1")
    assert result["tool_call_count"] == 4
    assert result["retry_attempts"] == 0
    assert result["summary"] == "Done."
    assert result["execution_errors"] == ["Old evidence"]
    result.update(status="failed", tool_call_count=5)
    agent.store.upsert_state("s1", result)
    with pytest.raises(ValueError, match="Tool budget exhausted"):
        agent.resume("s1")
    assert len(tools.executed) == 1


def test_history_retains_all_whole_batches_below_token_limit():
    workspace = make_workspace()
    agent, llm, tools = make_agent(workspace, [*[batch(("list_files", {}), ("list_files", {})) for _ in range(7)], final()])
    state = agent.run("Inspect", workspace)
    history = llm.action_calls[-1]["history"]
    assert len(history) == 22
    assert len(state["action_history"]) == 7
    history = history[1:]
    for start in range(0, len(history), 3):
        assert_pairs(history[start:start + 3], 2)


def test_protocol_error_can_be_followed_by_write_without_any_gate():
    workspace = make_workspace()
    agent, llm, tools = make_agent(workspace, [RuntimeError("act failed: invalid response"), batch(write("a.py")), final()])
    state = agent.run("Implement", workspace)
    assert (workspace / "a.py").exists()
    assert state["retry_attempts"] == 1
    assert state["failure_type"] == state["required_next_action"] == ""
    assert llm.text_calls == []


@pytest.mark.parametrize("status", ["diagnosing", "reflecting", "failed"])
def test_old_recovery_states_ignore_obsolete_diagnosis(status):
    workspace = make_workspace()
    agent, llm, tools = make_agent(workspace, [batch(write("a.py")), final()])
    state = make_state(workspace, status=status, summary="Old stopping summary", required_next_action="run_target_test",
                       root_cause_hypothesis="Implementation assertion failed", retry_attempts=19,
                       recent_failures=["old", "old"], error="old evidence")
    agent.store.upsert_state("s1", state)
    result = agent.resume("s1")
    assert result["summary"] == "Done."
    assert (workspace / "a.py").exists()
    assert "Implementation assertion failed" not in llm.action_calls[0]["user_prompt"]
    assert llm.text_calls == []


def test_explicit_model_test_is_not_followed_by_automatic_test():
    workspace = make_workspace()
    (workspace / "tests").mkdir()
    agent, llm, tools = make_agent(workspace, [])
    shell, args = tools.test_action()
    llm.action_responses = [batch(write("a.py"), (shell, args)), final()]
    state = agent.run("Implement and test", workspace)
    assert sum(call.name == shell for call in tools.executed) == 1
    assert state["tool_call_count"] == 2
    assert state["test_results"][-1]["status"] == "passed"


def test_internal_test_modifications_receive_post_static_check_without_recursion():
    workspace = make_workspace()
    (workspace / "tests").mkdir()
    agent, llm, tools = make_agent(workspace, [batch(write("first.py")), final("Verification left invalid code.")])
    shell, args = tools.test_action()
    llm.action_responses.insert(1, batch((shell, args)))
    def mutate_test(action, command, timeout):
        (workspace / "generated.py").write_text("def broken(:\n")
        return ToolResult(True, stdout="1 passed")
    tools._run_shell = mutate_test
    state = agent.run("Implement", workspace)
    assert state["summary"] == "Verification left invalid code."
    assert "Static validation failed" in state["verification_note"]
    assert state["test_results"][-1]["freshness"] == "stale"
    assert "generated.py" in state["changed_files"]
    assert state["tool_call_count"] == 2
    assert state["retry_attempts"] == 1
    assert len(tools.executed) == 2


def test_missing_pytest_returns_fact_then_allows_model_to_read_and_answer():
    workspace = make_workspace()
    (workspace / "tests").mkdir()
    agent, llm, tools = make_agent(workspace, [batch(write("first.py")), batch(("read_file", {"path": "first.py"})),
                                             final("pytest is not installed; validation remains incomplete.")],
                                   test_results=[ToolResult(False, stderr="No module named pytest", exit_code=1)])
    shell, args = tools.test_action()
    llm.action_responses.insert(1, batch((shell, args)))
    state = agent.run("Implement", workspace)
    assert state["status"] == "finished"
    assert state["error"] == "No module named pytest"
    assert state["retry_attempts"] == 1
    assert state["required_next_action"] == ""
    assert tools.executed[-1].name == "read_file"
    assert "assertion" not in state["verification_note"].lower()
    assert state["test_results"][-1]["exit_code"] == 1
    assert state["test_results"][-1]["status"] == "failed"


def test_real_lru_batch_writes_implementation_and_tests_then_verifies():
    workspace = make_workspace()
    source = """from collections import OrderedDict

class LRUCache:
    def __init__(self, capacity):
        if type(capacity) is not int or capacity <= 0:
            raise ValueError('capacity must be a positive integer')
        self.capacity = capacity
        self.items = OrderedDict()

    def get(self, key):
        if key not in self.items:
            return -1
        self.items.move_to_end(key)
        return self.items[key]

    def put(self, key, value):
        self.items[key] = value
        self.items.move_to_end(key)
        if len(self.items) > self.capacity:
            self.items.popitem(last=False)
"""
    tests = """import pytest
from test1.lru import LRUCache

@pytest.mark.parametrize('capacity', [0, -1, 1.5, True])
def test_invalid_capacity(capacity):
    with pytest.raises(ValueError):
        LRUCache(capacity)

def test_miss_and_access_eviction():
    cache = LRUCache(2)
    assert cache.get('absent') == -1
    cache.put('a', 1)
    cache.put('b', 2)
    assert cache.get('a') == 1
    cache.put('c', 3)
    assert cache.get('b') == -1
    assert cache.get('a') == 1
    assert cache.get('c') == 3

def test_update_changes_recency_without_growing():
    cache = LRUCache(2)
    cache.put('a', 1)
    cache.put('b', 2)
    cache.put('a', 9)
    cache.put('c', 3)
    assert cache.get('a') == 9
    assert cache.get('b') == -1
    assert len(cache.items) == 2

def test_capacity_one():
    cache = LRUCache(1)
    cache.put('a', 1)
    cache.put('b', 2)
    assert cache.get('a') == -1
    assert cache.get('b') == 2
"""
    agent, llm, tools = make_agent(workspace, [batch(write("test1/lru.py", source), write("test1/test_lru.py", tests)), final("LRU implemented.")])
    if not tools.permissions.shells:
        pytest.skip("Backend absent")
    tools._run_shell = ToolExecutor._run_shell.__get__(tools)
    shell, args = tools.test_action("test1/test_lru.py")
    llm.action_responses.insert(1, batch(("read_file", {"path": "test1/test_lru.py"})))
    llm.action_responses.insert(2, batch((shell, args)))
    state = agent.run("Implement LRU under test1 and add tests", workspace)
    assert state["test_results"][-1]["status"] == "passed"
    assert state["test_results"][-1]["scope"]["targets"] == ["test1/test_lru.py"]
    assert "7 passed" in state["tool_calls"][-1]["stdout"]
    assert state["tool_call_count"] == 4
    assert len(llm.action_calls) == 4
    assert llm.text_calls == []
