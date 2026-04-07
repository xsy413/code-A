from __future__ import annotations

import json
from pathlib import Path

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


class ToolExecutor:
    def __init__(self, policy: SandboxPolicy, test_command: str = "pytest -q") -> None:
        self.policy = policy
        self.test_command = test_command

    def is_risky_write(self, cwd: Path, path: str) -> bool:
        target = (cwd / path).resolve()
        critical_names = {".env", "pyproject.toml"}
        if target.name in critical_names:
            return True
        return False

    def _inspect_workspace(self, cwd: Path) -> ToolResult:
        top_entries = sorted([p.name for p in cwd.iterdir() if not p.name.startswith(".")])[:100]
        source_roots = [name for name in ("src", "app") if (cwd / name).is_dir()]
        payload = {
            "top_entries": top_entries,
            "has_tests": (cwd / "tests").exists(),
            "has_pyproject": (cwd / "pyproject.toml").exists(),
            "has_requirements": (cwd / "requirements.txt").exists(),
            "source_roots": source_roots,
        }
        return ToolResult(ok=True, stdout=json.dumps(payload, ensure_ascii=False), artifacts=[])

    def execute(self, payload: ToolInput) -> ToolResult:
        cwd = Path(payload.cwd).resolve()
        name = payload.name
        args = payload.args
        backup_dir = Path(payload.backup_dir).resolve() if payload.backup_dir else None

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

        if name == "python_probe":
            code = str(args.get("code", "")).strip()
            if not code:
                return ToolResult(ok=False, stderr="python_probe: code must not be empty", exit_code=2)
            timeout_s = int(args.get("timeout_s", 20))
            command = f"python -c {json.dumps(code)}"
            result = self.policy.run_shell_command(command, cwd, timeout_s=timeout_s)
            return ToolResult(
                ok=result.ok,
                stdout=result.stdout,
                stderr=result.stderr,
                exit_code=result.exit_code,
                artifacts=[],
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

        if name == "run_tests":
            result = self.policy.run_shell_command(self.test_command, cwd)
            return ToolResult(
                ok=result.ok,
                stdout=result.stdout,
                stderr=result.stderr,
                exit_code=result.exit_code,
                artifacts=[],
            )

        if name == "run_test_target":
            target = str(args.get("target", "")).strip()
            if not target:
                return ToolResult(ok=False, stderr="run_test_target: target must not be empty", exit_code=2)
            timeout_s = int(args.get("timeout_s", 60))
            command = f"pytest -q {target}"
            result = self.policy.run_shell_command(command, cwd, timeout_s=timeout_s)
            return ToolResult(
                ok=result.ok,
                stdout=result.stdout,
                stderr=result.stderr,
                exit_code=result.exit_code,
                artifacts=[],
            )

        if name == "run_command":
            command = str(args.get("command", "")).strip()
            if not command:
                return ToolResult(ok=False, stderr="run_command: command must not be empty", exit_code=2)
            timeout_s = int(args.get("timeout_s", 60))
            result = self.policy.run_shell_command(command, cwd, timeout_s=timeout_s)
            return ToolResult(
                ok=result.ok,
                stdout=result.stdout,
                stderr=result.stderr,
                exit_code=result.exit_code,
                artifacts=[],
            )

        if name == "finish":
            return ToolResult(ok=True, stdout="Finishing run.")

        return ToolResult(ok=False, stderr=f"Unsupported tool: {name}", exit_code=2)
