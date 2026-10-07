from __future__ import annotations

import json
import shlex
import shutil
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from app.sandbox import SandboxPolicy
from app.tools.filesystem import (
    delete_file,
    list_files,
    patch_file,
    read_file,
    search_text,
    write_file,
)
from app.tools.protocol import ToolInput, ToolResult
from app.tools.shell import run_shell, snapshot
from app.context.config import ContextConfig
from app.context.results import present, read_archive, cursor_decode, query_key

if TYPE_CHECKING:
    from app.permissions import PermissionEngine


class ToolExecutor:
    def __init__(self, policy: SandboxPolicy, test_command: str = "pytest -q", *, permissions: PermissionEngine | None = None, audit=None) -> None:
        from app.permissions import PermissionEngine

        self.policy = policy
        self.test_command = test_command
        self.permissions = permissions or PermissionEngine(policy.workspace)
        self.policy.readable = self.permissions.readable
        self.audit = audit
        self.store = None
        self.context = ContextConfig()

    @property
    def schemas(self) -> list[dict]:
        from app.tools.schemas import tool_schemas
        return tool_schemas(self.permissions.shells)

    @property
    def default_shell(self) -> str:
        return "powershell" if "powershell" in self.permissions.shells else "bash"

    def test_action(self, target: str = "") -> tuple[str, dict]:
        command = self.test_command
        if target:
            quote = ("'" + target.replace("'", "''") + "'") if self.default_shell == "powershell" else shlex.quote(target)
            command = f"pytest -q {quote}"
        if command.startswith("pytest ") and shutil.which("pytest") is None:
            quote = "'" + sys.executable.replace("'", "''") + "'" if self.default_shell == "powershell" else shlex.quote(sys.executable)
            command = ("& " if self.default_shell == "powershell" else "") + quote + " -m " + command
        return self.default_shell, {"command": command}

    def canonical(self, payload: ToolInput) -> ToolInput:
        name, args = payload.name, dict(payload.args)
        if name in {"run_tests", "run_test_target"}:
            name, translated = self.test_action(str(args.get("target", "")))
            args = {**translated, **({"timeout_s": args["timeout_s"]} if "timeout_s" in args else {})}
        elif name == "run_command":
            name = self.default_shell
        elif name == "python_probe":
            code = str(args.get("code", ""))
            quote = "'" + code.replace("'", "''") + "'" if self.default_shell == "powershell" else shlex.quote(code)
            name, args = self.default_shell, {"command": f"python -c {quote}", "timeout_s": args.get("timeout_s", 20)}
        cwd = self.permissions.path(str(args.get("cwd", payload.cwd)), Path(payload.cwd))
        return ToolInput(name, args, str(cwd), payload.backup_dir, payload.session_id)

    def permission_for(self, payload: ToolInput):
        action = self.canonical(payload)
        args = dict(action.args)
        if action.name == "read_tool_result" or args.get("cursor"):
            try:
                ref = args.get("result_id") or cursor_decode(args["cursor"])["result_id"]
                saved = self.store.get_result(action.session_id, ref) if self.store else None
            except (ValueError, KeyError):
                saved = None
            if not saved:
                from app.permissions import PermissionDecision
                return PermissionDecision("deny", "result_not_found", "Result is not a completed result in this session.")
            if saved["name"] == "read_file":
                args["_source_path"] = saved.get("output_meta", {}).get("path", "")
            if saved["name"] in {"list_files", "search_text"}:
                # Directory/search archives must still satisfy current path rules.
                for chunk in saved["chunks"]:
                    if chunk["stream"] == "stdout":
                        for line in chunk["body"].splitlines():
                            path = line.split(":", 1)[0] if saved["name"] == "search_text" else line
                            if path and not self.permissions.readable(Path(saved.get("output_meta", {}).get("cwd", action.cwd)) / path):
                                from app.permissions import PermissionDecision
                                return PermissionDecision("deny", "archive_path_denied", "Archived source is no longer readable under current policy.")
        decision = self.permissions.evaluate(action.name, args, Path(action.cwd), action.session_id)
        if args.get("_source_path"):
            source = self.permissions.evaluate("read_file", {"path": args["_source_path"]}, Path(action.cwd), action.session_id)
            priority = {"allow": 0, "ask": 1, "deny": 2}
            if priority[source.decision] > priority[decision.decision]:
                return source
        return decision

    def describe_test(self, payload: ToolInput, decision=None) -> dict:
        from app.tools.testing import describe_test

        action = self.canonical(payload)
        if action.name not in self.permissions.shells:
            return {}
        analysis = self.permissions._analyze(action.name, str(action.args.get("command", "")))
        return describe_test(analysis, str(action.args.get("command", "")), action.cwd,
                             decision.executables if decision else [])

    def _audit(self, payload: ToolInput, decision, outcome: str, approval: str = "") -> None:
        if self.audit:
            self.audit(payload.session_id, self.permissions.redact({"tool": payload.name, "args": payload.args,
                       "cwd": payload.cwd, "decision": decision.to_dict(), "outcome": outcome, "approval": approval}))

    def record_permission(self, payload: ToolInput, decision, outcome: str) -> None:
        self._audit(payload, decision, outcome)

    def execute(self, payload: ToolInput, *, approval: str = "", expected_fingerprint: str = "") -> ToolResult:
        action = self.canonical(payload)
        decision = self.permission_for(action)
        if decision.decision == "deny":
            result = ToolResult(False, stderr=f"Permission denied: {decision.rule_id}: {decision.reason}", exit_code=13,
                                execution_status="denied", error_kind="permission_denied")
        elif expected_fingerprint and decision.fingerprint != expected_fingerprint:
            result = ToolResult(False, stderr="Approval invalidated; operation changed. Please approve again.", exit_code=13,
                                execution_status="needs_approval", error_kind="approval_changed")
        elif decision.unsupported:
            result = ToolResult(False, stderr="Background execution is not supported in this version.", exit_code=2,
                                execution_status="not_executed", error_kind="unsupported_background")
        elif decision.decision == "ask" and approval not in {"approve_once", "approve_session"}:
            result = ToolResult(False, stderr=f"Approval required: {decision.rule_id}: {decision.reason}", exit_code=13,
                                execution_status="needs_approval", error_kind="needs_approval")
        else:
            if approval == "approve_session" and decision.decision == "ask":
                self.permissions.approve_session(action.session_id, decision)
            try:
                if action.name in {"bash", "powershell"}:
                    timeout = action.args.get("timeout_s", 60)
                    if type(timeout) is not int or not 1 <= timeout <= 600 or not str(action.args.get("command", "")).strip():
                        raise ValueError("Shell requires a nonempty command and timeout_s between 1 and 600")
                    before, complete_before = snapshot(self.policy.workspace, self.permissions.readable)
                    try:
                        command = self.permissions.execution_command(action.name, str(action.args["command"]), Path(action.cwd))
                        result = self._run_shell(action, command, timeout)
                    except OSError as exc:
                        result = ToolResult(False, stderr=f"Shell startup failed: {exc}", exit_code=1,
                                            execution_status="not_executed", error_kind="execution_error")
                    finally:
                        after, complete_after = snapshot(self.policy.workspace, self.permissions.readable)
                    deleted = {p for p in before.keys() - after.keys() if complete_after or not (self.policy.workspace / p).exists()}
                    result.changed_files = sorted(p for p in before.keys() | after.keys() if (p in after and before.get(p) != after[p]) or p in deleted)
                    result.deleted_files = sorted(deleted)
                    result.snapshot_complete = complete_before and complete_after
                else:
                    self.policy.authorized_paths = {self.permissions.path(str(action.args["path"]), Path(action.cwd))} if "path" in action.args else set()
                    try:
                        result = self._execute_raw(action)
                    finally:
                        self.policy.authorized_paths = set()
                    if result.ok and action.name in {"write_file", "patch_file", "delete_file"}:
                        path = self.permissions.path(str(action.args["path"]), Path(action.cwd))
                        path = str(path.relative_to(self.policy.workspace)) if path.is_relative_to(self.policy.workspace) else str(path)
                        result.changed_files = [path]
                        result.deleted_files = [path] if action.name == "delete_file" else []
                        target = self.permissions.path(str(action.args["path"]), Path(action.cwd))
                        import hashlib
                        result.output_meta.update({"path": str(target), "version": hashlib.sha256(target.read_bytes()).hexdigest() if target.is_file() else None})
                result.is_test = decision.is_test
            except (OSError, ValueError) as exc:
                result = ToolResult(False, stderr=f"Execution error: {exc}", exit_code=1, error_kind="execution_error")
        result.permission = self.permissions.redact(decision.to_dict())
        result.test_info = self.permissions.redact(self.describe_test(action, decision))
        result.is_test = bool(result.test_info.get("direct"))
        result.stdout, result.stderr = self.permissions.redact(result.stdout), self.permissions.redact(result.stderr)
        result.archive_streams = self.permissions.redact(result.archive_streams)
        result.output_meta = self.permissions.redact(result.output_meta)
        present(result, action.name, action.args, action.cwd, self.context)
        self._audit(action, decision, result.execution_status, approval)
        return result

    def _run_shell(self, action: ToolInput, command: str, timeout: int) -> ToolResult:
        return run_shell(self.permissions.shells[action.name], action.name, command, Path(action.cwd), timeout)

    def is_risky_write(self, cwd: Path, path: str) -> bool:
        target = (cwd / path).resolve()
        critical_names = {".env", "pyproject.toml"}
        if target.name in critical_names:
            return True
        return False

    def _inspect_workspace(self, cwd: Path) -> ToolResult:
        top_entries = sorted([p.name for p in cwd.iterdir() if not p.name.startswith(".") and self.permissions.readable(p)])
        source_roots = [name for name in ("src", "app") if (cwd / name).is_dir()]
        payload = {
            "top_entries": top_entries,
            "has_tests": (cwd / "tests").exists(),
            "has_pyproject": (cwd / "pyproject.toml").exists(),
            "has_requirements": (cwd / "requirements.txt").exists(),
            "source_roots": source_roots,
        }
        return ToolResult(ok=True, stdout=json.dumps(payload, ensure_ascii=False), artifacts=[])

    def _execute_raw(self, payload: ToolInput) -> ToolResult:
        cwd = Path(payload.cwd).resolve()
        name = payload.name
        args = payload.args
        backup_dir = Path(payload.backup_dir).resolve() if payload.backup_dir else None

        if name == "read_tool_result":
            return read_archive(self.store, payload.session_id, args, self.context)
        if name in {"list_files", "search_text"} and args.get("cursor"):
            cursor = cursor_decode(args["cursor"])
            saved = self.store.get_result(payload.session_id, cursor["result_id"])
            if not saved or cursor["query"] != query_key(name, args, payload.cwd) or cursor["version"] != saved["output_meta"].get("version"):
                raise ValueError("Cursor does not match the original query and result version.")
            if not saved["output_meta"].get("archive_complete"):
                raise ValueError("Result archive is incomplete; this cursor cannot reconstruct missing entries.")
            text = "".join(c["body"] for c in saved["chunks"] if c["stream"] == "stdout")
            result = ToolResult(True, stdout=text, archive_streams={"stdout": text})
            present(result, name, args, payload.cwd, self.context, continuation=cursor)
            result.output_meta["presented"] = True
            return result

        if name == "inspect_workspace":
            self.policy.validate_path(cwd)
            return self._inspect_workspace(cwd)

        if name == "list_files":
            pattern = str(args.get("pattern", "*"))
            return list_files(self.policy, cwd, pattern=pattern)

        if name == "read_file":
            path = str(args.get("path", ""))
            line_start = args.get("line_start")
            line_end = args.get("line_end")
            return read_file(
                self.policy,
                cwd,
                path=path,
                line_start=int(line_start) if line_start is not None else None,
                line_end=int(line_end) if line_end is not None else None,
                column_start=args.get("column_start", 1), expected_version=args.get("expected_version", ""),
                max_chars=self.context.limits["read_file"]["chars"], max_lines=self.context.limits["read_file"]["lines"],
            )

        if name == "search_text":
            pattern = str(args.get("pattern", ""))
            is_regex = bool(args.get("is_regex", False))
            include_glob = str(args.get("include_glob", "*"))
            return search_text(
                self.policy,
                cwd,
                pattern=pattern,
                is_regex=is_regex,
                include_glob=include_glob,
            )

        if name == "write_file":
            path = str(args.get("path", ""))
            content = str(args.get("content", ""))
            return write_file(self.policy, cwd, path=path, content=content, backup_dir=backup_dir)

        if name == "patch_file":
            path = str(args.get("path", ""))
            old_str = str(args.get("old_str", ""))
            new_str = str(args.get("new_str", ""))
            return patch_file(self.policy, cwd, path=path, old_str=old_str, new_str=new_str, backup_dir=backup_dir)

        if name == "delete_file":
            path = str(args.get("path", ""))
            return delete_file(self.policy, cwd, path=path, backup_dir=backup_dir)

        return ToolResult(ok=False, stderr=f"Unsupported tool: {name}", exit_code=2)
