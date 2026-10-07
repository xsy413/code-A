from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class EventRecord:
    session_id: str
    node_name: str
    ts: str
    duration_ms: int
    input_summary: str
    output_summary: str
    error: str


class SQLiteStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.sanitize = lambda value: value
        self.result_bytes = 20 * 1024 * 1024
        self.session_bytes = 100 * 1024 * 1024
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with closing(self._connect()) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    state_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    node_name TEXT NOT NULL,
                    ts TEXT NOT NULL,
                    duration_ms INTEGER NOT NULL,
                    input_summary TEXT NOT NULL,
                    output_summary TEXT NOT NULL,
                    error TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS tool_calls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    ts TEXT NOT NULL,
                    node_name TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    args_json TEXT NOT NULL,
                    result_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS token_usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    ts TEXT NOT NULL,
                    node_name TEXT NOT NULL,
                    model TEXT NOT NULL,
                    prompt_tokens INTEGER NOT NULL DEFAULT 0,
                    completion_tokens INTEGER NOT NULL DEFAULT 0,
                    total_tokens INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS permission_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    ts TEXT NOT NULL,
                    details_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS context_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL, record_id TEXT NOT NULL,
                    kind TEXT NOT NULL, payload_json TEXT NOT NULL, ts TEXT NOT NULL,
                    UNIQUE(session_id, record_id)
                );
                CREATE TABLE IF NOT EXISTS tool_output_chunks (
                    session_id TEXT NOT NULL, result_id TEXT NOT NULL, stream TEXT NOT NULL,
                    part INTEGER NOT NULL, start_line INTEGER NOT NULL, start_column INTEGER NOT NULL,
                    body TEXT NOT NULL, byte_count INTEGER NOT NULL,
                    PRIMARY KEY(session_id, result_id, stream, part)
                );
                CREATE TABLE IF NOT EXISTS context_compactions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
                    ts TEXT NOT NULL, details_json TEXT NOT NULL
                );
                """
            )
            conn.commit()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def upsert_state(self, session_id: str, state: dict[str, Any]) -> None:
        now = self._now()
        payload = json.dumps(self.sanitize(state), ensure_ascii=False)
        with closing(self._connect()) as conn:
            self._save_context_records(conn, session_id, state)
            conn.execute(
                """
                INSERT INTO sessions(session_id, state_json, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    state_json = excluded.state_json,
                    updated_at = excluded.updated_at
                """,
                (session_id, payload, now, now),
            )
            conn.commit()

    def load_state(self, session_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT state_json FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if not row:
            return None
        return json.loads(row["state_json"])

    def add_event(
        self,
        session_id: str,
        node_name: str,
        duration_ms: int,
        input_summary: str,
        output_summary: str,
        error: str = "",
    ) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO events(session_id, node_name, ts, duration_ms, input_summary, output_summary, error)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    node_name,
                    self._now(),
                    duration_ms,
                    self.sanitize(input_summary),
                    self.sanitize(output_summary),
                    self.sanitize(error),
                ),
            )
            conn.commit()

    def add_tool_call(self, session_id: str, node_name: str, tool_name: str, args: dict, result: dict) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO tool_calls(session_id, ts, node_name, tool_name, args_json, result_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    self._now(),
                    node_name,
                    tool_name,
                    json.dumps(self.sanitize(args), ensure_ascii=False),
                    json.dumps(self.sanitize(result), ensure_ascii=False),
                ),
            )
            conn.commit()

    def save_action_result(self, session_id: str, state: dict, node_name: str, call: dict) -> None:
        """Commit a completed call and its queue checkpoint together."""
        now = self._now()
        with closing(self._connect()) as conn, conn:
            archive = call.pop("_archive", None)
            if archive is not None:
                self._archive_result(conn, session_id, call, archive)
            conn.execute(
                "INSERT INTO tool_calls(session_id, ts, node_name, tool_name, args_json, result_json) VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, now, node_name, call["name"], json.dumps(self.sanitize(call["args"]), ensure_ascii=False),
                 json.dumps(self.sanitize(call), ensure_ascii=False)),
            )
            conn.execute(
                "INSERT INTO sessions(session_id, state_json, created_at, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(session_id) DO UPDATE SET state_json=excluded.state_json, updated_at=excluded.updated_at",
                (session_id, json.dumps(self.sanitize(state), ensure_ascii=False), now, now),
            )

    def _save_context_records(self, conn, session_id: str, state: dict) -> None:
        for group in state.get("session_history", []):
            if group.get("record_id"):
                conn.execute("INSERT OR IGNORE INTO context_records(session_id,record_id,kind,payload_json,ts) VALUES (?,?,?,?,?)",
                             (session_id, group["record_id"], group["type"], json.dumps(self.sanitize(group), ensure_ascii=False), self._now()))

    def _archive_result(self, conn, session_id: str, call: dict, archive: dict) -> None:
        used = conn.execute("SELECT COALESCE(SUM(byte_count),0) FROM tool_output_chunks WHERE session_id=?", (session_id,)).fetchone()[0]
        remaining = max(0, min(self.result_bytes, self.session_bytes - used))
        meta = call.setdefault("output_meta", {})
        complete = bool(meta.get("archive_complete", True))
        for stream in ("stdout", "stderr"):
            text = self.sanitize(archive.get(stream, ""))
            supplied = meta.get("capture_segments", {}).get(stream)
            segments = [(s["start_line"], s.get("start_column", 1), self.sanitize(s["body"])) for s in supplied] if supplied else [(1, 1, text)]
            size = sum(len(body.encode("utf-8")) for _, _, body in segments)
            allowance = min(remaining, size)
            remaining -= allowance
            if allowance < size:
                complete = False
                kept = []
                head_left, tail_left = allowance // 4, allowance - allowance // 4
                for line, column, body in segments:
                    raw = body.encode("utf-8")
                    piece = raw[:head_left].decode("utf-8", errors="ignore")
                    head_left -= min(head_left, len(raw))
                    if piece:
                        kept.append((line, column, piece))
                for line, column, body in reversed(segments):
                    raw = body.encode("utf-8")
                    piece = raw[max(0, len(raw) - tail_left):].decode("utf-8", errors="ignore") if tail_left else ""
                    tail_left -= min(tail_left, len(raw))
                    if piece:
                        preceding = body[:-len(piece)]
                        lines = preceding.count("\n")
                        kept.append((line + lines, len(preceding.rsplit("\n", 1)[-1]) + 1 if lines else column + len(preceding), piece))
                segments = sorted(kept)
            for part, (line, column, body) in enumerate(segments):
                if body:
                    conn.execute("INSERT OR IGNORE INTO tool_output_chunks VALUES (?,?,?,?,?,?,?,?)",
                                 (session_id, call["result_id"], stream, part, line, column, body, len(body.encode("utf-8"))))
        meta["archive_complete"] = complete
        meta.pop("capture_segments", None)
        payload = {key: call.get(key) for key in ("result_id", "name", "args", "ok", "exit_code", "execution_status", "error_kind", "output_meta", "permission")}
        conn.execute("INSERT OR IGNORE INTO context_records(session_id,record_id,kind,payload_json,ts) VALUES (?,?,?,?,?)",
                     (session_id, call["result_id"], "tool_result", json.dumps(self.sanitize(payload), ensure_ascii=False), self._now()))

    def get_result(self, session_id: str, result_id: str) -> dict | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT payload_json FROM context_records WHERE session_id=? AND record_id=? AND kind='tool_result'", (session_id, result_id)).fetchone()
            if not row:
                return None
            result = json.loads(row[0])
            chunks = conn.execute("SELECT stream,start_line,start_column,body FROM tool_output_chunks WHERE session_id=? AND result_id=? ORDER BY stream,part", (session_id, result_id)).fetchall()
        result["chunks"] = [dict(chunk) for chunk in chunks]
        return result

    def get_tool_records(self, session_id: str) -> list[dict]:
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT tool_name,args_json,result_json FROM tool_calls WHERE session_id=? ORDER BY id", (session_id,)).fetchall()
        records = []
        for row in rows:
            record = json.loads(row["result_json"])
            record.setdefault("name", row["tool_name"])
            record.setdefault("args", json.loads(row["args_json"]))
            records.append(record)
        return records

    def commit_compaction(self, session_id: str, state: dict, details: dict, *, originals=None) -> None:
        with closing(self._connect()) as conn, conn:
            if originals is not None:
                self._save_context_records(conn, session_id, {"session_history": originals})
            self._save_context_records(conn, session_id, state)
            conn.execute("INSERT INTO context_compactions(session_id,ts,details_json) VALUES (?,?,?)",
                         (session_id, self._now(), json.dumps(self.sanitize(details), ensure_ascii=False)))
            conn.execute("UPDATE sessions SET state_json=?,updated_at=? WHERE session_id=?",
                         (json.dumps(self.sanitize(state), ensure_ascii=False), self._now(), session_id))

    def record_compaction_failure(self, session_id: str, details: dict) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute("INSERT INTO context_compactions(session_id,ts,details_json) VALUES (?,?,?)",
                         (session_id, self._now(), json.dumps(self.sanitize(details), ensure_ascii=False)))

    def get_context_details(self, session_id: str) -> dict:
        with closing(self._connect()) as conn:
            used = conn.execute("SELECT COALESCE(SUM(byte_count),0) FROM tool_output_chunks WHERE session_id=?", (session_id,)).fetchone()[0]
            rows = conn.execute("SELECT details_json FROM context_compactions WHERE session_id=? ORDER BY id", (session_id,)).fetchall()
            results = conn.execute("SELECT payload_json FROM context_records WHERE session_id=? AND kind='tool_result'", (session_id,)).fetchall()
        return {"archive_bytes": used, "archive_limit": self.session_bytes,
                "incomplete_results": sum(not json.loads(row[0]).get("output_meta", {}).get("archive_complete", False) for row in results),
                "compactions": [json.loads(row[0]) for row in rows]}

    def get_calibration(self, key: str) -> float:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT payload_json FROM context_records WHERE session_id='__calibration__' AND record_id=?", (key,)).fetchone()
        return float(json.loads(row[0])["factor"]) if row else 1.0

    def save_calibration(self, key: str, factor: float) -> None:
        with closing(self._connect()) as conn, conn:
            row = conn.execute("SELECT payload_json FROM context_records WHERE session_id='__calibration__' AND record_id=?", (key,)).fetchone()
            factor = max(factor, float(json.loads(row[0])["factor"]) if row else 1.0)
            conn.execute("INSERT INTO context_records(session_id,record_id,kind,payload_json,ts) VALUES ('__calibration__',?,'calibration',?,?) "
                         "ON CONFLICT(session_id,record_id) DO UPDATE SET payload_json=excluded.payload_json,ts=excluded.ts",
                         (key, json.dumps({"factor": factor}), self._now()))

    def get_events(self, session_id: str) -> list[EventRecord]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT session_id, node_name, ts, duration_ms, input_summary, output_summary, error
                FROM events
                WHERE session_id = ?
                ORDER BY id ASC
                """,
                (session_id,),
            ).fetchall()
        return [EventRecord(**dict(row)) for row in rows]

    def add_permission_event(self, session_id: str, details: dict) -> None:
        with closing(self._connect()) as conn:
            conn.execute("INSERT INTO permission_events(session_id, ts, details_json) VALUES (?, ?, ?)",
                         (session_id, self._now(), json.dumps(self.sanitize(details), ensure_ascii=False)))
            conn.commit()

    def get_permission_events(self, session_id: str) -> list[dict]:
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT ts, details_json FROM permission_events WHERE session_id = ? ORDER BY id", (session_id,)).fetchall()
        return [{"ts": row["ts"], **json.loads(row["details_json"])} for row in rows]

    def last_session_id(self) -> str | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT session_id FROM sessions ORDER BY updated_at DESC LIMIT 1"
            ).fetchone()
        if not row:
            return None
        return str(row["session_id"])

    # ── Token 用量追踪 ───────────────────────────────────────────────────────

    def record_token_usage(
        self,
        session_id: str,
        node_name: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
    ) -> None:
        """Per-call 记录一次 LLM 调用的 token 用量到数据库。"""
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO token_usage
                    (session_id, ts, node_name, model,
                     prompt_tokens, completion_tokens, total_tokens)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    self._now(),
                    node_name,
                    model,
                    prompt_tokens,
                    completion_tokens,
                    total_tokens,
                ),
            )
            conn.commit()

    def get_token_summary(self, session_id: str) -> dict:
        """返回某个 session 的 token 用量汇总。

        Returns:
            {
                "prompt_tokens": int,
                "completion_tokens": int,
                "total_tokens": int,
                "llm_calls": int,
                "by_node": [{"node_name": str, "model": str, "total_tokens": int, "calls": int}, ...]
            }
        """
        with closing(self._connect()) as conn:
            # 整体汇总
            total_row = conn.execute(
                """
                SELECT
                    COALESCE(SUM(prompt_tokens), 0)     AS prompt_tokens,
                    COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                    COALESCE(SUM(total_tokens), 0)      AS total_tokens,
                    COUNT(*)                             AS llm_calls
                FROM token_usage
                WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()

            # 按节点分断
            node_rows = conn.execute(
                """
                SELECT
                    node_name,
                    model,
                    COALESCE(SUM(total_tokens), 0) AS total_tokens,
                    COUNT(*)                        AS calls
                FROM token_usage
                WHERE session_id = ?
                GROUP BY node_name, model
                ORDER BY total_tokens DESC
                """,
                (session_id,),
            ).fetchall()

        return {
            "prompt_tokens": int(total_row["prompt_tokens"]),
            "completion_tokens": int(total_row["completion_tokens"]),
            "total_tokens": int(total_row["total_tokens"]),
            "llm_calls": int(total_row["llm_calls"]),
            "by_node": [
                {
                    "node_name": r["node_name"],
                    "model": r["model"],
                    "total_tokens": int(r["total_tokens"]),
                    "calls": int(r["calls"]),
                }
                for r in node_rows
            ],
        }

