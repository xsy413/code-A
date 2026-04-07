from __future__ import annotations

from pathlib import Path

from app.graph.nodes import AgentNodes
from tests import FakeLLM, FakeStore, FakeTools, make_settings, make_state, make_workspace


def make_nodes(tmp_path: Path, *, llm: FakeLLM | None = None, settings_overrides=None):
    _llm = llm or FakeLLM()
    store = FakeStore()
    tools = FakeTools()
    settings = make_settings(tmp_path, **(settings_overrides or {}))
    return AgentNodes(_llm, store, tools, settings), _llm


def test_reflect_repeated_failure_short_circuits() -> None:
    tmp_path = make_workspace()
    nodes, _ = make_nodes(tmp_path)
    state = make_state(tmp_path, error="Traceback: boom", recent_failures=["real_failure:runtime"])

    out = nodes.reflect(state)

    assert out["status"] == "failed"
    assert "Detected repeated identical failure" in out["error"]


def test_reflect_max_retry_steps_marks_failed() -> None:
    tmp_path = make_workspace()
    nodes, _ = make_nodes(tmp_path, settings_overrides={"max_retry_steps": 2})
    state = make_state(tmp_path, error="some failure", retry_attempts=1)

    out = nodes.reflect(state)

    assert out["status"] == "failed"
    assert "MAX_RETRY_STEPS=2" in out["error"]


def test_reflect_success_sets_acting_and_reflection() -> None:
    tmp_path = make_workspace()
    llm = FakeLLM(text_responses=["next step"])
    nodes, _ = make_nodes(tmp_path, llm=llm)
    state = make_state(tmp_path, error="tool failed", retry_attempts=0)

    out = nodes.reflect(state)

    assert out["status"] == "acting"
    assert out["needs_more_action"] is True
    assert out["last_reflection"] == "next step"


def test_reflect_llm_error_marks_failed() -> None:
    tmp_path = make_workspace()
    llm = FakeLLM(text_responses=[RuntimeError("llm down")])
    nodes, _ = make_nodes(tmp_path, llm=llm)
    state = make_state(tmp_path, error="tool failed", retry_attempts=0)

    out = nodes.reflect(state)

    assert out["status"] == "failed"
    assert out["error"].startswith("reflection failed:")
