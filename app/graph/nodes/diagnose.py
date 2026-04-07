from __future__ import annotations

import re
import time

from app.graph.state import AgentState


class _DiagnoseNode:
    def diagnose(self, state: AgentState) -> AgentState:
        started = time.time()
        failure = str(state.get("error", "") or "")
        lowered = failure.lower()

        failure_type = "unknown"
        hypothesis = "Inspect failing test details before next edit."
        required_next_action = "read_failure_output"

        if "no tests ran" in lowered or "no tests collected" in lowered:
            failure_type = "no_tests"
            hypothesis = "Test discovery did not find runnable tests in current workspace."
            required_next_action = "inspect_workspace"
        elif "modulenotfounderror" in lowered or "importerror" in lowered:
            failure_type = "import_path"
            hypothesis = "Package/module path is inconsistent with tests and workspace layout."
            required_next_action = "python_probe"
        elif " error at setup" in lowered or re.search(r"\b[eE]{4,}\b", failure):
            failure_type = "test_setup"
            hypothesis = "Fixture/setup step failed before assertions; inspect setup path first."
            required_next_action = "read_test_file"
        elif "assert" in lowered or "failed" in lowered:
            failure_type = "assertion"
            hypothesis = "Implementation behavior does not match test assertions."
            required_next_action = "run_test_target"

        signature = self._normalize_failure(failure)
        same_signature = signature and signature == str(state.get("failure_signature", ""))
        same_action = required_next_action == str(state.get("required_next_action", ""))

        state["failure_type"] = failure_type
        state["failure_signature"] = signature
        state["root_cause_hypothesis"] = hypothesis
        state["required_next_action"] = required_next_action
        state["diagnostic_budget_used"] = int(state.get("diagnostic_budget_used", 0)) + 1

        if same_signature and same_action and int(state.get("diagnostic_budget_used", 0)) > 1:
            state["status"] = "failed"
            state["error"] = (
                "Repeated failure with unchanged diagnosis/action; stopping to avoid blind retries."
            )
            self._record(state, "diagnose", started, "Repeated diagnosed failure; stopped.", error=state["error"])
            return state

        state["status"] = "reflecting"
        state["needs_more_action"] = True
        self._record(
            state,
            "diagnose",
            started,
            f"Diagnosed failure_type={failure_type}; next={required_next_action}",
        )
        return state
