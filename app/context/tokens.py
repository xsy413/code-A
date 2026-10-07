from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path


class TokenCounter:
    def __init__(self, model: str, tokenizer: str = "", factor: float = 1.20, service: str = ""):
        self.factor = factor
        self.estimated = True
        if tokenizer:
            from tokenizers import Tokenizer
            path = Path(tokenizer).expanduser().resolve()
            self.encoder = Tokenizer.from_file(str(path))
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            self.identity = f"local:{digest}"
            self.encode = lambda text: self.encoder.encode(text, add_special_tokens=False).ids
        else:
            import tiktoken
            try:
                encoding = tiktoken.encoding_for_model(model)
                self.identity = f"known:{encoding.name}"
            except KeyError:
                encoding = tiktoken.get_encoding("o200k_base")
                self.identity = "estimated:o200k_base"
            self.encode = lambda text: encoding.encode(text, disallowed_special=())
        self.key = hashlib.sha256(json.dumps([service.rstrip("/"), model, self.identity]).encode()).hexdigest()

    def text(self, value: str) -> int:
        return len(self.encode(value))

    def request(self, messages: list[dict], tools: list[dict] | None = None, calibration: float = 1) -> dict:
        serialized = json.dumps({"messages": messages, "tools": tools or []}, ensure_ascii=False, separators=(",", ":"))
        raw = self.text(serialized) + len(messages) * 32 + 256
        return {"tokens": math.ceil(raw * max(self.factor, calibration)), "raw_tokens": raw,
                "estimated": self.estimated, "tokenizer": self.identity, "key": self.key,
                "factor": max(self.factor, calibration),
                "message_tokens": self.text(json.dumps(messages, ensure_ascii=False)),
                "tool_schema_tokens": self.text(json.dumps(tools or [], ensure_ascii=False))}
