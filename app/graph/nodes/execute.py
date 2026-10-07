from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

from app.graph.state import AgentState
from app.tools import ToolResult
from app.tools.schemas import validate_tool_args
from app.tools.shell import snapshot
from app.tools.testing import test_outcome


class _ExecuteNode:
    MAX_BATCH_CALLS = 8

    def _accept_batch(self, state: AgentState, calls: list[dict], origin: str, assistant: dict | None = None,
                      *, reserved: bool = False) -> None:
        self._ensure_history(state)
        batch = {"id": uuid.uuid4().hex, "origin": origin, "assistant_message": assistant,
                 "calls": [{**call, "state": "pending"} for call in calls], "index": 0,
                 "results": [], "changed": False, "baseline": None}
        state["pending_batch"] = batch
        state["status"] = "executing"
        state["needs_more_action"] = True
        errors = {}
        if not reserved and not self._reserve_tool_budget(state, len(calls)):
            errors = {i: ("tool_budget", state["stop_reason"]) for i in range(len(calls))}
        elif len(calls) > self.MAX_BATCH_CALLS:
            errors = {i: ("batch_limit", f"At most {self.MAX_BATCH_CALLS} tool requests are allowed per response.")
                      for i in range(len(calls))}
        elif origin == "act":
            available = {schema["function"]["name"] for schema in self.tools.schemas}
            for i, call in enumerate(calls):
                try:
                    if call["tool"] not in available:
                        raise ValueError(f"Tool is not available: {call['tool']}")
                    if call.get("argument_error") or not isinstance(call.get("args"), dict):
                        raise ValueError(call.get("argument_error") or "Tool arguments must be an object.")
                    validate_tool_args(call["tool"], call["args"])
                except ValueError as exc:
                    errors[i] = ("invalid_tool_args", str(exc))
        batch["admission_errors"] = {str(i): pair for i, pair in errors.items()}
        self.store.upsert_state(state["session_id"], state)
        if errors:
            self._reject_admission(state)
            self._finish_batch(state)

    def _reject_admission(self, state: AgentState) -> None:
        batch = state["pending_batch"]
        while batch["index"] < len(batch["calls"]):
            kind, reason = batch["admission_errors"].get(str(batch["index"]),
                ("batch_skipped", "Batch not executed because another request has invalid arguments."))
            self._complete_call(state, ToolResult(False, stderr=reason, exit_code=2,
                                                 execution_status="not_executed", error_kind=kind))

    def _legacy_pending(self, state: AgentState) -> None:
        pending = state.get("pending_action", {})
        if not pending or state.get("pending_batch"):
            return
        if hasattr(self.tools, "canonical"):
            translated = self.tools.canonical(self._make_payload(state, pending["tool"], pending.get("args") or {}))
            pending = {**pending, "tool": translated.name, "args": translated.args}
        self._accept_batch(state, [pending], pending.get("origin", "act"), pending.get("assistant_message"),
                           reserved=pending.get("origin") == "verify" or bool(pending.get("budget_counted")))
        if state.get("pending_batch"):
            state["pending_action"] = {**pending, "batch_id": state["pending_batch"]["id"], "call_index": 0}

    def _baseline(self, state: AgentState, call: dict) -> dict:
        workspace = Path(state["workspace"])
        reader = self.tools.permissions.readable if hasattr(self.tools, "permissions") else lambda p: p.is_relative_to(workspace)
        files, complete = snapshot(workspace, reader)
        if self._is_write_like_tool(call["tool"]):
            target = (workspace / str((call.get("args") or {}).get("path", ""))).resolve()
            complete &= target.is_relative_to(workspace.resolve())
        return {"files": files, "complete": complete}

    def _recover_changes(self, state: AgentState, call: dict, baseline: dict | None) -> ToolResult:
        result = ToolResult(False, stderr="Execution was interrupted; its outcome is unknown. The request was not retried.",
                            exit_code=1, execution_status="indeterminate", error_kind="execution_indeterminate")
        if baseline is None:
            result.snapshot_complete = False
            return result
        try:
            after = self._baseline(state, call)
            before = baseline
            old, new = before["files"], after["files"]
            deleted = {p for p in old.keys() - new.keys()
                       if after["complete"] or not (Path(state["workspace"]) / p).exists()}
            result.changed_files = sorted(p for p in old.keys() | new.keys()
                                          if p in deleted or p in new and old.get(p) != new[p])
            result.deleted_files = sorted(deleted)
            result.snapshot_complete = before["complete"] and after["complete"]
        except Exception:
            result.snapshot_complete = False
        return result

    def execute(self, state: AgentState, *, approval: str = "", fingerprint: str = "") -> AgentState:
        started = time.time()
        self._legacy_pending(state)
        batch = state.get("pending_batch")
        if not batch:
            self._observe_error(state, "queue_error", "No pending tool batch.")
            return state
        if batch["index"] == len(batch["calls"]):
            self._finish_batch(state)
            return state
        if batch["origin"] == "verify":
            call = batch["calls"][batch["index"]]
            if call["state"] == "running":
                result = self._recover_changes(state, call, batch.get("baseline"))
                self._complete_call(state, result)
            self._skip_remaining(state, "Automatic testing policy changed; queued automatic tests were not started.",
                                 kind="auto_test_cancelled")
            self._finish_batch(state)
            self._record(state, "execute", started, "Closed legacy automatic-test queue without executing.")
            return state
        if batch.get("admission_errors"):
            self._reject_admission(state)
            self._finish_batch(state)
            return state
        if batch.get("halted"):
            self._skip_remaining(state, "Earlier request failed or was declined; remaining requests were not executed.")
            self._finish_batch(state)
            return state
        call = batch["calls"][batch["index"]]
        if call["state"] == "running":
            result = self._recover_changes(state, call, batch.get("baseline"))
        else:
            payload = self._make_payload(state, call["tool"], call.get("args") or {})
            permission = self.tools.permission_for(payload)
            call["test_info"] = self._describe_test(state, call, permission)
            if permission.decision == "ask" and not permission.unsupported and not approval:
                self._wait_for_approval(state, call, permission.to_dict())
                self._record(state, "execute", started, "Waiting for operation approval.")
                return state
            batch["baseline"] = None
            if permission.decision != "deny" and not permission.unsupported:
                try:
                    batch["baseline"] = self._baseline(state, call)
                except Exception:
                    pass
            call["state"] = "running"
            call["start_revision"] = state.get("change_revision", 0)
            state["status"] = "executing"
            self.store.upsert_state(state["session_id"], state)
            try:
                result = self.tools.execute(payload, approval=approval, expected_fingerprint=fingerprint)
            except (Exception, KeyboardInterrupt) as exc:
                result = self._recover_changes(state, call, batch["baseline"])
                result.execution_status = "executed"
                result.error_kind = "execution_error" if isinstance(exc, Exception) else "cancelled"
                result.stderr = f"Tool execution error: {exc}"
            if result.execution_status == "needs_approval":
                call["state"] = "pending"
                self._wait_for_approval(state, call, result.permission)
                self._record(state, "execute", started, "Approval must be renewed.")
                return state
            if (not result.ok and result.execution_status == "executed"
                    and self._is_write_like_tool(call["tool"]) and not result.changed_files):
                recovered = self._recover_changes(state, call, batch["baseline"])
                result.changed_files = recovered.changed_files
                result.deleted_files = recovered.deleted_files
                result.snapshot_complete &= recovered.snapshot_complete
        self._complete_call(state, result)
        if not result.ok:
            self._skip_remaining(state, "Earlier request failed or was declined; remaining requests were not executed.")
        if batch["index"] == len(batch["calls"]):
            self._finish_batch(state)
        else:
            state["status"] = "executing"
        self._record(state, "execute", started, "Advanced tool batch.")
        return state

    def _wait_for_approval(self, state: AgentState, call: dict, permission: dict) -> None:
        batch = state["pending_batch"]
        state["pending_action"] = {**call, "origin": batch["origin"], "batch_id": batch["id"],
                                   "call_index": batch["index"], "permission": permission, "budget_counted": True}
        state["approval_unavailable"] = False
        state["status"] = "awaiting_human_confirm"

    def _complete_call(self, state: AgentState, result: ToolResult) -> None:
        import copy
        checkpoint = copy.deepcopy(state)
        batch = state["pending_batch"]
        call = batch["calls"][batch["index"]]
        if not result.ok and not result.error_kind:
            result.error_kind = "verification_error" if batch["origin"] == "verify" else "execution_error"
        modified = self._note_changes(state, call["tool"], call.get("args") or {}, result)
        batch["changed"] |= modified
        record = self._tool_record(call["tool"], call.get("args") or {}, result)
        record.update({"batch_id": batch["id"], "tool_call_id": call.get("tool_call_id"), "origin": batch["origin"]})
        import uuid
        record["result_id"] = record.get("result_id") or uuid.uuid4().hex
        self._record_test_result(state, call, record, f"{batch['id']}:{batch['index']}")
        call["state"] = "completed"
        batch["results"].append(record)
        if not result.ok:
            batch["halted"] = True
        batch["index"] += 1
        batch["baseline"] = None
        state["tool_calls"].append(record)
        state["artifacts"] = list(dict.fromkeys([*state.get("artifacts", []), *result.artifacts]))
        state["last_tool_output"] = self._redact(result.stdout or result.stderr)
        state["pending_action"] = {}
        state["approval_unavailable"] = False
        state["status"] = "executing"
        if result.execution_status in {"executed", "indeterminate"} and not result.ok and batch["origin"] == "act":
            state["execution_errors"] = [*state.get("execution_errors", []), record["stderr"] or record["stdout"]][-5:]
        if result.ok and call["tool"] == "inspect_workspace":
            try:
                observations = json.loads(result.stdout)
                if isinstance(observations, dict):
                    state["workspace_snapshot"] = observations
            except ValueError:
                pass
        facts = state.setdefault("session_facts", {"file_versions": {}, "errors": [], "denials": []})
        if not result.ok:
            evidence = {"result_id": record["result_id"], "tool": call["tool"], "error_kind": result.error_kind,
                        "status": result.execution_status, "turn_id": state.get("active_turn_id"),
                        "note": "Historical evidence; resolution is not inferred from unrelated success."}
            if result.execution_status in {"denied", "rejected"}:
                facts.setdefault("denials", []).append(evidence)
            elif result.execution_status != "not_executed":
                facts.setdefault("errors", []).append(evidence)
        import hashlib
        for path in result.changed_files:
            target = (Path(state["workspace"]) / path).resolve()
            try:
                stamp = hashlib.sha256(target.read_bytes()).hexdigest() if target.is_file() else None
            except OSError:
                stamp = "unknown"
            facts.setdefault("file_versions", {})[str(target)] = stamp
        record["_archive"] = self._redact(result.archive_streams or {"stdout": result.stdout, "stderr": result.stderr})
        if result.ok and call["tool"] == "read_file":
            entry = {**record.get("output_meta", {}), "result_id": record["result_id"], "freshness": "current", "location": "context"}
            for previous in state.get("file_read_index", []):
                if previous.get("path") == entry.get("path") and previous.get("version") != entry.get("version"):
                    previous["freshness"] = "stale"
            index = state.setdefault("file_read_index", [])
            identity = ("path", "version", "line_start", "column_start", "line_end", "column_end")
            duplicate = next((reading for reading in index if all(reading.get(k) == entry.get(k) for k in identity)), None)
            if duplicate is not None:
                entry["previous_reads"] = duplicate.get("previous_reads", 0) + 1
                duplicate.update(entry)
            else:
                index.append(entry)
            facts.setdefault("file_versions", {})[entry.get("path", "")] = entry.get("version", "unknown")
        try:
            self.store.save_action_result(state["session_id"], state, "execute", record)
            record.pop("_archive", None)
        except Exception:
            record.pop("_archive", None)
            state.clear()
            state.update(checkpoint)
            state["status"] = "awaiting_context"
            state["context_error"] = "Tool completed but result persistence failed. Do not replay; resume will recover the running checkpoint as indeterminate."
            from app.context.manager import ContextPaused
            error = ContextPaused(state["context_error"])
            error.checkpoint = state
            raise error

    def _skip_remaining(self, state: AgentState, reason: str, *, kind: str = "batch_skipped") -> None:
        batch = state["pending_batch"]
        while batch["index"] < len(batch["calls"]):
            self._complete_call(state, ToolResult(False, stderr=reason, exit_code=0,
                                                 execution_status="not_executed", error_kind=kind))

    def _finish_batch(self, state: AgentState) -> None:
        batch = state["pending_batch"]
        results = batch["results"]
        from app.context.memory import append_group, migrate
        migrate(state)
        group = {"type": "tool_batch" if batch["assistant_message"] else "tool_observation",
                 "record_id": batch["id"], "assistant_message": batch["assistant_message"], "results": results}
        state["action_history"].append(group)
        append_group(state, group)
        state["pending_batch"] = {}
        state["pending_action"] = {}
        errors = [r for r in results if not r.get("ok", True)
                  and r.get("error_kind") not in {"batch_skipped", "auto_test_cancelled"}]
        if batch["origin"] == "verify":
            for index, record in enumerate(results):
                self._record_test_result(state, {"tool": record["name"], "args": record.get("args", {})},
                                         record, f"{batch['id']}:{index}")
            if state.get("tool_call_count", 0) >= self.settings.max_tool_calls:
                self._request_stop(state, f"Reached MAX_TOOL_CALLS={self.settings.max_tool_calls}; legacy automatic-test queue closed.")
            if state.get("retry_attempts", 0) >= self.settings.max_retry_steps:
                self._request_stop(state, f"Reached MAX_RETRY_STEPS={self.settings.max_retry_steps}; legacy automatic-test queue closed.")
        if errors:
            state["error"] = errors[0].get("stderr") or errors[0].get("stdout", "")
            identity = {"cwd": state["workspace"], "calls": [
                {"tool": c["tool"], "args": c.get("args"),
                 **({"raw_arguments": c.get("raw_arguments")} if not isinstance(c.get("args"), dict) else {})}
                for c in batch["calls"]]}
            self._register_failure(state, identity, errors)
        elif batch["origin"] == "act":
            state["recent_failures"] = []
        if batch["changed"] or batch["origin"] == "verify" and state.get("changed_files"):
            state["verification_note"] = "File changes pending verification."
            state["status"] = "verifying"
        else:
            self._return_to_model(state)
        self.store.upsert_state(state["session_id"], state)

    def _describe_test(self, state: AgentState, call: dict, permission=None) -> dict:
        if not hasattr(self.tools, "describe_test") or not isinstance(call.get("args"), dict):
            return {}
        try:
            validate_tool_args(call["tool"], call["args"])
            return self.tools.describe_test(self._make_payload(state, call["tool"], call["args"]), permission)
        except (OSError, ValueError, KeyError):
            return {}

    def _record_test_result(self, state: AgentState, call: dict, record: dict, result_id: str) -> None:
        if any(result.get("id") == result_id for result in state.get("test_results", [])):
            return
        info = record.get("test_info") or call.get("test_info") or self._describe_test(state, call)
        if not info:
            if record.get("origin") != "verify":
                return
            info = {"command": record.get("args", {}).get("command", ""), "cwd": state["workspace"],
                    "runner": "legacy", "direct": False, "executables": [], "scope": {"kind": "unknown"}}
        if not info.get("executables"):
            info = {**info, "executables": record.get("permission", {}).get("executables", [])}
        status = test_outcome(info, record)
        revision = call.get("start_revision")
        freshness = "unknown" if revision is None or status == "not_run" else (
            "current" if revision == state.get("change_revision", 0) else "stale")
        result = self._redact({**info, "id": result_id, "result_id": record.get("result_id", ""), "status": status, "freshness": freshness,
                              "revision": revision, "exit_code": record.get("exit_code"),
                              "execution_status": record.get("execution_status", "executed"),
                              "stdout": record.get("stdout", ""), "stderr": record.get("stderr", ""),
                              "origin": record.get("origin", "act")})
        if status == "unknown":
            reason = "Test conclusion is unknown; opaque or incomplete execution evidence does not establish a test result."
        elif status in {"not_run", "failed", "interrupted"}:
            reason = record.get("stderr") or record.get("stdout", "")
        else:
            reason = ""
        result["reason"] = self._redact(reason)
        state.setdefault("test_results", []).append(result)
        record["test_result"] = result

    def _note_changes(self, state: AgentState, name: str, args: dict, result: ToolResult) -> bool:
        paths = result.changed_files
        if not paths and result.ok and self._is_write_like_tool(name):
            paths = [str(args.get("path", ""))]
        paths = [p for p in paths if p]
        if not result.snapshot_complete:
            state["snapshot_complete"] = False
            state["verification_note"] = "Workspace change scan incomplete; full verification cannot be established."
        if not paths:
            return False
        state["change_revision"] = int(state.get("change_revision", 0)) + 1
        for reading in state.get("file_read_index", []):
            if any(Path(reading.get("path", "")) == (Path(state["workspace"]) / p).resolve() for p in paths):
                reading["freshness"] = "stale"
        records = [*state.get("test_results", []),
                   *[r.get("test_result", {}) for r in state.get("tool_calls", [])],
                   *[r.get("test_result", {}) for r in state.get("pending_batch", {}).get("results", [])],
                   *[r.get("test_result", {}) for group in state.get("action_history", []) for r in group.get("results", [])]]
        records.extend(r.get("test_result", {}) for group in state.get("session_history", []) for r in group.get("results", []))
        for record in records:
            if record.get("revision") is not None and record.get("status") != "not_run":
                record["freshness"] = "stale"
        state["changed_files"] = list(dict.fromkeys([*state.get("changed_files", []), *paths]))
        if self._is_write_like_tool(name) and result.execution_status in {"executed", "indeterminate"}:
            state["authorized_changed_files"] = list(dict.fromkeys([*state.get("authorized_changed_files", []),
                *[str((Path(state["workspace"]) / p).resolve()) for p in paths]]))
        deleted = set(state.get("deleted_files", [])) - set(paths)
        deleted.update(result.deleted_files or (paths if name == "delete_file" else []))
        state["deleted_files"] = sorted(deleted)
        state["write_count"] = int(state.get("write_count", 0)) + 1
        state["turn_progress"] = "modified"
        return True

    def human_confirm(self, state: AgentState) -> AgentState:
        started = time.time()
        self._legacy_pending(state)
        pending = state.get("pending_action")
        if not pending:
            self._observe_error(state, "queue_error", "No pending action for human_confirm.")
            return state
        batch = state["pending_batch"]
        if batch["origin"] == "verify":
            return self.execute(state)
        call = batch["calls"][batch["index"]]
        payload = self._make_payload(state, call["tool"], call.get("args") or {})
        permission = self.tools.permission_for(payload)
        state["approval_unavailable"] = False
        if permission.decision != "ask" or permission.unsupported:
            return self.execute(state)
        pending["permission"] = permission.to_dict()
        try:
            answer = self.confirm_fn(call["tool"], {**(call.get("args") or {}), "_permission": permission.to_dict(),
                                                    "_workspace": state["workspace"]}) if self.confirm_fn else "unavailable"
        except (KeyboardInterrupt, EOFError):
            answer = "reject"
        if isinstance(answer, bool):
            answer = "approve_once" if answer else "reject"
        if answer == "unavailable":
            state["approval_unavailable"] = True
            state["status"] = "awaiting_human_confirm"
            self._record(state, "human_confirm", started, "Approval unavailable; paused without executing.")
            return state
        if answer not in {"approve_once", "approve_session"} or answer == "approve_session" and not permission.reusable:
            self.tools.record_permission(payload, permission, "rejected")
            self._complete_call(state, ToolResult(False, stderr=f"User declined {call['tool']}; operation not executed.",
                                                 exit_code=13, execution_status="rejected", error_kind="permission_rejected",
                                                 permission=permission.to_dict()))
            self._skip_remaining(state, "Batch stopped after user rejection.")
            self._finish_batch(state)
            self._record(state, "human_confirm", started, "Request rejected; batch closed.")
            return state
        return self.execute(state, approval=answer, fingerprint=permission.fingerprint)
