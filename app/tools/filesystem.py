from __future__ import annotations

import re
import os
import shutil
from pathlib import Path

from app.sandbox import SandboxPolicy
from app.tools.protocol import ToolResult
from app.context.paging import page_text, version


def _workspace_files(policy: SandboxPolicy, cwd: Path, pattern: str):
    for root, dirs, names in os.walk(cwd, followlinks=False):
        dirs[:] = [d for d in dirs if policy.readable(Path(root) / d) and not (Path(root) / d).is_symlink()]
        for name in names:
            path = Path(root) / name
            if policy.readable(path) and path.relative_to(cwd).match(pattern):
                yield path


def list_files(policy: SandboxPolicy, cwd: Path, pattern: str = "*") -> ToolResult:
    policy.validate_path(cwd)
    files: list[str] = []
    for p in _workspace_files(policy, cwd, pattern):
        if p.is_file() and policy.readable(p):
            try:
                p.relative_to(cwd)
            except ValueError:
                continue
            files.append(str(p.relative_to(cwd)))
    files.sort()
    return ToolResult(ok=True, stdout="\n".join(files), output_meta={"total_items": len(files)})


def read_file(
    policy: SandboxPolicy,
    cwd: Path,
    path: str,
    line_start: int | None = None,
    line_end: int | None = None,
    max_chars: int = 20000,
    max_lines: int = 2000,
    column_start: int = 1,
    expected_version: str = "",
) -> ToolResult:
    """读取文件内容，支持行号分页（line_start/line_end 均为 1-indexed，含两端）。"""
    target = (cwd / path).resolve()
    policy.validate_path(target)
    if not target.exists() or not target.is_file():
        return ToolResult(ok=False, stderr=f"File not found: {path}", exit_code=1)

    import hashlib
    raw = target.read_bytes()
    content = raw.decode("utf-8", errors="ignore").replace("\r\n", "\n")
    stamp = hashlib.sha256(raw).hexdigest()
    if expected_version and expected_version != stamp:
        return ToolResult(False, stderr="File version changed; restart reading the current version.",
                          error_kind="version_changed", exit_code=1, output_meta={"path": str(target), "version": stamp})
    body, meta = page_text(content, line_start=line_start or 1, line_end=line_end,
                           column_start=column_start, chars=max_chars, lines=max_lines)
    meta.update({"path": str(target), "version": stamp})
    return ToolResult(ok=True, stdout=body, artifacts=[str(target)], output_meta=meta,
                      archive_streams={"stdout": content})


def _validate_write_path(cwd: Path, path: str) -> str | None:
    normalized = str(path or "").strip()
    if not normalized:
        return "invalid_write_path: empty path"
    if normalized in {".", "./", ".\\"}:
        return "invalid_write_path: path points to current directory"
    if normalized.endswith("/") or normalized.endswith("\\"):
        return "invalid_write_path: directory path is not allowed"

    target = (cwd / normalized).resolve()
    if target == cwd.resolve():
        return "invalid_write_path: target resolves to workspace root"
    if target.exists() and target.is_dir():
        return "invalid_write_path: target is a directory"
    return None


def _backup_file(backup_dir: Path, cwd: Path, rel_path: str) -> str:
    """备份文件到 backup_dir，保持相对路径结构。

    规则：
    - 源文件不存在时跳过（首次创建的新文件无需备份）。
    - 将 rel_path 展开为定时shard名，格式为 `<rel_path>.bak`。
    - 如果备份已存在则跳过（保留 session 开始前的 *原始文件*）。

    Returns:
        实际备份路径字符串，若跳过则返回空字符串。
    """
    source = (cwd / rel_path).resolve()
    if not source.exists() or not source.is_file():
        return ""  # 新文件，无需备份

    try:
        canonical_rel = source.relative_to(cwd.resolve())
    except ValueError:
        return ""  # External paths need approval, but are not covered by workspace backups.
    dest = (backup_dir / canonical_rel).with_suffix(source.suffix + ".bak")
    if dest.exists():
        return ""  # 本 session 已有备份，保留原始版本，不覆盖

    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(source), str(dest))
    return str(dest)


def write_file(
    policy: SandboxPolicy,
    cwd: Path,
    path: str,
    content: str,
    backup_dir: Path | None = None,
) -> ToolResult:
    """写入文件内容。若目标文件已存在且 backup_dir 非空，则先备份再覆盖。"""
    invalid_reason = _validate_write_path(cwd, path)
    if invalid_reason:
        return ToolResult(ok=False, stderr=invalid_reason, exit_code=2)

    try:
        target = (cwd / path).resolve()
        policy.validate_path(target)

        # 备份（静默失败，不阻断主流程）
        if backup_dir is not None:
            try:
                _backup_file(backup_dir, cwd, path)
            except Exception:
                pass

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return ToolResult(ok=True, stdout=f"Wrote {path}", artifacts=[str(target)])
    except PermissionError as exc:
        return ToolResult(ok=False, stderr=f"permission_error: {exc}", exit_code=13)
    except OSError as exc:
        return ToolResult(ok=False, stderr=f"write_error: {exc}", exit_code=1)


