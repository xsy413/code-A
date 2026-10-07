from __future__ import annotations

import io
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from app.agent import CodingAgent
from app.cli import _print_result
from app.llm import ActionResponse, OpenAICompatClient
from app.tools import ToolExecutor, ToolInput, ToolResult
from tests import FakeLLM, act_and_execute, make_settings, make_state, make_workspace, tool_response


class ScriptedTests(ToolExecutor):
    def __init__(self, policy, test_results: list[ToolResult] | None = None):
        super().__init__(policy)
        self.test_results = list(test_results or [])
        self.executed: list[ToolInput] = []

    def _run_shell(self, action: ToolInput, command: str, timeout: int) -> ToolResult:
        if "pytest" in command:
            return self.test_results.pop(0) if self.test_results else ToolResult(ok=True, stdout="1 passed")
        return super()._run_shell(action, command, timeout)

    def execute(self, payload: ToolInput, **kwargs) -> ToolResult:
        result = super().execute(payload, **kwargs)
        if result.execution_status == "executed":
            self.executed.append(payload)
        return result


def make_agent(workspace: Path, responses: list, *, test_results=None, **settings_overrides):
    settings = make_settings(workspace, **settings_overrides)
    agent = CodingAgent(settings, confirm_fn=lambda *_: "approve_once")
    llm = FakeLLM(action_responses=responses)
    tools = ScriptedTests(agent.sandbox, test_results)
    tools.audit = agent.store.add_permission_event
    agent.permissions = tools.permissions
    agent.store.sanitize = tools.permissions.redact
    agent.llm = agent.nodes.llm = llm
    agent.tools = agent.nodes.tools = tools
    return agent, llm, tools


def test_graph_verifies_two_writes_then_preserves_final_answer(capsys) -> None:
    workspace = make_workspace()
    (workspace / "tests").mkdir()
    answer = "Both files implemented.\nStatic checks passed; tests not run."
    agent, llm, tools = make_agent(workspace, [
        tool_response("write_file", {"path": "first.py", "content": "first = 1\n"}, call_id="first"),
        tool_response("write_file", {"path": "second.py", "content": "second = 2\n"}, call_id="second"),
        ActionResponse(answer, (), "stop"),
    ])

    result = agent.run("Implement two files", workspace)

    assert result["status"] == "finished"
    assert result["summary"] == answer
    assert result["changed_files"] == ["first.py", "second.py"]
    assert result["tool_call_count"] == 2
    assert [call.name for call in tools.executed] == ["write_file", "write_file"]
    assert (workspace / "first.py").read_text() == "first = 1\n"
    assert (workspace / "second.py").read_text() == "second = 2\n"
    assert [message["role"] for message in llm.action_calls[1]["history"]] == ["user", "assistant", "tool", "user"]
    assert llm.text_calls == []
    assert len(llm.action_calls) == 3
    saved = agent.store.load_state(result["session_id"])
    assert saved["turns"][-1]["summary"] == answer
    assert saved["action_history"][0]["assistant_message"]["tool_calls"][0]["id"] == "first"
    assert agent.store.get_events(result["session_id"])[-1].node_name == "finish"
    _print_result(result)
    assert capsys.readouterr().out.count(answer) == 1


def test_failed_verification_can_end_with_explanation_and_start_next_turn() -> None:
    workspace = make_workspace()
    (workspace / "tests").mkdir()
    agent, llm, tools = make_agent(workspace, [
        tool_response("write_file", {"path": "first.py", "content": "value = 1\n"}),
        ActionResponse("Tests still fail: assertion mismatch.", (), "stop"),
    ], test_results=[ToolResult(ok=False, stdout="assert expected 2, got 1", exit_code=1)])
    shell, args = tools.test_action()
    llm.action_responses.insert(1, tool_response(shell, args, call_id="chosen-test"))

    first = agent.run("Implement the feature", workspace)

    assert first["status"] == "finished"
    assert first["error"] == "assert expected 2, got 1"
    assert first["test_results"][-1]["status"] == "failed"
    assert first["static_check"]["status"] == "passed"
    assert first["required_next_action"] == ""
    assert '"status": "failed"' in llm.action_calls[-1]["user_prompt"]

    llm.action_responses.extend([
        tool_response("write_file", {"path": "second.py", "content": "value = 2\n"}),
        ActionResponse("Second turn complete.", (), "stop"),
    ])
    second = agent.run_turn(first["session_id"], "Now implement another file", workspace)

    assert second["status"] == "finished"
    assert second["error"] == ""
    assert second["retry_attempts"] == 0
    assert second["diagnostic_budget_used"] == 0
    assert second["changed_files"] == ["second.py"]
    assert "Tests still fail" in json.dumps(llm.action_calls[3]["history"])
    assert "Required next action from diagnosis:" not in llm.action_calls[3]["user_prompt"]
    assert len(second["turns"]) == 2
    assert second["turns"][0]["error"] == first["error"]
    assert tools.executed[-1].name == "write_file"
    assert second["test_results"] == []
    assert second["turns"][0]["test_results"][0]["status"] == "failed"


