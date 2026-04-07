from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class CommandResult:
    ok: bool
    stdout: str
    stderr: str
    exit_code: int


class SandboxPolicy:
    def __init__(self, workspace: Path, allowed_commands: set[str] | None = None) -> None:
        self.workspace = workspace.resolve()
        self.allowed_commands = allowed_commands or {"pytest", "python"}

    def is_within_workspace(self, path: Path) -> bool:
        resolved = path.resolve()
        try:
            resolved.relative_to(self.workspace)
            return True
        except ValueError:
            return False

    def validate_path(self, path: Path) -> None:
        if not self.is_within_workspace(path):
            raise PermissionError(f"Path is outside workspace: {path}")

    def run_shell_command(self, command: str, cwd: Path, timeout_s: int = 120) -> CommandResult:
        self.validate_path(cwd)
        args = shlex.split(command)
        if not args:
            raise PermissionError("Empty command is not allowed.")
        if args[0] not in self.allowed_commands:
            raise PermissionError(f"Command is not in whitelist: {args[0]}")

        completed = subprocess.run(
            args,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
        return CommandResult(
            ok=(completed.returncode == 0),
            stdout=completed.stdout,
            stderr=completed.stderr,
            exit_code=completed.returncode,
        )
