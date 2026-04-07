from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class ToolInput:
    name: str
    args: dict[str, Any]
    cwd: str
    backup_dir: str = ""  # 若非空，write/patch/delete 在操作前会备份原始文件


@dataclass(slots=True)
class ToolResult:
    ok: bool
    stdout: str = ""
    stderr: str = ""
    artifacts: list[str] = field(default_factory=list)
    exit_code: int = 0

