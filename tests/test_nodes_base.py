from __future__ import annotations

import json
from pathlib import Path

from app.graph.nodes import AgentNodes
from app.tools import ToolResult
from tests import (
    FakeLLM,
    FakeStore,
    FakeTools,
    make_settings,
    make_state,
    make_workspace,
    set_usage,
    tool_response,
)


def make_nodes(tmp_path: Path, *, stream_fn=None, settings_overrides=None):
    llm = FakeLLM()
    store = FakeStore()
    tools = FakeTools()
    settings = make_settings(tmp_path, **(settings_overrides or {}))
    nodes = AgentNodes(llm, store, tools, settings, stream_fn=stream_fn)
    return nodes, llm, store, tools


def test_protocol_error_is_not_classified_from_error_text() -> None:
    tmp_path = make_workspace()
    nodes, _, _, _ = make_nodes(tmp_path)

    state = make_state(tmp_path)
    nodes._observe_error(state, "protocol_error", "act failed: multiple tool calls")
    assert state["status"] == "acting"
    assert state["required_next_action"] == ""
    observation = json.loads(state["action_history"][-1]["content"])
    assert observation["error_kind"] == "protocol_error"


def test_recent_turns_are_only_in_session_messages_without_character_truncation() -> None:
    tmp_path = make_workspace()
    nodes, _, _, _ = make_nodes(tmp_path, settings_overrides={"max_context_turns": 2})
    long_text = "x" * 200
    state = make_state(
        tmp_path,
        active_turn_id="t3",
        turns=[
            {"turn_id": "t1", "user_request": long_text, "status": "finished", "summary": long_text},
            {"turn_id": "t2", "user_request": "request2", "status": "failed", "summary": "summary2"},
            {"turn_id": "t3", "user_request": "active", "status": "running", "summary": "active"},
        ],
    )

    text = nodes._recent_turns_text(state)

    assert "active" not in text
    assert "request2" not in text
    history = nodes._build_action_history(state)
    assert "request2" in json.dumps(history)
    assert long_text in json.dumps(history)


def test_validate_changed_files_success_and_failure_cases() -> None:
    tmp_path = make_workspace()
    nodes, _, _, _ = make_nodes(tmp_path)

    good = tmp_path / "good.py"
    good.write_text("x = 1\n", encoding="utf-8")

    state = make_state(tmp_path, changed_files=["good.py"])
    assert nodes._validate_changed_files(state) is None
    state["changed_files"] = []
    assert nodes._validate_changed_files(state) == "verification requires recorded changed_files."
    state["changed_files"] = ["../evil.py"]
    assert "outside workspace" in nodes._validate_changed_files(state)
    state["changed_files"] = ["missing.py"]
    assert "not found" in nodes._validate_changed_files(state)

    bad = tmp_path / "bad.py"
    bad.write_text("def x(:\n", encoding="utf-8")
    state["changed_files"] = ["bad.py"]
    err = nodes._validate_changed_files(state)
    assert err is not None and "py_compile check failed" in err


def test_validate_changed_files_skips_deleted_files() -> None:
    tmp_path = make_workspace()
    nodes, _, _, _ = make_nodes(tmp_path)
    state = make_state(tmp_path, deleted_files=["removed.py"], changed_files=["removed.py"])

    assert nodes._validate_changed_files(state) is None


def test_reserve_budget_and_three_identical_failures() -> None:
    tmp_path = make_workspace()
    nodes, _, _, _ = make_nodes(tmp_path, settings_overrides={"max_tool_calls": 1})
    state = make_state(tmp_path)

    assert nodes._reserve_tool_budget(state, 1) is True
    assert nodes._reserve_tool_budget(state, 1) is False
    assert state["tool_call_count"] == 1
    assert "MAX_TOOL_CALLS" in state["stop_reason"]

    state2 = make_state(tmp_path)
    for _ in range(2):
        nodes._observe_error(state2, "execution_error", "Traceback: boom", {"tool": "read_file", "path": "x"})
        assert state2["status"] == "acting"
    nodes._observe_error(state2, "execution_error", "Traceback: boom", {"tool": "read_file", "path": "x"})
    assert state2["status"] == "failed"


