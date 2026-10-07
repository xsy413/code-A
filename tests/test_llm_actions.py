from __future__ import annotations

import io
import json
import urllib.error
from unittest.mock import patch

import pytest

from app.llm import OpenAICompatClient
from app.tools.schemas import TOOL_SCHEMAS


def make_client() -> OpenAICompatClient:
    return OpenAICompatClient(api_key="test-key", model="test-model", base_url="https://example.com/v1")


def response(*, content="Final answer.", tool_calls=None, reason="stop") -> dict:
    return {"choices": [{"finish_reason": reason, "message": {
        "role": "assistant", "content": content, "tool_calls": tool_calls,
    }}], "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13}}


def raw_call(arguments='{"path":"a.py"}', *, call_id="call_a") -> dict:
    return {"id": call_id, "type": "function", "function": {"name": "read_file", "arguments": arguments}}


def test_complete_action_sends_native_tools_and_records_usage() -> None:
    client = make_client()
    history = [{"role": "user", "content": "Earlier observation"}]
    body = io.BytesIO(json.dumps(response(content=" Exact answer.\n")).encode())
    with patch("urllib.request.urlopen", return_value=body) as request:
        action = client.complete_action("system", "current request", tools=TOOL_SCHEMAS, history=history)

    payload = json.loads(request.call_args.args[0].data)
    assert payload["tool_choice"] == "auto"
    assert payload["parallel_tool_calls"] is True
    assert "response_format" not in payload
    assert payload["messages"] == [{"role": "system", "content": "system"}, *history,
                                   {"role": "user", "content": "current request"}]
    assert {item["function"]["name"] for item in payload["tools"]} == {
        "inspect_workspace", "list_files", "read_file", "search_text",
        "write_file", "patch_file", "delete_file", "bash", "powershell", "read_tool_result",
    }
    assert action.content == " Exact answer.\n"
    assert action.tool_calls == ()
    assert action.finish_reason == "stop"
    assert client.last_usage.total_tokens == 13


def test_complete_action_parses_native_tool_and_accompanying_text() -> None:
    client = make_client()
    with patch.object(OpenAICompatClient, "_post", return_value=response(
        content="Inspecting.", tool_calls=[raw_call()], reason="tool_calls",
    )):
        action = client.complete_action("system", "request", tools=TOOL_SCHEMAS)

    assert action.content == "Inspecting."
    assert action.tool_calls[0].id == "call_a"
    assert action.tool_calls[0].name == "read_file"
    assert action.tool_calls[0].args == {"path": "a.py"}
    message = action.to_message()
    assert message["role"] == "assistant"
    assert json.loads(message["tool_calls"][0]["function"]["arguments"]) == {"path": "a.py"}


@pytest.mark.parametrize("body", [
    response(content=""),
    response(content="  "),
    response(content=None),
    response(reason="length"),
    response(reason="content_filter"),
    response(reason=None),
    response(tool_calls=[], reason="tool_calls"),
    response(content=[{"type": "text", "text": "bad shape"}]),
    response(tool_calls={}, reason="tool_calls"),
    response(tool_calls=[raw_call(), raw_call()], reason="tool_calls"),
    response(tool_calls=[raw_call()], reason="stop"),
    response(tool_calls=[raw_call(None)], reason="tool_calls"),
    response(tool_calls=[raw_call(call_id="")], reason="tool_calls"),
    response(tool_calls=[{"id": "call", "type": "function"}], reason="tool_calls"),
    response(tool_calls=[{"id": "call", "type": "other"}], reason="tool_calls"),
    {"choices": []},
    {"choices": [{"finish_reason": "stop"}]},
])
def test_complete_action_rejects_abnormal_or_malformed_responses(body) -> None:
    client = make_client()
    with patch.object(OpenAICompatClient, "_post", return_value=body) as post:
        with pytest.raises(RuntimeError):
            client.complete_action("system", "request", tools=TOOL_SCHEMAS)
    assert post.call_count == 1


@pytest.mark.parametrize("arguments", ["broken JSON", "[]", "null"])
def test_malformed_arguments_keep_call_identity_and_original_wire_message(arguments):
    client = make_client()
    with patch.object(OpenAICompatClient, "_post", return_value=response(
        tool_calls=[raw_call(), raw_call(arguments, call_id="bad")], reason="tool_calls",
    )):
        action = client.complete_action("system", "request", tools=TOOL_SCHEMAS)
    assert len(action.tool_calls) == 2
    assert action.tool_calls[0].args == {"path": "a.py"}
    assert action.tool_calls[1].args is None
    assert action.tool_calls[1].argument_error
    assert action.to_message()["tool_calls"][1]["function"]["arguments"] == arguments


def test_multiple_native_calls_are_accepted_in_order():
    client = make_client()
    with patch.object(OpenAICompatClient, "_post", return_value=response(
        tool_calls=[raw_call(), raw_call(call_id="second")], reason="tool_calls",
    )):
        action = client.complete_action("system", "request", tools=TOOL_SCHEMAS)
    assert [call.id for call in action.tool_calls] == ["call_a", "second"]


def test_complete_action_connection_error_clears_stale_usage() -> None:
    client = make_client()
    with patch("urllib.request.urlopen", return_value=io.BytesIO(json.dumps(response()).encode())):
        client.complete_action("system", "request", tools=TOOL_SCHEMAS)
    assert client.last_usage.total_tokens == 13
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("offline")):
        with pytest.raises(RuntimeError, match="connection error"):
            client.complete_action("system", "request", tools=TOOL_SCHEMAS)
    assert client.last_usage is None