def patch_file(
    policy: SandboxPolicy,
    cwd: Path,
    path: str,
    old_str: str,
    new_str: str,
    backup_dir: Path | None = None,
) -> ToolResult:
    """原地替换文件中的精确字符串片段（首次出现）。
    old_str 必须在文件中唯一存在；若有多处匹配则报错，要求提供更唯一的上下文。
    backup_dir 非空时，操作前先备份原始文件。
    """
    normalized = str(path or "").strip()
    if not normalized:
        return ToolResult(ok=False, stderr="patch_file: empty path", exit_code=2)
    if not old_str:
        return ToolResult(ok=False, stderr="patch_file: old_str must not be empty", exit_code=2)

    target = (cwd / normalized).resolve()
    try:
        policy.validate_path(target)
    except PermissionError as exc:
        return ToolResult(ok=False, stderr=f"permission_error: {exc}", exit_code=13)

    if not target.exists() or not target.is_file():
        return ToolResult(ok=False, stderr=f"patch_file: file not found: {path}", exit_code=1)

    try:
        content = target.read_text(encoding="utf-8", errors="ignore")
    except OSError as exc:
        return ToolResult(ok=False, stderr=f"patch_file: read error: {exc}", exit_code=1)

    count = content.count(old_str)
    if count == 0:
        preview = content[:300].replace("\n", "↵")
        return ToolResult(
            ok=False,
            stderr=(
                f"patch_file: old_str not found in {path}.\n"
                f"File preview (first 300 chars): {preview}"
            ),
            exit_code=1,
        )
    if count > 1:
        return ToolResult(
            ok=False,
            stderr=(
                f"patch_file: old_str matches {count} locations in {path}; "
                "add more surrounding context to make it unique."
            ),
            exit_code=1,
        )

    new_content = content.replace(old_str, new_str, 1)

    # 如果是 Python 文件，先做语法检查
    if target.suffix == ".py":
        try:
            compile(new_content, str(target), "exec")
        except SyntaxError as exc:
            return ToolResult(
                ok=False,
                stderr=f"patch_file: result has SyntaxError: {exc}",
                exit_code=1,
            )

    # 备份原文件（静默失败）
    if backup_dir is not None:
        try:
            _backup_file(backup_dir, cwd, path)
        except Exception:
            pass

    try:
        target.write_text(new_content, encoding="utf-8")
    except OSError as exc:
        return ToolResult(ok=False, stderr=f"patch_file: write error: {exc}", exit_code=1)

    return ToolResult(
        ok=True,
        stdout=f"Patched {path} (replaced 1 occurrence)",
        artifacts=[str(target)],
    )


def delete_file(policy: SandboxPolicy, cwd: Path, path: str, backup_dir: Path | None = None) -> ToolResult:
    """删除工作区内的指定文件（不可删目录）。删除前自动备份。"""
    normalized = str(path or "").strip()
    if not normalized:
        return ToolResult(ok=False, stderr="delete_file: empty path", exit_code=2)

    target = (cwd / normalized).resolve()
    try:
        policy.validate_path(target)
    except PermissionError as exc:
        return ToolResult(ok=False, stderr=f"permission_error: {exc}", exit_code=13)

    if not target.exists():
        return ToolResult(ok=False, stderr=f"delete_file: file not found: {path}", exit_code=1)
    if target.is_dir():
        return ToolResult(
            ok=False,
            stderr=f"delete_file: target is a directory (only files allowed): {path}",
            exit_code=2,
        )

    # 删除前备份（静默失败）
    if backup_dir is not None:
        try:
            _backup_file(backup_dir, cwd, path)
        except Exception:
            pass

    try:
        target.unlink()
    except OSError as exc:
        return ToolResult(ok=False, stderr=f"delete_file: error: {exc}", exit_code=1)

    return ToolResult(ok=True, stdout=f"Deleted {path}", artifacts=[])


def search_text(
    policy: SandboxPolicy,
    cwd: Path,
    pattern: str,
    is_regex: bool = False,
    include_glob: str = "*",
) -> ToolResult:
    """在工作区中搜索文本，支持正则和 glob 文件过滤。
    Args:
        pattern: 搜索字符串或正则表达式。
        is_regex: True 时将 pattern 视为正则（re.search）。
        include_glob: glob 模式，限定搜索的文件范围，如 '*.py'。
    """
    policy.validate_path(cwd)

    compiled: re.Pattern | None = None
    if is_regex:
        try:
            compiled = re.compile(pattern)
        except re.error as exc:
            return ToolResult(ok=False, stderr=f"search_text: invalid regex: {exc}", exit_code=1)

    hits: list[str] = []
    for file_path in sorted(_workspace_files(policy, cwd, include_glob)):
        if not file_path.is_file() or not policy.readable(file_path):
            continue
        try:
            policy.validate_path(file_path)
            text = file_path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue

        source_lines = text.splitlines()
        for idx, line in enumerate(source_lines, start=1):
            matched = compiled.search(line) if compiled else (pattern in line)
            if matched:
                rel = file_path.relative_to(cwd)
                context = [f"{rel}:{n + 1}:{source_lines[n]}" for n in range(max(0, idx - 4), min(len(source_lines), idx + 3))]
                hits.append("\n".join(context))
    return ToolResult(ok=True, stdout="\n\n".join(hits), artifacts=[], output_meta={"total_items": len(hits)})