def test_build_action_history_retains_all_complete_native_pairs() -> None:
    tmp_path = make_workspace()
    nodes, _, _, _ = make_nodes(tmp_path)
    tool_calls = []
    for i in range(8):
        response = tool_response("read_file", {"path": f"file{i}"}, content=f"Reading {i}", call_id=f"call{i}")
        tool_calls.append(
            {
                "name": f"tool{i}",
                "args": {"i": i},
                "ok": i % 2 == 0,
                "stdout": f"out{i}",
                "stderr": f"err{i}",
                "assistant_message": response.to_message(),
                "tool_call_id": f"call{i}",
            }
        )
    state = make_state(tmp_path, tool_calls=tool_calls)

    history = nodes._build_action_history(state)

    assert history is not None
    history = [m for m in history if m["role"] in {"assistant", "tool"}]
    assert len(history) == 16
    assert history[0]["content"] == "Reading 0"
    for i in range(0, len(history), 2):
        assert history[i]["role"] == "assistant"
        assert history[i + 1]["role"] == "tool"
        assert history[i]["tool_calls"][0]["id"] == history[i + 1]["tool_call_id"]
    assert json.loads(history[3]["content"])["stderr"] == "err1"


def test_build_action_history_converts_legacy_calls_to_observations() -> None:
    tmp_path = make_workspace()
    nodes, _, _, _ = make_nodes(tmp_path)
    state = make_state(tmp_path, tool_calls=[{
        "name": "read_file", "args": {"path": "old.py"}, "ok": True, "stdout": "old content",
        "llm_decision": {"tool": "read_file", "args": {"path": "old.py"}},
    }])

    history = nodes._build_action_history(state)

    assert history is not None
    assert all(m["role"] == "user" for m in history)
    assert "old.py" in json.dumps(history)
    assert "old content" in json.dumps(history)
    assert "unrecoverable" in json.dumps(history)


def test_latest_tool_observation_preserves_long_output_without_prompt_duplication():
    tmp_path = make_workspace()
    nodes, _, _, _ = make_nodes(tmp_path)
    calls = []
    for index in range(2):
        response = tool_response("read_file", {"path": f"file{index}"}, call_id=f"call{index}")
        calls.append({
            "assistant_message": response.to_message(), "tool_call_id": f"call{index}",
            "stdout": "x" * 2000, "stderr": "e" * 2000,
        })

    history = nodes._build_action_history(make_state(tmp_path, tool_calls=calls))
    history = [m for m in history if m["role"] in {"assistant", "tool"}]

    older = json.loads(history[1]["content"])
    latest = json.loads(history[3]["content"])
    assert len(older["stdout"]) == 2000
    assert len(older["stderr"]) == 2000
    assert len(latest["stdout"]) == len(latest["stderr"]) == 2000


def test_make_payload_and_backup_dir() -> None:
    tmp_path = make_workspace()
    nodes, _, _, _ = make_nodes(tmp_path)
    state = make_state(tmp_path, session_id="session-123")

    backup_dir = nodes._session_backup_dir(state)
    payload = nodes._make_payload(state, "write_file", {"path": "a.py"})

    assert backup_dir.endswith("session-123")
    assert payload.backup_dir == backup_dir
    assert payload.name == "write_file"


def test_accumulate_usage_records_state_and_store() -> None:
    tmp_path = make_workspace()
    nodes, llm, store, _ = make_nodes(tmp_path)
    state = make_state(tmp_path)
    set_usage(llm, prompt=10, completion=5)

    nodes._accumulate_usage(state, "plan")

    assert state["token_usage"]["total_tokens"] == 15
    assert state["token_usage"]["llm_calls"] == 1
    assert store.token_usages[0]["node_name"] == "plan"


def test_stream_or_complete_text_prefers_stream_when_stream_fn_exists() -> None:
    tmp_path = make_workspace()
    chunks: list[str] = []
    llm = FakeLLM(stream_responses=[["A", "B"]], text_responses=["fallback"])
    store = FakeStore()
    tools = FakeTools([ToolResult(ok=True)])
    settings = make_settings(tmp_path)
    nodes = AgentNodes(llm, store, tools, settings, stream_fn=chunks.append)

    text = nodes._stream_or_complete_text("sys", "user")

    assert text == "AB"
    assert chunks == ["A", "B"]
    assert len(llm.stream_calls) == 1


def test_stream_or_complete_text_falls_back_to_complete_text() -> None:
    tmp_path = make_workspace()
    nodes, llm, _, _ = make_nodes(tmp_path)
    llm.text_responses = ["plain"]

    text = nodes._stream_or_complete_text("sys", "user")

    assert text == "plain"
    assert len(llm.text_calls) == 1
