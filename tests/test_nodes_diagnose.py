from __future__ import annotations

from pathlib import Path

from app.graph.nodes import AgentNodes
from tests import FakeLLM, FakeStore, FakeTools, make_settings, make_state, make_workspace


def make_nodes(tmp_path: Path):
    llm = FakeLLM()
    store = FakeStore()
    tools = FakeTools()
    settings = make_settings(tmp_path)
    return AgentNodes(llm, store, tools, settings)


def test_diagnose_classifies_import_errors() -> None:
    tmp_path = make_workspace()
    nodes = make_nodes(tmp_path)
    state = make_state(tmp_path, error="ModuleNotFoundError: No module named 'src'")

    out = nodes.diagnose(state)

    assert out["status"] == "reflecting"
    assert out["failure_type"] == "import_path"
    assert out["required_next_action"] == "python_probe"


def test_diagnose_repeated_same_signature_and_action_fails() -> None:
    tmp_path = make_workspace()
    nodes = make_nodes(tmp_path)
    state = make_state(
        tmp_path,
        error="ModuleNotFoundError: No module named 'src'",
        failure_signature="modulenotfounderror: no module named 'src'",
        required_next_action="python_probe",
        diagnostic_budget_used=1,
    )

    out = nodes.diagnose(state)

    assert out["status"] == "failed"
    assert "Repeated failure" in out["error"]