def test_graph_read_only_turn_outlives_default_graph_recursion_limit() -> None:
    workspace = make_workspace()
    agent, llm, tools = make_agent(workspace, [
        *[tool_response("list_files", {}) for _ in range(15)],
        ActionResponse("Architecture explained.", (), "stop"),
    ], max_tool_calls=20)

    result = agent.run("Explain this project", workspace)

    assert result["status"] == "finished"
    assert result["write_count"] == 0
    assert result["tool_call_count"] == 15
    assert len(tools.executed) == 15
    assert len(llm.action_calls[-1]["history"]) == 31


def test_graph_budget_failure_is_deterministic_without_summary_llm_call() -> None:
    workspace = make_workspace()
    agent, llm, tools = make_agent(workspace, [tool_response("list_files", {}) for _ in range(3)], max_tool_calls=2)

    result = agent.run("Inspect files", workspace)

    assert result["status"] == "failed"
    assert "MAX_TOOL_CALLS=2" in result["error"]
    assert "MAX_TOOL_CALLS=2" in result["summary"]
    assert len(tools.executed) == 2
    assert llm.text_calls == []
    assert result["needs_more_action"] is False


def test_resume_legacy_json_history_then_finish_and_continue_session() -> None:
    workspace = make_workspace()
    agent, llm, tools = make_agent(workspace, [ActionResponse("Resumed answer.", (), "stop")])
    state = make_state(workspace, status="acting", active_turn_id="legacy-turn", turns=[{
        "turn_id": "legacy-turn", "user_request": "Explain old code", "status": "running",
    }], tool_calls=[{
        "name": "read_file", "args": {"path": "old.py"}, "stdout": "old code", "ok": True,
        "llm_decision": {"tool": "read_file", "args": {"path": "old.py"}},
    }])
    agent.store.upsert_state("s1", state)

    resumed = agent.resume("s1")

    assert resumed["summary"] == "Resumed answer."
    assert resumed["turns"][0]["summary"] == "Resumed answer."
    assert llm.action_calls[0]["history"][0]["role"] == "user"
    assert "old code" in json.dumps(llm.action_calls[0]["history"])
    assert agent.resume("s1")["summary"] == "Resumed answer."
    assert len(llm.action_calls) == 1
    llm.action_responses.append(ActionResponse("Follow-up answer.", (), "stop"))
    result = agent.run_turn("s1", "Explain more", workspace)
    assert result["summary"] == "Follow-up answer."
    assert result["tool_calls"] == []
    assert len(result["turns"]) == 2
    assert "Resumed answer." in json.dumps(llm.action_calls[-1]["history"])
    assert tools.executed == []


@pytest.mark.parametrize("approved", [True, False])
def test_resume_pending_native_confirmation_keeps_protocol_pair(approved) -> None:
    workspace = make_workspace()
    agent, llm, tools = make_agent(workspace, [
        tool_response("write_file", {"path": "pyproject.toml", "content": "[project]\nname = 'example'\n"},
                      call_id="pending"),
        ActionResponse("Confirmed." if approved else "Write declined.", (), "stop"),
    ])
    agent.nodes.confirm_fn = lambda name, args: approved
    state = make_state(workspace, status="acting")
    assert act_and_execute(agent.nodes, state)["status"] == "awaiting_human_confirm"
    assert agent.store.load_state("s1")["pending_action"]["tool_call_id"] == "pending"

    result = agent.resume("s1")

    assert result["status"] == "finished"
    history = llm.action_calls[-1]["history"]
    history = [m for m in history if m["role"] in {"assistant", "tool"}]
    assert history[0]["tool_calls"][0]["id"] == history[1]["tool_call_id"] == "pending"
    assert result["tool_calls"][0]["ok"] is approved
    assert (workspace / "pyproject.toml").exists() is approved
    assert len(tools.executed) == int(approved)


def test_graph_recovers_from_invalid_response_without_treating_it_as_final() -> None:
    workspace = make_workspace()
    agent, llm, tools = make_agent(workspace, [
        RuntimeError("Invalid tool arguments: expected a JSON object."),
        ActionResponse("Unable to execute that action; invalid tool arguments.", (), "stop"),
    ])

    result = agent.run("Inspect project", workspace)

    assert result["status"] == "finished"
    assert "Invalid tool arguments" in result["error"]
    assert result["retry_attempts"] == 1
    assert len(llm.action_calls) == 2
    assert tools.executed == []


