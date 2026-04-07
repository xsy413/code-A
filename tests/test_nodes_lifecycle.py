from __future__ import annotations

from pathlib import Path

from app.graph.nodes import AgentNodes
from tests import FakeLLM, FakeStore, FakeTools, make_settings, make_state, make_workspace, set_usage


def make_nodes(tmp_path: Path, *, llm: FakeLLM | None = None):
    _llm = llm or FakeLLM()
    store = FakeStore()
    tools = FakeTools()
    settings = make_settings(tmp_path)
    return AgentNodes(_llm, store, tools, settings), _llm, store


def test_intake_sets_preflighting_and_records_event() -> None:
    tmp_path = make_workspace()
    nodes, _, store = make_nodes(tmp_path)
    state = make_state(tmp_path)

    out = nodes.intake(state)

    assert out["status"] == "preflighting"
    assert store.events[-1]["node_name"] == "intake"


def test_preflight_collects_workspace_snapshot_and_moves_to_planning() -> None:
    tmp_path = make_workspace()
    (tmp_path / "tests").mkdir()
    (tmp_path / "app").mkdir()
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")

    nodes, _, _ = make_nodes(tmp_path)
    state = make_state(tmp_path)

    out = nodes.preflight(state)

    assert out["status"] == "planning"
    snap = out["workspace_snapshot"]
    assert snap["has_tests"] is True
    assert snap["has_pyproject"] is True
    assert "app" in snap["source_roots"]


def test_plan_success_sets_plan_and_acting() -> None:
    tmp_path = make_workspace()
    llm = FakeLLM(text_responses=["1. do x"])
    set_usage(llm, prompt=3, completion=2)
    nodes, _, store = make_nodes(tmp_path, llm=llm)
    state = make_state(tmp_path)

    out = nodes.plan(state)

    assert out["status"] == "acting"
    assert out["plan"] == "1. do x"
    assert out["token_usage"]["llm_calls"] == 1
    assert store.events[-1]["node_name"] == "plan"


def test_plan_failure_marks_failed() -> None:
    tmp_path = make_workspace()
    nodes, llm, _ = make_nodes(tmp_path)
    llm.text_responses = [RuntimeError("boom")]
    state = make_state(tmp_path)

    out = nodes.plan(state)

    assert out["status"] == "failed"
    assert out["error"].startswith("planning failed:")


def test_finish_success_writes_summary() -> None:
    tmp_path = make_workspace()
    llm = FakeLLM(text_responses=["summary line"])
    set_usage(llm, prompt=1, completion=1)
    nodes, _, _ = make_nodes(tmp_path, llm=llm)
    state = make_state(tmp_path, status="finished", plan="p")

    out = nodes.finish(state)

    assert out["summary"] == "summary line"
    assert out["token_usage"]["total_tokens"] == 2


def test_finish_fallback_summary_on_error() -> None:
    tmp_path = make_workspace()
    nodes, llm, _ = make_nodes(tmp_path)
    llm.text_responses = [RuntimeError("summary failed")]
    state = make_state(tmp_path, status="failed", retry_attempts=2, tool_call_count=3, verification_note="none")

    out = nodes.finish(state)

    assert "status=failed" in out["summary"]
    assert "retry_attempts=2" in out["summary"]
