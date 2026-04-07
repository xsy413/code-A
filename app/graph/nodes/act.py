from __future__ import annotations

import json
import time
from pathlib import Path

from app.graph.state import AgentState
from app.prompts import ACTION_PROMPT, SYSTEM_PROMPT


class _ActNode:
    def _tool_matches_required_action(self, tool_name: str, required_next_action: str) -> bool:
        if not required_next_action:
            return True
        mapping: dict[str, set[str]] = {
            "inspect_workspace": {"inspect_workspace", "read_file", "list_files"},
            "python_probe": {"python_probe", "read_file", "search_text"},
            "run_test_target": {"run_test_target", "read_file"},
            "read_test_file": {"read_file", "search_text"},
            "read_failure_output": {"read_file", "search_text"},
        }
        allowed = mapping.get(required_next_action, {required_next_action})
        return tool_name in allowed

    def act(self, state: AgentState) -> AgentState:
        started = time.time()
        try:
            prompt = ACTION_PROMPT.format(
                current_request=self._current_request(state),
                plan=state.get("plan", ""),
                workspace_snapshot=state.get("workspace_snapshot", {}),
                required_next_action=state.get("required_next_action", ""),
                plan_constraints=(
                    f"failure_type={state.get('failure_type', '')}; "
                    f"hypothesis={state.get('root_cause_hypothesis', '')}"
                ),
                conversation_summary=self._conversation_summary(state),
                recent_turns=self._recent_turns_text(state),
                last_tool_output=state.get("last_tool_output", ""),
                reflection=state.get("last_reflection", ""),
            )
            if self.stream_fn:
                tc = state.get("tool_call_count", 0)
                self.stream_fn(f"\n\u001b[2m[act #{tc + 1}] deciding...\u001b[0m\n")
            decision = self.llm.complete_json(
                SYSTEM_PROMPT,
                prompt,
                history=self._build_action_history(state),
            )
            self._accumulate_usage(state, "act")
            tool_name = str(decision.get("tool", "finish"))
            args = decision.get("args", {})

            self.store.add_tool_call(
                state["session_id"],
                "act_decision",
                tool_name,
                args if isinstance(args, dict) else {},
                {"ok": None, "phase": "planned"},
            )

            required_next_action = str(state.get("required_next_action", "") or "")
            if required_next_action and not self._tool_matches_required_action(tool_name, required_next_action):
                state["status"] = "reflecting"
                state["needs_more_action"] = True
                state["error"] = (
                    f"diagnostic_gate: required_next_action={required_next_action}, got={tool_name}. "
                    "Satisfy diagnosis before editing again."
                )
                self._record(state, "act", started, "Blocked by diagnostic gate.", error=state["error"])
                return state

            if tool_name == "write_file":
                path = str(args.get("path", ""))
                cwd = Path(state["workspace"])
                if self.tools.is_risky_write(cwd, path) and not self.settings.auto_confirm_risky_writes:
                    state["pending_action"] = decision
                    state["status"] = "awaiting_human_confirm"
                    self._record(state, "act", started, f"Pending human confirmation for write: {path}")
                    return state

            if not self._bump_tool_budget(state):
                self._record(state, "act", started, "Tool budget exceeded.", error=state["error"])
                return state

            if tool_name == "finish":
                changed_files = args.get("changed_files") or state.get("changed_files", [])
                completion_reason = str(args.get("completion_reason", "")).strip()
                if isinstance(changed_files, str):
                    changed_files = [changed_files]
                if not isinstance(changed_files, list):
                    changed_files = []

                validation_error = self._validate_finish(
                    state,
                    [str(p) for p in changed_files],
                    completion_reason,
                )
                if validation_error:
                    state["status"] = "reflecting"
                    state["error"] = validation_error
                    state["needs_more_action"] = True
                    self._record(state, "act", started, "Finish self-check failed.", error=state["error"])
                    return state

                payload = self._make_payload(state, tool_name, args)
                result = self.tools.execute(payload)
                call = {
                    "name": tool_name,
                    "args": args,
                    "ok": result.ok,
                    "stdout": result.stdout[:2000],
                    "stderr": result.stderr[:2000],
                    "exit_code": result.exit_code,
                }
                state["tool_calls"].append(call)
                self.store.add_tool_call(state["session_id"], "act", tool_name, args, call)

                state["finish_reason"] = completion_reason
                state["changed_files"] = [str(p) for p in changed_files]
                state["needs_more_action"] = False
                state["status"] = "finished"
                self._record(state, "act", started, "Executed finish with self-check.")
                return state

            payload = self._make_payload(state, tool_name, args)
            result = self.tools.execute(payload)
            call = {
                "name": tool_name,
                "args": args,
                "ok": result.ok,
                "stdout": result.stdout[:2000],
                "stderr": result.stderr[:2000],
                "exit_code": result.exit_code,
                "llm_decision": decision,
            }
            state["tool_calls"].append(call)
            state["artifacts"] = list({*state.get("artifacts", []), *result.artifacts})
            state["last_tool_output"] = (result.stdout or result.stderr)[:2000]
            self.store.add_tool_call(state["session_id"], "act", tool_name, args, call)

            if not result.ok:
                state["status"] = "diagnosing"
                state["needs_more_action"] = True
                state["error"] = (result.stderr or result.stdout or f"tool failed: {tool_name}")[:2000]
                self._record(state, "act", started, f"Tool failed: {tool_name}", error=state["error"])
                return state

            if tool_name == "inspect_workspace":
                try:
                    snapshot = json.loads(result.stdout)
                    if isinstance(snapshot, dict):
                        state["workspace_snapshot"] = snapshot
                except Exception:
                    pass

            if tool_name in {"write_file", "patch_file", "delete_file"}:
                state["write_count"] = int(state.get("write_count", 0)) + 1
                path = str(args.get("path", "")).strip()
                if path:
                    changed = list(state.get("changed_files", []))
                    if path not in changed:
                        changed.append(path)
                    state["changed_files"] = changed

                if tool_name == "delete_file" and path:
                    deleted = list(state.get("deleted_files", []))
                    if path not in deleted:
                        deleted.append(path)
                    state["deleted_files"] = deleted

                state["turn_progress"] = "modified"
                state["explore_streak"] = 0
                state["required_next_action"] = ""
                state["needs_more_action"] = False
                state["status"] = "verifying"
                self._record(state, "act", started, f"Executed tool: {tool_name}; progress=modified")
                return state

            if tool_name == "run_command":
                state["explore_streak"] = 0
                state["needs_more_action"] = True
                state["status"] = "acting"
                self._record(state, "act", started, "Executed run_command; continue acting")
                return state

            if self._is_exploration_tool(tool_name):
                current_progress = str(state.get("turn_progress", "none"))
                if current_progress != "modified":
                    state["turn_progress"] = "explored"

                streak = int(state.get("explore_streak", 0)) + 1
                state["explore_streak"] = streak
                state["needs_more_action"] = True

                if required_next_action and self._tool_matches_required_action(tool_name, required_next_action):
                    state["required_next_action"] = ""

                if int(state.get("write_count", 0)) == 0 and streak > self.settings.max_explore_steps_before_write:
                    state["status"] = "reflecting"
                    state["error"] = (
                        "no_code_change_yet: too many exploration steps without write_file. "
                        "Create/modify code or finish with valid evidence."
                    )
                    self._record(state, "act", started, "Exploration threshold exceeded.", error=state["error"])
                    return state

                state["status"] = "acting"
                self._record(state, "act", started, f"Executed exploration tool: {tool_name}; continue acting")
                return state

            state["needs_more_action"] = True
            state["status"] = "acting"
            self._record(state, "act", started, f"Executed tool: {tool_name}; continue acting")
            return state
        except Exception as exc:
            state["status"] = "diagnosing"
            state["needs_more_action"] = True
            state["error"] = f"act failed: {exc}"
            self._record(state, "act", started, "Action failed.", error=state["error"])
            return state

    def human_confirm(self, state: AgentState) -> AgentState:
        started = time.time()
        pending = state.get("pending_action") or {}
        if not pending:
            state["status"] = "failed"
            state["error"] = "No pending action for human_confirm."
            self._record(state, "human_confirm", started, "Missing pending action.", error=state["error"])
            return state

        tool_name = str(pending.get("tool", ""))
        args = pending.get("args", {}) or {}

        if self.confirm_fn is not None:
            try:
                approved = self.confirm_fn(tool_name, args)
            except (KeyboardInterrupt, EOFError):
                approved = False
        else:
            approved = self.settings.auto_confirm_risky_writes

        if not approved:
            state["pending_action"] = {}
            state["status"] = "reflecting"
            state["needs_more_action"] = True
            state["error"] = (
                f"User declined {tool_name} on {args.get('path', '?')}. "
                "Find an alternative approach that does not require overwriting existing files, "
                "or finish with what's already done."
            )
            self._record(state, "human_confirm", started, "User declined.", error=state["error"])
            return state

        if not self._bump_tool_budget(state):
            self._record(state, "human_confirm", started, "Tool budget exceeded.", error=state["error"])
            return state

        payload = self._make_payload(state, str(pending.get("tool", "")), pending.get("args", {}))
        result = self.tools.execute(payload)
        call = {
            "name": payload.name,
            "args": payload.args,
            "ok": result.ok,
            "stdout": result.stdout[:2000],
            "stderr": result.stderr[:2000],
            "exit_code": result.exit_code,
        }
        state["tool_calls"].append(call)
        state["pending_action"] = {}
        self.store.add_tool_call(state["session_id"], "human_confirm", payload.name, payload.args, call)

        if payload.name == "write_file" and result.ok:
            state["write_count"] = int(state.get("write_count", 0)) + 1
            path = str(payload.args.get("path", "")).strip()
            if path:
                changed = list(state.get("changed_files", []))
                if path not in changed:
                    changed.append(path)
                state["changed_files"] = changed
            state["turn_progress"] = "modified"
            state["explore_streak"] = 0

        if result.ok:
            if payload.name == "write_file":
                state["needs_more_action"] = False
                state["status"] = "verifying"
            else:
                state["needs_more_action"] = True
                state["status"] = "acting"
            self._record(state, "human_confirm", started, f"Approved and executed: {payload.name}")
        else:
            state["status"] = "diagnosing"
            state["needs_more_action"] = True
            state["error"] = (result.stderr or result.stdout or "human_confirm tool failed")[:2000]
            self._record(state, "human_confirm", started, "Approved action failed.", error=state["error"])
        return state
