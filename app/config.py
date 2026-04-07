from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


@dataclass(slots=True)
class Settings:
    openai_api_key: str
    openai_model: str
    base_url: str
    max_retry_steps: int
    max_tool_calls: int
    max_explore_steps_before_write: int
    max_context_turns: int
    context_summary_max_chars: int
    allow_finish_without_tests: bool
    verify_mode: str
    sandbox_mode: str
    db_path: Path
    workspace: Path
    test_command: str
    auto_confirm_risky_writes: bool
    allowed_commands: frozenset[str]

    @classmethod
    def from_env(cls, workspace: Path, auto_confirm_risky_writes: bool = False) -> "Settings":
        _load_dotenv(workspace / ".env")

        api_key = os.getenv("OPENAI_API_KEY", "")
        model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
        base_url = os.getenv("BASE_URL", "https://api.openai.com/v1")
        max_retry_steps = int(os.getenv("MAX_RETRY_STEPS", os.getenv("MAX_STEPS", "20")))
        max_tool_calls = int(os.getenv("MAX_TOOL_CALLS", "80"))
        max_explore_steps_before_write = int(os.getenv("MAX_EXPLORE_STEPS_BEFORE_WRITE", "2"))
        max_context_turns = int(os.getenv("MAX_CONTEXT_TURNS", "8"))
        context_summary_max_chars = int(os.getenv("CONTEXT_SUMMARY_MAX_CHARS", "2000"))
        allow_finish_without_tests = os.getenv("ALLOW_FINISH_WITHOUT_TESTS", "true").lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        verify_mode = os.getenv("VERIFY_MODE", "auto").lower()
        sandbox_mode = os.getenv("SANDBOX_MODE", "restricted")
        test_command = os.getenv("TEST_COMMAND", "pytest -q")

        # 允许 run_command 工具调用的命令白名单（逗号分隔）
        _raw_cmds = os.getenv("ALLOWED_COMMANDS", "pytest,python,pip,uv")
        allowed_commands = frozenset(c.strip() for c in _raw_cmds.split(",") if c.strip())

        db_dir = workspace / ".agent"
        db_dir.mkdir(parents=True, exist_ok=True)
        db_path = db_dir / "agent.db"

        return cls(
            openai_api_key=api_key,
            openai_model=model,
            base_url=base_url,
            max_retry_steps=max_retry_steps,
            max_tool_calls=max_tool_calls,
            max_explore_steps_before_write=max_explore_steps_before_write,
            max_context_turns=max_context_turns,
            context_summary_max_chars=context_summary_max_chars,
            allow_finish_without_tests=allow_finish_without_tests,
            verify_mode=verify_mode,
            sandbox_mode=sandbox_mode,
            db_path=db_path,
            workspace=workspace.resolve(),
            test_command=test_command,
            auto_confirm_risky_writes=auto_confirm_risky_writes,
            allowed_commands=allowed_commands,
        )
