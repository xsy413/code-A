from __future__ import annotations

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
        self.authorized_paths: set[Path] = set()
        self.readable = lambda path: self.is_within_workspace(path)

    def is_within_workspace(self, path: Path) -> bool:
        resolved = path.resolve()
        try:
            resolved.relative_to(self.workspace)
            return True
        except ValueError:
            return False

    def validate_path(self, path: Path) -> None:
        if not self.is_within_workspace(path) and path.resolve() not in self.authorized_paths:
            raise PermissionError(f"Path is outside workspace: {path}")

    def run_shell_command(self, command: str, cwd: Path, timeout_s: int = 120) -> CommandResult:
        raise PermissionError("Legacy shell execution disabled; use the permission-aware ToolExecutor.")
