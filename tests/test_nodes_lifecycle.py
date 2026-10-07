from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

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


def test_preflight_checks_workspace_without_discovering_project_or_calling_llm() -> None:
    tmp_path = make_workspace()
    (tmp_path / "tests").mkdir()
    (tmp_path / "app").mkdir()
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")

    nodes, llm, store = make_nodes(tmp_path)
    state = make_state(tmp_path, workspace_snapshot={"has_tests": False})

    with patch.object(Path, "iterdir", side_effect=AssertionError("Unexpected directory listing")), \
         patch.object(Path, "exists", side_effect=AssertionError("Unexpected project discovery")):
        out = nodes.preflight(state)

    assert out["status"] == "acting"
    assert out["workspace"] == str(tmp_path.resolve())
    assert out["workspace_snapshot"] == {}
    assert llm.text_calls == llm.action_calls == llm.stream_calls == []
    assert store.events[-1]["node_name"] == "preflight"


@pytest.mark.parametrize("target", ["missing", "file.txt"])
def test_preflight_rejects_missing_or_nondirectory_workspace(target) -> None:
    tmp_path = make_workspace()
    (tmp_path / "file.txt").write_text("example", encoding="utf-8")
    nodes, llm, store = make_nodes(tmp_path)
    state = make_state(tmp_path, workspace=str(tmp_path / target))

    out = nodes.preflight(state)

    assert out["status"] == "failed"
    assert out["error"].startswith("preflight failed:")
    assert llm.action_calls == llm.text_calls == []
    assert store.events[-1]["node_name"] == "preflight"


def test_finish_preserves_final_answer_without_llm_call() -> None:
    tmp_path = make_workspace()
    llm = FakeLLM(text_responses=["summary line"])
    set_usage(llm, prompt=1, completion=1)
    nodes, _, _ = make_nodes(tmp_path, llm=llm)
    state = make_state(tmp_path, status="finished", summary="Original answer.\nExact text.")

    out = nodes.finish(state)

    assert out["summary"] == "Original answer.\nExact text."
    assert out["token_usage"]["total_tokens"] == 0
    assert llm.text_calls == []
    assert llm.stream_calls == []


def test_finish_fallback_summary_on_error() -> None:
    tmp_path = make_workspace()
    nodes, llm, _ = make_nodes(tmp_path)
    llm.text_responses = [RuntimeError("summary failed")]
    state = make_state(tmp_path, status="failed", retry_attempts=2, tool_call_count=3,
                       verification_note="none", error="Budget exceeded.")

    out = nodes.finish(state)

    assert "status=failed" in out["summary"]
    assert "retry_attempts=2" in out["summary"]
    assert "Budget exceeded." in out["summary"]
    assert llm.text_calls == []
