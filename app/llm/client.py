from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Iterator, Protocol


class LLMHTTPError(RuntimeError):
    def __init__(self, status: int, detail: str):
        super().__init__(f"LLM HTTP error {status}: {detail}")
        self.capacity_error = False
        try:
            error = json.loads(detail).get("error", {})
            self.capacity_error = error.get("code") in {"context_length_exceeded", "max_tokens_exceeded"}
        except (ValueError, AttributeError):
            pass


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    args: dict | None
    raw_arguments: str | None = None
    argument_error: str = ""

    def to_message(self) -> dict:
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": self.raw_arguments if self.raw_arguments is not None else json.dumps(self.args, ensure_ascii=False)},
        }


@dataclass(frozen=True)
class ActionResponse:
    content: str
    tool_calls: tuple[ToolCall, ...]
    finish_reason: str

    def to_message(self) -> dict:
        message = {"role": "assistant", "content": self.content or None}
        if self.tool_calls:
            message["tool_calls"] = [call.to_message() for call in self.tool_calls]
        return message


@dataclass(frozen=True)
class TokenUsage:
    """单次 LLM 调用的 token 用量（frozen 以保证不可变）。"""

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    model: str

    def __add__(self, other: "TokenUsage") -> "TokenUsage":
        return TokenUsage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
            model=self.model,
        )

    @classmethod
    def zero(cls, model: str = "") -> "TokenUsage":
        return cls(prompt_tokens=0, completion_tokens=0, total_tokens=0, model=model)


class LLMClient(Protocol):
    last_usage: TokenUsage | None

    def complete_action(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        tools: list[dict],
        temperature: float = 0,
        history: list[dict] | None = None,
    ) -> ActionResponse: ...

    def complete_text(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        temperature: float = 0,
        history: list[dict] | None = None,
    ) -> str: ...

    def complete_json(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        temperature: float = 0,
        history: list[dict] | None = None,
    ) -> dict: ...

    def stream_text(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        temperature: float = 0,
        history: list[dict] | None = None,
    ) -> Iterator[str]: ...


