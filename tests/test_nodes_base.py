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
)


def make_nodes(tmp_path: Path, *, stream_fn=None, settings_overrides=None):
    llm = FakeLLM()
    store = FakeStore()
    tools = FakeTools()
    settings = make_settings(tmp_path, **(settings_overrides or {}))
    nodes = AgentNodes(llm, store, tools, settings, stream_fn=stream_fn)
    return nodes, llm, store, tools


def test_normalize_failure_variants() -> None:
    tmp_path = make_workspace()
    nodes, _, _, _ = make_nodes(tmp_path)

    assert nodes._normalize_failure("invalid_write_path: x") == "real_failure:invalid_path"
    assert nodes._normalize_failure("Traceback happened") == "real_failure:runtime"
    assert nodes._normalize_failure("permission denied") == "real_failure:permission"
    assert nodes._normalize_failure("no tests collected") == "verification_unavailable:no_tests"
    assert nodes._normalize_failure("no tests ran") == "verification_unavailable:no_tests"
    assert nodes._normalize_failure("ERROR at setup") == "real_failure:test_setup"


def test_recent_turns_text_ignores_active_turn_and_truncates() -> None:
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
    assert "request2" in text
    assert "..." in text


def test_validate_finish_success_and_failure_cases() -> None:
    tmp_path = make_workspace()
    nodes, _, _, _ = make_nodes(tmp_path)

    good = tmp_path / "good.py"
    good.write_text("x = 1\n", encoding="utf-8")

    state = make_state(tmp_path)
    assert nodes._validate_finish(state, ["good.py"], "done") is None

    assert nodes._validate_finish(state, [], "done") == "finish requires args.changed_files."
    assert nodes._validate_finish(state, ["good.py"], "") == "finish requires args.completion_reason."
    assert "outside workspace" in nodes._validate_finish(state, ["..\\evil.py"], "done")
    assert "not found" in nodes._validate_finish(state, ["missing.py"], "done")

    bad = tmp_path / "bad.py"
    bad.write_text("def x(:\n", encoding="utf-8")
    err = nodes._validate_finish(state, ["bad.py"], "done")
    assert err is not None and "py_compile check failed" in err


def test_validate_finish_skips_deleted_files() -> None:
    tmp_path = make_workspace()
    nodes, _, _, _ = make_nodes(tmp_path)
    state = make_state(tmp_path, deleted_files=["removed.py"])

    assert nodes._validate_finish(state, ["removed.py"], "done") is None


def test_bump_tool_budget_and_track_failure() -> None:
    tmp_path = make_workspace()
    nodes, _, _, _ = make_nodes(tmp_path, settings_overrides={"max_tool_calls": 1})
    state = make_state(tmp_path)

    assert nodes._bump_tool_budget(state) is True
    assert nodes._bump_tool_budget(state) is False
    assert state["status"] == "failed"

    state2 = make_state(tmp_path)
    assert nodes._track_failure(state2, "Traceback: boom") is True
    assert nodes._track_failure(state2, "Traceback: boom") is False
    assert state2["status"] == "failed"


def test_build_action_history_truncates_and_uses_llm_decision() -> None:
    tmp_path = make_workspace()
    nodes, _, _, _ = make_nodes(tmp_path)
    tool_calls = []
    for i in range(8):
        tool_calls.append(
            {
                "name": f"tool{i}",
                "args": {"i": i},
                "ok": i % 2 == 0,
                "stdout": f"out{i}",
                "stderr": f"err{i}",
                "llm_decision": {"tool": f"tool{i}", "args": {"i": i}},
            }
        )
    state = make_state(tmp_path, tool_calls=tool_calls)

    history = nodes._build_action_history(state)

    assert history is not None
    assert len(history) == 12
    first_assistant = history[0]["content"]
    parsed = json.loads(first_assistant)
    assert parsed["tool"] == "tool2"


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
