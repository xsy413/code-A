from __future__ import annotations

import time
from pathlib import Path

from app.graph.state import AgentState


class _VerifyNode:
    def verify(self, state: AgentState) -> AgentState:
        started = time.time()
        try:
            if str(state.get("turn_progress", "none")) != "modified" and int(state.get("write_count", 0)) == 0:
                state["status"] = "diagnosing"
                state["needs_more_action"] = True
                state["error"] = "no_code_change_yet: verification requires a successful write_file or explicit finish."
                self._record(state, "verify", started, "Skipped verify due to no code change.", error=state["error"])
                return state

            workspace = Path(state["workspace"]).resolve()
            verify_mode = self.settings.verify_mode if self.settings.verify_mode in {"auto", "required"} else "auto"
            state["verification_mode"] = verify_mode
            has_tests = self._has_test_entry(workspace)

            if verify_mode == "auto" and not has_tests:
                if self.settings.allow_finish_without_tests and int(state.get("write_count", 0)) > 0:
                    state["verification_note"] = "No test entry found; accepted completion with static self-check only."
                    state["needs_more_action"] = False
                    state["status"] = "finished"
                    self._record(state, "verify", started, "No tests found; accepted completion in auto mode.")
                    return state

                if int(state.get("write_count", 0)) == 0:
                    state["status"] = "diagnosing"
                    state["needs_more_action"] = True
                    state["error"] = "no_code_change_yet: no file changes detected; cannot verify."
                    self._record(state, "verify", started, "No code changes to verify.", error=state["error"])
                    return state

                state["status"] = "diagnosing"
                state["needs_more_action"] = True
                state["error"] = "no_tests_available: completion without tests is disabled."
                self._record(state, "verify", started, "No tests found and verification is required.", error=state["error"])
                return state

            if not self._bump_tool_budget(state):
                self._record(state, "verify", started, "Tool budget exceeded.", error=state["error"])
                return state

            target_test = str(state.get("target_test", "") or "").strip()
            tool_name = "run_test_target" if target_test else "run_tests"
            tool_args = {"target": target_test} if target_test else {}

            payload = self._make_payload(state, tool_name, tool_args)
            result = self.tools.execute(payload)
            call = {
                "name": tool_name,
                "args": tool_args,
                "ok": result.ok,
                "stdout": result.stdout[:2000],
                "stderr": result.stderr[:2000],
                "exit_code": result.exit_code,
            }
            state["tool_calls"].append(call)
            state["last_tool_output"] = (result.stdout or result.stderr)[:2000]
            self.store.add_tool_call(state["session_id"], "verify", tool_name, tool_args, call)

            if result.ok:
                state["verification_note"] = "Tests passed."
                state["needs_more_action"] = False
                state["status"] = "finished"
                self._record(state, "verify", started, "Tests passed.")
                return state

            output = (result.stderr or result.stdout or "").lower()
            if (
                verify_mode == "auto"
                and self.settings.allow_finish_without_tests
                and result.exit_code == 5
                and "no tests collected" in output
            ):
                state["verification_note"] = "No tests collected (pytest exit code 5); accepted completion in auto mode."
                state["needs_more_action"] = False
                state["status"] = "finished"
                self._record(state, "verify", started, "No tests collected; accepted completion.")
                return state

            state["status"] = "diagnosing"
            state["needs_more_action"] = True
            state["error"] = (result.stderr or result.stdout or "tests failed")[:2000]
            self._record(state, "verify", started, "Tests failed.", error=state["error"])
            return state
        except Exception as exc:
            state["status"] = "diagnosing"
            state["needs_more_action"] = True
            state["error"] = f"verify failed: {exc}"
            self._record(state, "verify", started, "Verify crashed.", error=state["error"])
            return state