@dataclass(slots=True)
class OpenAICompatClient:
    api_key: str
    model: str
    base_url: str
    timeout_s: int = 45
    max_json_retries: int = 3
    # 流式请求使用更长的超时（等待首个 token + 全部 token 生成时间）
    stream_timeout_s: int = 300
    max_output_tokens: int = 25000
    # 最近一次调用（或重试累计）的 token 用量
    last_usage: TokenUsage | None = field(default=None, repr=False)

    # ── 内部工具 ────────────────────────────────────────────────────────────

    def _post(self, payload: dict) -> dict:
        """发送非流式请求并返回完整响应体。"""
        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY is missing.")

        url = self.base_url.rstrip("/") + "/chat/completions"
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", f"Bearer {self.api_key}")

        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                body = resp.read().decode("utf-8")
                response = json.loads(body)
                raw = response.get("usage") or {}
                self.last_usage = TokenUsage(
                    prompt_tokens=int(raw.get("prompt_tokens", 0)),
                    completion_tokens=int(raw.get("completion_tokens", 0)),
                    total_tokens=int(raw.get("total_tokens", 0)),
                    model=str(response.get("model", self.model)),
                )
                return response
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")
            raise LLMHTTPError(exc.code, detail) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"LLM connection error: {exc}") from exc

    @staticmethod
    def _build_messages(
        system_prompt: str,
        user_prompt: str,
        history: list[dict] | None,
    ) -> list[dict]:
        """组装 messages 数组：[system] → [history...] → [user]"""
        msgs: list[dict] = [{"role": "system", "content": system_prompt}]
        if history:
            msgs.extend(history)
        msgs.append({"role": "user", "content": user_prompt})
        return msgs

    # ── 非流式接口 ──────────────────────────────────────────────────────────

    def complete_action(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        tools: list[dict],
        temperature: float = 0,
        history: list[dict] | None = None,
    ) -> ActionResponse:
        self.last_usage = None
        data = self._post({
            "model": self.model,
            "messages": self._build_messages(system_prompt, user_prompt, history),
            "temperature": temperature,
            "tools": tools,
            "tool_choice": "auto",
            "parallel_tool_calls": True,
            "max_tokens": self.max_output_tokens,
        })
        choices = data.get("choices")
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
            raise RuntimeError("Invalid action response: expected one choice.")
        choice = choices[0]
        reason = choice.get("finish_reason")
        if reason not in {"stop", "tool_calls"}:
            raise RuntimeError(f"Incomplete or abnormal action response: finish_reason={reason!r}.")
        message = choice.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            raise RuntimeError("Invalid action response: missing assistant message.")
        if message.get("refusal"):
            raise RuntimeError(f"Action response refused: {message['refusal']}")
        content = message.get("content")
        if content is not None and not isinstance(content, str):
            raise RuntimeError("Invalid action response: content must be text.")
        raw_calls = message.get("tool_calls")
        if raw_calls is None:
            raw_calls = []
        if not isinstance(raw_calls, list):
            raise RuntimeError("Invalid action response: tool_calls must be a list.")
        calls: list[ToolCall] = []
        ids: set[str] = set()
        for raw in raw_calls:
            if not isinstance(raw, dict) or raw.get("type") != "function":
                raise RuntimeError("Invalid action response: expected a function tool call.")
            function = raw.get("function")
            if not isinstance(function, dict):
                raise RuntimeError("Invalid action response: missing function.")
            call_id, name = raw.get("id"), function.get("name")
            if not isinstance(call_id, str) or not call_id.strip() or not isinstance(name, str) or not name.strip():
                raise RuntimeError("Invalid action response: tool ID and name are required.")
            if call_id in ids:
                raise RuntimeError("Invalid action response: duplicate tool call ID.")
            ids.add(call_id)
            arguments = function.get("arguments")
            if not isinstance(arguments, str):
                raise RuntimeError("Invalid action response: arguments must be a string.")
            argument_error = ""
            try:
                args = json.loads(arguments)
            except ValueError:
                args = None
            if not isinstance(args, dict):
                args = None
                argument_error = "Invalid tool arguments: expected a JSON object."
            calls.append(ToolCall(call_id, name, args, arguments, argument_error))
        if calls and reason != "tool_calls":
            raise RuntimeError("Invalid action response: tool calls require finish_reason=tool_calls.")
        if not calls and (reason != "stop" or not (content or "").strip()):
            raise RuntimeError("Invalid action response: expected nonempty final text.")
        return ActionResponse(content=content or "", tool_calls=tuple(calls), finish_reason=reason)

    def complete_compact(self, system_prompt: str, user_prompt: str) -> str:
        self.last_usage = None
        data = self._post({"model": self.model, "messages": self._build_messages(system_prompt, user_prompt, None),
                           "max_tokens": self.max_output_tokens})
        if self.last_usage and self.last_usage.completion_tokens > self.max_output_tokens:
            raise RuntimeError("Compaction generation exceeded the configured output budget.")
        choices = data.get("choices", [])
        if len(choices) != 1 or choices[0].get("finish_reason") != "stop":
            raise RuntimeError("Compaction response did not finish normally.")
        message = choices[0].get("message", {})
        if message.get("role") != "assistant" or message.get("tool_calls") or message.get("refusal"):
            raise RuntimeError("Invalid compaction response.")
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError("Empty compaction response.")
        return content

    def complete_text(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        temperature: float = 0,
        history: list[dict] | None = None,
    ) -> str:
        payload = {
            "model": self.model,
            "messages": self._build_messages(system_prompt, user_prompt, history),
            "temperature": temperature,
        }
        data = self._post(payload)
        return data["choices"][0]["message"]["content"]

    def complete_json(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        temperature: float = 0,
        history: list[dict] | None = None,
    ) -> dict:
        """调用模型并解析 JSON，失败时最多重试 max_json_retries 次（自我纠正模式）。
        last_usage 记录所有重试的累计 token 用量。
        """
        messages = self._build_messages(system_prompt, user_prompt, history)
        accumulated: TokenUsage | None = None

        for attempt in range(self.max_json_retries):
            payload = {
                "model": self.model,
                "messages": messages,
                "temperature": temperature,
                "response_format": {"type": "json_object"},
            }
            data = self._post(payload)
            last_content = data["choices"][0]["message"]["content"]

            if self.last_usage is not None:
                accumulated = (accumulated + self.last_usage) if accumulated else self.last_usage

            try:
                result = json.loads(last_content)
                if accumulated is not None:
                    self.last_usage = accumulated
                return result
            except json.JSONDecodeError as exc:
                if attempt >= self.max_json_retries - 1:
                    if accumulated is not None:
                        self.last_usage = accumulated
                    raise RuntimeError(
                        f"Model returned invalid JSON after {self.max_json_retries} attempts. "
                        f"Last response: {last_content[:500]}"
                    ) from exc

                messages = [
                    *messages,
                    {"role": "assistant", "content": last_content},
                    {
                        "role": "user",
                        "content": (
                            f"Your response is not valid JSON (error: {exc}). "
                            "Please correct and respond with valid JSON only."
                        ),
                    },
                ]

        raise RuntimeError("complete_json: unexpected exit from retry loop")

    # ── 流式接口 ────────────────────────────────────────────────────────────

    def stream_text(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        temperature: float = 0,
        history: list[dict] | None = None,
    ) -> Iterator[str]:
        """流式调用 LLM，逐 token chunk yield 文本。

        - 使用 OpenAI SSE 协议（Server-Sent Events）
        - 通过 stream_options.include_usage 在最后一帧获取 token 用量
        - 流式结束后更新 last_usage
        - stream_timeout_s 控制每次 readline 的 socket 超时

        Yields:
            str: 每次 LLM 返回的文本 chunk（可能是一个或多个字符）
        """
        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY is missing.")

        payload = {
            "model": self.model,
            "messages": self._build_messages(system_prompt, user_prompt, history),
            "temperature": temperature,
            "stream": True,
            # OpenAI 扩展：在流式最后一帧返回 usage（大多数兼容服务也支持）
            "stream_options": {"include_usage": True},
        }

        url = self.base_url.rstrip("/") + "/chat/completions"
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", f"Bearer {self.api_key}")
        req.add_header("Cache-Control", "no-cache")

        try:
            with urllib.request.urlopen(req, timeout=self.stream_timeout_s) as resp:
                accumulated_prompt = 0
                accumulated_completion = 0
                model_used = self.model

                for raw_line in resp:
                    line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                    if not line or not line.startswith("data: "):
                        continue

                    payload_str = line[6:]
                    if payload_str.strip() == "[DONE]":
                        break

                    try:
                        evt = json.loads(payload_str)
                        model_used = evt.get("model", self.model)

                        # 流式最后一帧（usage-only chunk）：累加 token 用量
                        usage = evt.get("usage")
                        if usage:
                            accumulated_prompt = int(usage.get("prompt_tokens", 0))
                            accumulated_completion = int(usage.get("completion_tokens", 0))

                        choices = evt.get("choices") or []
                        if not choices:
                            continue

                        delta = choices[0].get("delta") or {}
                        content = delta.get("content") or ""
                        if content:
                            yield content

                    except (json.JSONDecodeError, KeyError, IndexError):
                        continue

                # 流式完成后更新 last_usage
                self.last_usage = TokenUsage(
                    prompt_tokens=accumulated_prompt,
                    completion_tokens=accumulated_completion,
                    total_tokens=accumulated_prompt + accumulated_completion,
                    model=model_used,
                )

        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"LLM HTTP error {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"LLM connection error: {exc}") from exc