def test_native_http_responses_drive_graph_with_valid_history_and_usage() -> None:
    workspace = make_workspace()
    (workspace / "tests").mkdir()
    agent, _, tools = make_agent(workspace, [])
    agent.llm = agent.nodes.llm = OpenAICompatClient("test-key", "test-model", "https://example.com/v1")
    messages = [
        (tool_response("write_file", {"path": "first.py", "content": "first = 1\n"}, call_id="first").to_message(), "tool_calls"),
        (tool_response("write_file", {"path": "second.py", "content": "second = 2\n"}, call_id="second").to_message(), "tool_calls"),
        ({"role": "assistant", "content": "Both files done. Static checks passed."}, "stop"),
    ]
    bodies = [io.BytesIO(json.dumps({
        "choices": [{"message": message, "finish_reason": reason}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
    }).encode()) for message, reason in messages]

    with patch("urllib.request.urlopen", side_effect=bodies) as request:
        result = agent.run("Implement both files", workspace)

    assert result["summary"] == "Both files done. Static checks passed."
    assert result["status"] == "finished"
    assert result["token_usage"]["total_tokens"] == 21
    assert result["token_usage"]["llm_calls"] == 3
    assert agent.store.get_token_summary(result["session_id"])["llm_calls"] == 3
    assert len(tools.executed) == 2
    assert request.call_count == 3
    payloads = [json.loads(call.args[0].data) for call in request.call_args_list]
    for payload in payloads:
        assert payload["tool_choice"] == "auto"
        assert payload["parallel_tool_calls"] is True
    history = payloads[1]["messages"][1:-1]
    assert [message["role"] for message in history] == ["user", "assistant", "tool", "user"]
    assert history[1]["tool_calls"][0]["id"] == history[2]["tool_call_id"] == "first"


def test_direct_answer_uses_one_action_call_without_planning_or_verification():
    workspace = make_workspace()
    agent, llm, tools = make_agent(workspace, [ActionResponse("A short explanation.", (), "stop")])
    output = []
    agent.nodes.stream_fn = output.append

    result = agent.run("Explain without modifying files", workspace)

    assert result["summary"] == "A short explanation."
    assert result["changed_files"] == []
    assert len(llm.action_calls) == 1
    assert llm.text_calls == llm.stream_calls == []
    assert tools.executed == []
    assert [event.node_name for event in agent.get_logs(result["session_id"])] == [
        "intake", "preflight", "act", "finish",
    ]
    assert "Planning" not in "".join(output)
    assert "plan" not in agent.graph.get_graph().nodes


def test_simple_file_query_uses_one_tool_then_returns_the_original_answer():
    workspace = make_workspace()
    (workspace / "note.txt").write_text("notes", encoding="utf-8")
    agent, llm, tools = make_agent(workspace, [
        tool_response("list_files", {"pattern": "*.txt"}, call_id="listing"),
        ActionResponse("note.txt", (), "stop"),
    ])

    result = agent.run("List text files without modifying files", workspace)

    assert result["summary"] == "note.txt"
    assert result["changed_files"] == []
    assert result["verification_note"] == ""
    assert len(llm.action_calls) == 2
    assert llm.text_calls == llm.stream_calls == []
    assert [action.name for action in tools.executed] == ["list_files"]
    history = llm.action_calls[-1]["history"]
    history = [m for m in history if m["role"] in {"assistant", "tool"}]
    assert history[0]["tool_calls"][0]["id"] == history[1]["tool_call_id"] == "listing"
    assert "note.txt" in history[1]["content"]
    assert "Last tool output:" not in llm.action_calls[-1]["user_prompt"]
    assert not {"plan", "verify", "diagnose", "reflect"}.intersection(
        event.node_name for event in agent.get_logs(result["session_id"])
    )


def test_resume_legacy_planning_state_skips_plan_and_ignores_legacy_plan():
    workspace = make_workspace()
    agent, llm, tools = make_agent(workspace, [ActionResponse("Resumed directly.", (), "stop")])
    state = make_state(workspace, status="planning", plan="Obsolete mandatory plan",
                       workspace_snapshot={"has_tests": False})
    agent.store.upsert_state("s1", state)

    result = agent.resume("s1")

    assert result["summary"] == "Resumed directly."
    assert result["plan"] == "Obsolete mandatory plan"
    assert result["workspace_snapshot"] == {}
    assert llm.text_calls == llm.stream_calls == []
    assert len(llm.action_calls) == 1
    assert "Obsolete mandatory plan" not in llm.action_calls[0]["user_prompt"]
    assert tools.executed == []


def test_new_turn_resets_legacy_plan_and_workspace_observations():
    workspace = make_workspace()
    agent, llm, _ = make_agent(workspace, [ActionResponse("New request answered.", (), "stop")])
    state = make_state(workspace, status="finished", plan="Old development plan",
                       workspace_snapshot={"top_entries": ["obsolete-entry"]})
    agent.store.upsert_state("s1", state)

    result = agent.run_turn("s1", "Answer a new question", workspace)

    assert result["plan"] == ""
    assert result["workspace_snapshot"] == {}
    assert "obsolete-entry" not in llm.action_calls[0]["user_prompt"]
