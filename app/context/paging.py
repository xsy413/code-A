from __future__ import annotations

import hashlib


def version(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def page_text(text: str, *, line_start: int = 1, line_end: int | None = None,
              column_start: int = 1, chars: int = 20000, lines: int = 2000) -> tuple[str, dict]:
    """Inclusive, one-based lines; columns count Unicode characters including newlines."""
    if line_start < 1 or column_start < 1 or (line_end is not None and line_end < line_start):
        raise ValueError("Invalid one-based page range.")
    source = text.splitlines(keepends=True)
    last = min(len(source), line_end or len(source), line_start + lines - 1)
    if line_start > len(source):
        return "", {"line_start": line_start, "line_end": None, "total_lines": len(source), "next": None}
    if column_start > len(source[line_start - 1]) + 1:
        raise ValueError("column_start exceeds the requested line.")
    selected = "".join(source[line_start - 1:last])[column_start - 1:]
    body = selected[:chars]
    line, column = line_start, column_start
    for char in body:
        if char == "\n":
            line, column = line + 1, 1
        else:
            column += 1
    end_line = line - 1 if body.endswith("\n") else line
    requested_end = min(line_end or len(source), len(source))
    more = len(body) < len(selected) or last < requested_end
    return body, {"line_start": line_start, "column_start": column_start,
                  "line_end": end_line, "column_end": len(source[end_line - 1]) if body.endswith("\n") else column - 1,
                  "total_lines": len(source), "next": {"line_start": line, "column_start": column} if more else None}


def head_tail(text: str, chars: int, head: int) -> str:
    if len(text) <= chars:
        return text
    marker = "\n[... archived output omitted ...]\n"
    if chars <= len(marker):
        return text[-chars:] if chars else ""
    head = min(head, chars - len(marker))
    tail = chars - head - len(marker)
    return text[:head] + marker + (text[-tail:] if tail else "")
