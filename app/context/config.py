from __future__ import annotations

import os
import math
from dataclasses import dataclass, field, fields


@dataclass(slots=True)
class ContextConfig:
    window: int = 185000
    output_reserve: int = 25000
    safety_margin: int = 10000
    target_tokens: int = 100000
    recent_tokens: int = 30000
    tokenizer: str = ""
    estimate_factor: float = 1.20
    compact_model: str = ""
    compact_base_url: str = ""
    compact_api_key: str = ""
    compact_window: int = 0
    compact_tokenizer: str = ""
    compact_summary_tokens: int = 8000
    compact_output_tokens: int = 25000
    compact_timeout: int = 180
    result_bytes: int = 20 * 1024 * 1024
    session_bytes: int = 100 * 1024 * 1024
    limits: dict = field(default_factory=lambda: {
        "inspect_workspace": {"chars": 2000, "items": 50},
        "list_files": {"chars": 20000, "items": 500},
        "read_file": {"chars": 20000, "lines": 2000},
        "search_text": {"chars": 20000, "items": 200},
        "write_file": {"chars": 2000}, "patch_file": {"chars": 2000},
        "delete_file": {"chars": 2000},
        "bash": {"chars": 20000, "head": 5000},
        "powershell": {"chars": 20000, "head": 5000},
        "read_tool_result": {"chars": 20000, "lines": 2000},
    })

    @property
    def input_limit(self) -> int:
        return self.window - self.output_reserve - self.safety_margin

    def validate(self) -> None:
        if self.output_reserve <= 0 or self.safety_margin < 0 or self.input_limit <= 0 or not 0 < self.target_tokens < self.input_limit:
            raise ValueError("CONTEXT budget requires 0 < target < window - output - safety.")
        if not 0 < self.recent_tokens < self.input_limit or not math.isfinite(self.estimate_factor) or self.estimate_factor < 1:
            raise ValueError("Invalid recent context budget or estimation factor.")
        if any(getattr(self, f.name) <= 0 for f in fields(self)
               if f.name in {"compact_summary_tokens", "compact_output_tokens", "compact_timeout", "result_bytes", "session_bytes"}):
            raise ValueError("Context output, timeout and archive limits must be positive.")
        if self.compact_summary_tokens > self.compact_output_tokens:
            raise ValueError("COMPACT_SUMMARY_TOKENS exceeds COMPACT_OUTPUT_TOKENS.")
        if self.compact_window and self.compact_window <= self.compact_output_tokens + self.safety_margin:
            raise ValueError("COMPACT_WINDOW has no input space.")
        if any(value <= 0 for limit in self.limits.values() for value in limit.values()):
            raise ValueError("Tool context limits must be positive.")

    @classmethod
    def from_env(cls) -> "ContextConfig":
        config = cls()
        for name in (f.name for f in fields(config) if f.name != "limits"):
            key = name.upper() if name.startswith("compact_") else "CONTEXT_" + name.upper()
            if key in os.environ:
                old = getattr(config, name)
                setattr(config, name, type(old)(os.environ[key]))
        for tool, limit in config.limits.items():
            for unit in limit:
                key = f"CONTEXT_{tool.upper()}_{unit.upper()}"
                if key in os.environ:
                    limit[unit] = int(os.environ[key])
        config.validate()
        return config
