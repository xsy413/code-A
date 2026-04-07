from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from app.config import Settings
from app.graph.state import AgentState, new_state
from app.tools import ToolInput, ToolResult


def make_workspace() -> Path:
    root = Path.cwd() / ".ut_tmp"
    root.mkdir(parents=True, exist_ok=True)
    workspace = root / f"case-{uuid4().hex}"
    workspace.mkdir(parents=True, exist_ok=True)
    return workspace


def make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "openai_api_key": "",
        "openai_model": "test-model",
        "base_url": "https://example.com/v1",
        "max_retry_steps": 5,
        "max_tool_calls": 20,
        "max_explore_steps_before_write": 2,
        "max_context_turns": 3,
        "context_summary_max_chars": 1000,
        "allow_finish_without_tests": True,
        "verify_mode": "auto",
        "sandbox_mode": "restricted",
        "db_path": tmp_path / ".agent" / "agent.db",
        "workspace": tmp_path,
        "test_command": "pytest -q",
        "auto_confirm_risky_writes": False,
        "allowed_commands": frozenset({"python", "pytest"}),
    }
    values.update(overrides)
    return Settings(**values)


def make_state(tmp_path: Path, **overrides: Any) -> AgentState:
    state = dict(new_state(session_id="s1", task="implement feature", workspace=str(tmp_path)))
    state.update(overrides)
    return state  # type: ignore[return-value]


class FakeStore:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.states: list[tuple[str, dict[str, Any]]] = []
        self.tool_calls: list[dict[str, Any]] = []
        self.token_usages: list[dict[str, Any]] = []

    def add_event(self, **kwargs: Any) -> None:
        self.events.append(kwargs)

    def upsert_state(self, session_id: str, state: dict[str, Any]) -> None:
        self.states.append((session_id, dict(state)))

    def add_tool_call(self, session_id: str, node_name: str, tool_name: str, args: dict, result: dict) -> None:
        self.tool_calls.append(
            {
                "session_id": session_id,
                "node_name": node_name,
                "tool_name": tool_name,
                "args": dict(args),
                "result": dict(result),
            }
        )

    def record_token_usage(
        self,
        session_id: str,
        node_name: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
    ) -> None:
        self.token_usages.append(
            {
                "session_id": session_id,
                "node_name": node_name,
                "model": model,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total_tokens,
            }
        )


class FakeTools:
    def __init__(self, responses: list[ToolResult | Exception] | None = None, risky_write: bool = False) -> None:
        self.responses = list(responses or [])
        self.risky_write = risky_write
        self.executed: list[ToolInput] = []
        self.risky_checks: list[tuple[Path, str]] = []

    def is_risky_write(self, cwd: Path, path: str) -> bool:
        self.risky_checks.append((cwd, path))
        return self.risky_write

    def execute(self, payload: ToolInput) -> ToolResult:
        self.executed.append(payload)
        if not self.responses:
            return ToolResult(ok=True, stdout="ok", stderr="", artifacts=[], exit_code=0)
        next_item = self.responses.pop(0)
        if isinstance(next_item, Exception):
            raise next_item
        return next_item


class FakeLLM:
    def __init__(
        self,
        text_responses: list[str | Exception] | None = None,
        json_responses: list[dict[str, Any] | Exception] | None = None,
        stream_responses: list[list[str] | Exception] | None = None,
    ) -> None:
        self.text_responses = list(text_responses or [])
        self.json_responses = list(json_responses or [])
        self.stream_responses = list(stream_responses or [])
        self.last_usage: Any = None

        self.text_calls: list[dict[str, Any]] = []
        self.json_calls: list[dict[str, Any]] = []
        self.stream_calls: list[dict[str, Any]] = []

    def complete_text(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        temperature: float = 0,
        history: list[dict] | None = None,
    ) -> str:
        self.text_calls.append(
            {
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "temperature": temperature,
                "history": history,
            }
        )
        if not self.text_responses:
            return "text"
        item = self.text_responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def complete_json(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        temperature: float = 0,
        history: list[dict] | None = None,
    ) -> dict:
        self.json_calls.append(
            {
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "temperature": temperature,
                "history": history,
            }
        )
        if not self.json_responses:
            return {"tool": "run_command", "args": {"command": "python -V"}}
        item = self.json_responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def stream_text(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        temperature: float = 0,
        history: list[dict] | None = None,
    ):
        self.stream_calls.append(
            {
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "temperature": temperature,
                "history": history,
            }
        )
        if not self.stream_responses:
            chunks: list[str] = ["chunk"]
        else:
            item = self.stream_responses.pop(0)
            if isinstance(item, Exception):
                raise item
            chunks = item
        for chunk in chunks:
            yield chunk


def set_usage(llm: FakeLLM, *, prompt: int, completion: int, model: str = "test-model") -> None:
    llm.last_usage = SimpleNamespace(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=prompt + completion,
        model=model,
    )
