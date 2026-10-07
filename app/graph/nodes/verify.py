from __future__ import annotations

import time

from app.graph.state import AgentState


class _VerifyNode:
    def verify(self, state: AgentState) -> AgentState:
        started = time.time()
        try:
            check = self._collect_static_check(state)
        except (OSError, ValueError) as exc:
            check = {"status": "failed", "checked_files": list(state.get("changed_files", [])),
                     "checks": [], "errors": [f"Static check error: {exc}"], "snapshot_complete": False,
                     "revision": state.get("change_revision", 0)}
        check = self._redact(check)
        previous = state.get("static_check", {})
        state["static_check"] = check
        state.setdefault("session_facts", {})["latest_static_check"] = {**check, "turn_id": state.get("active_turn_id"), "historical": False}
        self._append_observation(state, {"kind": "static_check", **check,
                                        "note": "Static checks do not establish a test result."})
        if check["status"] == "failed":
            state["verification_note"] = "Static validation failed: " + "; ".join(check["errors"])
            if previous != check and not state.get("stop_reason"):
                state["error"] = "; ".join(check["errors"])
                self._register_failure(state, {"kind": "static_check", "files": check["checked_files"]},
                                       [{"error_kind": "static_check_error", "stderr": state["error"], "exit_code": None}])
        elif check["status"] == "incomplete":
            state["verification_note"] = "Static checks passed for recorded files; workspace change scan incomplete. Tests are model-selected."
        else:
            state["verification_note"] = "Static checks passed; tests are model-selected, not automatically executed."
        self._return_to_model(state)
        self._record(state, "verify", started, state["verification_note"],
                     error="; ".join(check["errors"]))
        return state
