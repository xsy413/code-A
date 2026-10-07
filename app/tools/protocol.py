from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class ToolInput:
    name: str
    args: dict[str, Any]
    cwd: str
    backup_dir: str = ""  # 若非空，write/patch/delete 在操作前会备份原始文件
    session_id: str = ""


@dataclass(slots=True)
class ToolResult:
    ok: bool
    stdout: str = ""
    stderr: str = ""
    artifacts: list[str] = field(default_factory=list)
    exit_code: int = 0
    execution_status: str = "executed"
    error_kind: str = ""
    permission: dict[str, Any] = field(default_factory=dict)
    changed_files: list[str] = field(default_factory=list)
    deleted_files: list[str] = field(default_factory=list)
    snapshot_complete: bool = True
    is_test: bool = False
    test_info: dict[str, Any] = field(default_factory=dict)
    result_id: str = ""
    output_meta: dict[str, Any] = field(default_factory=dict)
    archive_streams: dict[str, str] = field(default_factory=dict)

