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
                """
            )
            conn.commit()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def upsert_state(self, session_id: str, state: dict[str, Any]) -> None:
        now = self._now()
        payload = json.dumps(state, ensure_ascii=False)
        with closing(self._connect()) as conn:
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
                    input_summary,
                    output_summary,
                    error,
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
                    json.dumps(args, ensure_ascii=False),
                    json.dumps(result, ensure_ascii=False),
                ),
            )
            conn.commit()

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

