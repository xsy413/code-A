from __future__ import annotations

import base64
import json
import uuid
from pathlib import Path

from app.context.paging import head_tail, page_text, version
from app.tools.protocol import ToolResult


def query_key(name: str, args: dict, cwd: str) -> str:
    return version(json.dumps([name, {k: v for k, v in args.items() if k != "cursor"}, cwd], sort_keys=True))


def cursor_encode(value: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(value).encode()).decode()


def cursor_decode(value: str) -> dict:
    try:
        result = json.loads(base64.urlsafe_b64decode(value))
        if not isinstance(result, dict):
            raise ValueError()
        return result
    except (ValueError, TypeError) as exc:
        raise ValueError("Invalid result cursor.") from exc


def present(result: ToolResult, name: str, args: dict, cwd: str, config, *, continuation: dict | None = None) -> ToolResult:
    result.result_id = result.result_id or uuid.uuid4().hex
    result.archive_streams = result.archive_streams or {"stdout": result.stdout, "stderr": result.stderr}
    limit = config.limits.get(name, {"chars": 2000})
    meta = result.output_meta
    meta.setdefault("cwd", cwd)
    meta.setdefault("archive_complete", True)
    if meta.pop("presented", False):
        return result
    if name in {"list_files", "search_text"} and result.ok:
        text = result.archive_streams.get("stdout", result.stdout)
        sep = "\n" if name == "list_files" else "\n\n"
        items = text.split(sep) if text else []
        offset = int((continuation or {}).get("offset", 0))
        column = int((continuation or {}).get("column", 0))
        if offset < 0 or column < 0:
            raise ValueError("Invalid cursor range.")
        parts, used, index = [], 0, offset
        while index < len(items) and index - offset < limit["items"]:
            item = items[index][column:]
            room = limit["chars"] - used - (len(sep) if parts else 0)
            if room <= 0:
                break
            piece = item[:room]
            parts.append(piece)
            used += len(piece) + (len(sep) if len(parts) > 1 else 0)
            if len(piece) < len(item):
                column += len(piece)
                break
            index, column = index + 1, 0
        result.stdout = sep.join(parts)
        meta.update({"total_items": len(items), "item_start": offset, "item_end": index,
                     "version": version(text), "query": query_key(name, args, cwd)})
        meta["cursor"] = cursor_encode({"result_id": (continuation or {}).get("result_id", result.result_id),
                                         "version": meta["version"], "query": meta["query"], "offset": index, "column": column}) if index < len(items) else None
    elif name in {"bash", "powershell"}:
        # One combined quota, not 20K per channel. Preserve both channels' head and tail.
        streams = [result.stdout, result.stderr]
        total = sum(map(len, streams))
        allowances = [len(s) for s in streams]
        if total > limit["chars"]:
            allowances = [int(limit["chars"] * len(s) / total) for s in streams]
            allowances[0] += limit["chars"] - sum(allowances)
        result.stdout, result.stderr = [head_tail(s, budget, int(budget * limit["head"] / limit["chars"])) if budget else "" for s, budget in zip(streams, allowances)]
        meta["streams"] = {stream: {"captured_chars": len(text)} for stream, text in zip(("stdout", "stderr"), streams)}
    elif name not in {"read_file", "read_tool_result"}:
        if name == "inspect_workspace" and result.ok:
            info = json.loads(result.stdout)
            entries = info.get("top_entries", [])
            info["top_entries"] = entries[:limit["items"]]
            info["omitted_entries"] = len(entries) - len(info["top_entries"])
            while info["top_entries"] and len(json.dumps(info, ensure_ascii=False)) > limit["chars"]:
                info["top_entries"].pop()
                info["omitted_entries"] = len(entries) - len(info["top_entries"])
            result.stdout = json.dumps(info, ensure_ascii=False)
            if len(result.stdout) > limit["chars"]:
                meta["workspace"] = info
                result.stdout = ""
        else:
            result.stdout = result.stdout[:limit["chars"]]
        result.stderr = result.stderr[:max(0, limit["chars"] - len(result.stdout))]
    if len(result.artifacts) > 50:
        meta["artifact_count"] = len(result.artifacts)
        result.artifacts = result.artifacts[:50]
    if len(result.changed_files) > 50:
        meta["change_count"] = len(result.changed_files)
    return result


def read_archive(store, session_id: str, args: dict, config) -> ToolResult:
    saved = store.get_result(session_id, args["result_id"]) if store else None
    if not saved:
        return ToolResult(False, stderr="Completed result not found in this session.", error_kind="result_not_found", exit_code=1)
    stream = args.get("stream", "stdout")
    if stream not in {"stdout", "stderr"}:
        return ToolResult(False, stderr="stream must be stdout or stderr.", error_kind="invalid_tool_args", exit_code=2)
    start, column = args.get("line_start", 1), args.get("column_start", 1)
    chunks = [c for c in saved["chunks"] if c["stream"] == stream]
    limit = config.limits["read_tool_result"]
    ranges = [{"line_start": c["start_line"], "column_start": c["start_column"],
               "line_count": len(c["body"].splitlines())} for c in chunks]
    for chunk in chunks:
        line = chunk["start_line"]
        count = len(chunk["body"].splitlines())
        col = column - chunk["start_column"] + 1 if start == line else column
        chunk_lines = chunk["body"].splitlines(keepends=True)
        if line <= start < line + count and 1 <= col <= len(chunk_lines[start - line]):
            body, meta = page_text(chunk["body"], line_start=start - line + 1,
                                   line_end=(args["line_end"] - line + 1) if "line_end" in args else None,
                                   column_start=col, chars=limit["chars"], lines=limit["lines"])
            if meta["line_start"] == 1:
                meta["column_start"] += chunk["start_column"] - 1
            if meta.get("line_end") == 1:
                meta["column_end"] += chunk["start_column"] - 1
            for key in ("line_start", "line_end"):
                if meta.get(key) is not None:
                    meta[key] += line - 1
            if meta.get("next"):
                if meta["next"]["line_start"] == 1:
                    meta["next"]["column_start"] += chunk["start_column"] - 1
                meta["next"]["line_start"] += line - 1
            meta.update({"source_result_id": args["result_id"], "stream": stream,
                         "archive_complete": saved["output_meta"].get("archive_complete", False), "historical": True,
                         "available_ranges": ranges})
            return ToolResult(True, stdout=body, output_meta=meta)
    if saved["output_meta"].get("archive_complete", False):
        return ToolResult(True, output_meta={"source_result_id": args["result_id"], "next": None})
    return ToolResult(False, stderr="Requested lines were not archived and are unrecoverable.",
                      error_kind="archive_gap", exit_code=1, output_meta={"available_ranges": ranges})
