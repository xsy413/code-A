from __future__ import annotations

import copy
import io
import json
from unittest.mock import patch

import pytest

from app.agent import CodingAgent
from app.context.config import ContextConfig
from app.context.manager import ContextManager, ContextPaused
from app.context.memory import append_group, messages
from app.context.paging import page_text
from app.context.results import present, read_archive
from app.context.tokens import TokenCounter
from app.llm import ActionResponse, OpenAICompatClient
from app.sandbox import SandboxPolicy
from app.store import SQLiteStore
from app.tools import ToolExecutor, ToolInput, ToolResult
from tests import FakeLLM, make_settings, make_state, make_workspace, tool_response


class CompactModel:
    model = "compact-test"
    last_usage = None

    def __init__(self, responses=None):
        self.responses = list(responses or ["<analysis>PRIVATE DRAFT</analysis><summary>Keep the authorized task and next step.</summary>"])
        self.calls = []

    def complete_compact(self, system, prompt):
        self.calls.append((system, prompt))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def setup(config=None):
    workspace = make_workspace()
    settings = make_settings(workspace, context=config or ContextConfig())
    store = SQLiteStore(settings.db_path)
    tools = ToolExecutor(SandboxPolicy(workspace))
    tools.store, tools.context = store, settings.context
    state = make_state(workspace, context_version=1)
    store.upsert_state("s1", state)
    return workspace, settings, store, tools, state


def archive(store, state, result, name="bash", args=None):
    record = {"name": name, "args": args or {}, "ok": result.ok, "result_id": result.result_id,
              "stdout": result.stdout, "stderr": result.stderr, "output_meta": result.output_meta,
              "_archive": result.archive_streams or {"stdout": result.stdout, "stderr": result.stderr}}
    store.save_action_result(state["session_id"], state, "execute", record)
    return record


def large_history(state, count=12):
    for i in range(count):
        response = tool_response("read_file", {"path": f"{i}.txt"}, call_id=f"call{i}")
        append_group(state, {"type": "tool_batch", "assistant_message": response.to_message(),
                             "results": [{"tool_call_id": f"call{i}", "result_id": f"result{i}", "ok": True,
                                          "stdout": (f"evidence{i} " * 2000), "stderr": ""}]})


def small_config(**kwargs):
    return ContextConfig(window=15000, output_reserve=2000, safety_margin=1000,
                         target_tokens=7000, recent_tokens=2000,
                         compact_window=200000, compact_output_tokens=4000, compact_summary_tokens=2000, **kwargs)


def test_default_budget_and_explicit_smaller_window():
    assert ContextConfig().input_limit == 150000
    config = small_config()
    config.validate()
    assert config.input_limit == 12000
    with pytest.raises(ValueError):
        ContextConfig(window=20000).validate()


def test_counter_includes_full_parameters_schemas_and_protocol_allowance():
    counter = TokenCounter("unknown-service-alias")
    small = counter.request([{"role": "assistant", "tool_calls": [{"function": {"arguments": "short"}}]}], [])
    large = counter.request([{"role": "assistant", "tool_calls": [{"function": {"arguments": "long argument " * 4000}}]}], [{"description": "schema " * 2000}])
    assert large["tokens"] > small["tokens"] + 4000
    assert small["estimated"] and small["factor"] == 1.2
    assert small["raw_tokens"] >= 288


def test_local_tokenizer_load_does_not_download_or_execute_project_code():
    from tokenizers import Tokenizer, models, pre_tokenizers
    workspace = make_workspace()
    path = workspace / "tokenizer.json"
    tokenizer = Tokenizer(models.WordLevel({"[UNK]": 0, "hello": 1}, unk_token="[UNK]"))
    tokenizer.pre_tokenizer = pre_tokenizers.Whitespace()
    tokenizer.save(str(path))
    counter = TokenCounter("alias", str(path))
    assert counter.text("hello hello") == 2
    assert counter.identity.startswith("local:")


def test_calibration_only_increases_and_survives_new_manager():
    _, settings, store, _, state = setup()
    manager = ContextManager(settings, store)
    state["context_stats"] = manager.count(state, "system", "current", [], [])
    raw = state["context_stats"]["raw_tokens"]
    manager.calibrate(state, raw * 2)
    manager.calibrate(state, raw)
    second = ContextManager(settings, store)
    assert second.count({}, "system", "current", [], [])["factor"] == 2


def test_long_unicode_line_paginates_without_loss_or_stall():
    text = "\u4e2d\u6587" * 25000 + "\nlast\n"
    position, bodies = {"line_start": 1, "column_start": 1}, []
    while position:
        body, meta = page_text(text, chars=20000, lines=2000, **position)
        assert len(body) <= 20000
        bodies.append(body)
        position = meta["next"]
    assert "".join(bodies) == text


def test_line_limit_is_independent_of_character_limit():
    body, meta = page_text("x\n" * 3000)
    assert len(body.splitlines()) == 2000
    assert meta["next"] == {"line_start": 2001, "column_start": 1}
    with pytest.raises(ValueError):
        page_text("x", column_start=4)


def test_file_pages_have_version_and_archive_does_not_claim_current_source():
    workspace, _, store, tools, state = setup()
    path = workspace / "source.txt"
    path.write_text("abc\n" * 3000)
    result = tools.execute(ToolInput("read_file", {"path": "source.txt"}, str(workspace), session_id="s1"))
    assert len(result.stdout.splitlines()) == 2000
    archive(store, state, result, "read_file", {"path": "source.txt"})
    second = tools.execute(ToolInput("read_file", {"path": "source.txt", "expected_version": result.output_meta["version"], **result.output_meta["next"]}, str(workspace), session_id="s1"))
    assert len(second.stdout.splitlines()) == 1000
    path.write_text("changed")
    stale = tools.execute(ToolInput("read_file", {"path": "source.txt", "expected_version": result.output_meta["version"]}, str(workspace), session_id="s1"))
    assert stale.error_kind == "version_changed"
    archived = tools.execute(ToolInput("read_tool_result", {"result_id": result.result_id, "line_start": 2500}, str(workspace), session_id="s1"))
    assert archived.ok and archived.output_meta["historical"]
    assert "abc" in archived.stdout
    other = tools.execute(ToolInput("read_tool_result", {"result_id": result.result_id}, str(workspace), session_id="other"))
    assert not other.ok and other.execution_status == "denied"


@pytest.mark.parametrize("name", ["list_files", "search_text"])
def test_cursor_uses_archived_query_and_cannot_be_rebound(name):
    config = ContextConfig()
    config.limits[name]["items"] = 2
    workspace, _, store, tools, state = setup(config)
    for index in range(5):
        (workspace / f"{index}.txt").write_text("needle\n")
    args = {"pattern": "*.txt"} if name == "list_files" else {"pattern": "needle", "include_glob": "*.txt"}
    first = tools.execute(ToolInput(name, args, str(workspace), session_id="s1"))
    assert first.output_meta["cursor"]
    archive(store, state, first, name, args)
    (workspace / "new.txt").write_text("needle\n")
    second = tools.execute(ToolInput(name, {**args, "cursor": first.output_meta["cursor"]}, str(workspace), session_id="s1"))
    assert second.ok and "2.txt" in second.stdout and "3.txt" in second.stdout
    assert "new.txt" not in second.stdout
    mismatch = tools.execute(ToolInput(name, {**args, "pattern": "other", "cursor": first.output_meta["cursor"]}, str(workspace), session_id="s1"))
    assert not mismatch.ok


def test_result_quota_retains_head_tail_and_marks_unrecoverable_gap():
    _, settings, store, _, state = setup()
    store.result_bytes = store.session_bytes = 1000
    result = present(ToolResult(True, stdout="".join(f"line{i:04d}\n" for i in range(1000))), "bash", {}, "cwd", settings.context)
    archive(store, state, result)
    assert not result.output_meta["archive_complete"]
    gap = read_archive(store, "s1", {"result_id": result.result_id, "line_start": 500}, settings.context)
    assert gap.error_kind == "archive_gap"
    tail = read_archive(store, "s1", {"result_id": result.result_id, "line_start": 999}, settings.context)
    assert tail.ok and "line0998" in tail.stdout
    full = store.get_context_details("s1")
    assert full["archive_bytes"] <= 1000 and full["incomplete_results"] == 1
    another = present(ToolResult(True, stdout="second" * 1000), "bash", {}, "cwd", settings.context)
    archive(store, state, another)
    assert store.get_result("s1", another.result_id)


@pytest.mark.parametrize("name", ["write_file", "patch_file", "delete_file", "bash", "powershell", "inspect_workspace"])
def test_tool_presentation_does_not_truncate_structured_status(name):
    config = ContextConfig()
    body = json.dumps({"top_entries": [f"entry{i}" for i in range(100)]}) if name == "inspect_workspace" else "H" * 10000 + "T" * 30000
    result = present(ToolResult(True, stdout=body, exit_code=7, changed_files=["a.py"],
                                permission={"decision": "allow"}), name, {}, "cwd", config)
    assert len(result.stdout) <= config.limits[name]["chars"]
    assert result.exit_code == 7 and result.changed_files == ["a.py"]
    assert result.permission["decision"] == "allow"
    if name in {"bash", "powershell"}:
        assert result.stdout.startswith("H" * 5000) and result.stdout.endswith("T" * 10000)


def test_compaction_atomically_replaces_prefix_and_discards_private_draft():
    _, settings, store, _, state = setup(small_config())
    large_history(state)
    store.upsert_state("s1", state)
    model = CompactModel()
    manager = ContextManager(settings, store, model)
    history = manager.prepare(state, "system", "current request", [])
    assert len(model.calls) == 1
    assert state["context_summary"] == "Keep the authorized task and next step."
    assert "PRIVATE DRAFT" not in json.dumps(state)
    assert "PRIVATE DRAFT" not in json.dumps(store.get_context_details("s1"))
    assert state["context_stats"]["tokens"] <= settings.context.input_limit
    saved = store.load_state("s1")
    assert saved["context_summary"] == state["context_summary"]
    assert len(saved["session_history"]) < 12
    for i, message in enumerate(history):
        if message.get("tool_calls"):
            assert history[i + 1]["tool_call_id"] == message["tool_calls"][0]["id"]


def test_format_retry_does_not_drop_original_information_and_pauses_after_two_attempts():
    _, settings, store, _, state = setup(small_config())
    large_history(state)
    original = copy.deepcopy(state["session_history"])
    model = CompactModel(["broken summary", "<summary>unfinished"])
    with pytest.raises(ContextPaused):
        ContextManager(settings, store, model).prepare(state, "system", "current", [])
    assert len(model.calls) == 2 and model.calls[0][1] == model.calls[1][1]
    assert state["session_history"] == original
    assert store.get_context_details("s1")["compactions"][-1]["status"] == "failed"
    assert state["compact_usage"]["calls"] == state["compact_usage"]["errors"] == 2


def test_compaction_transaction_failure_keeps_original_view():
    _, settings, store, _, state = setup(small_config())
    large_history(state)
    original = copy.deepcopy(state["session_history"])
    with patch.object(store, "commit_compaction", side_effect=OSError("disk full")):
        with pytest.raises(ContextPaused):
            ContextManager(settings, store, CompactModel(["<summary>ok</summary>"] * 2)).prepare(state, "sys", "prompt", [])
    assert state["session_history"] == original


def test_budget_reduction_protects_latest_five_responses_and_preserves_ids():
    _, _, _, _, state = setup()
    large_history(state)
    prefix = copy.deepcopy(state["session_history"])
    assert ContextManager._downgrade(prefix, state["session_history"], TokenCounter("alias"))
    changed = [i for i, group in enumerate(prefix) if group != state["session_history"][i]]
    assert len(changed) == 5 and max(changed) < 7
    assert prefix[-5:] == state["session_history"][-5:]
    assert all(prefix[i]["results"][0]["result_id"] == f"result{i}" for i in changed)


def test_no_fixed_history_trim_across_ten_turns_and_no_duplicate_recent_turn_prompt():
    workspace, settings, _, _, _ = setup()
    agent = CodingAgent(settings)
    model = FakeLLM(action_responses=[ActionResponse(f"answer{i}", (), "stop") for i in range(10)])
    agent.nodes.llm = model
    sid = agent.start_session(workspace)
    for index in range(10):
        state = agent.run_turn(sid, f"request{index}", workspace)
    assert len(state["turns"]) == 10 and len(state["session_history"]) == 20
    assert "request0" in json.dumps(model.action_calls[-1]["history"])
    assert "answer0" not in model.action_calls[-1]["user_prompt"]


def test_oversized_recent_request_pauses_then_resume_does_not_charge_tools():
    workspace, settings, _, _, _ = setup(small_config())
    agent = CodingAgent(settings)
    model = FakeLLM(action_responses=[ActionResponse("done", (), "stop")])
    agent.nodes.llm = model
    state = agent.run("current authorized request " * 10000, workspace)
    assert state["status"] == "awaiting_context" and not model.action_calls
    assert state["tool_call_count"] == state["retry_attempts"] == 0
    settings.context.window = 200000
    resumed = agent.resume(state["session_id"])
    assert resumed["summary"] == "done" and resumed["tool_call_count"] == 0


def test_compact_client_requires_complete_normal_response_and_excludes_tools():
    client = OpenAICompatClient("key", "model", "https://example.com")
    response = {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "<summary>facts</summary>", "reasoning_content": "private"}}]}
    with patch("urllib.request.urlopen", return_value=io.BytesIO(json.dumps(response).encode())) as request:
        assert client.complete_compact("system", "history") == "<summary>facts</summary>"
    payload = json.loads(request.call_args.args[0].data)
    assert "tools" not in payload and payload["max_tokens"] == 25000
    response["choices"][0]["finish_reason"] = "length"
    with patch("urllib.request.urlopen", return_value=io.BytesIO(json.dumps(response).encode())):
        with pytest.raises(RuntimeError):
            client.complete_compact("system", "history")


@pytest.mark.parametrize("decision", ["ask", "deny"])
def test_custom_archive_and_source_permissions_are_not_bypassed(decision):
    workspace, _, store, tools, state = setup()
    (workspace / "a.txt").write_text("ordinary source")
    original = tools.execute(ToolInput("read_file", {"path": "a.txt"}, str(workspace), session_id="s1"))
    archive(store, state, original, "read_file", {"path": "a.txt"})
    policy = workspace / ".user" / "permissions.json"
    policy.parent.mkdir()
    rules = {"version": 1, "rules": [{"id": "source-rule", "tool": "read_file", "path": "*/a.txt", "decision": decision}], "overrides": []}
    policy.write_text(json.dumps(rules))
    tools.permissions.config_path = policy
    payload = ToolInput("read_tool_result", {"result_id": original.result_id}, str(workspace), session_id="s1")
    assert tools.permission_for(payload).decision == decision
    result = tools.execute(payload)
    assert result.execution_status == ("denied" if decision == "deny" else "needs_approval")
    rules["rules"] = [{"id": "archive-rule", "tool": "read_tool_result", "decision": "deny"}]
    policy.write_text(json.dumps(rules))
    assert tools.permission_for(payload).decision == "deny"


def test_redacted_archive_and_internal_directory_protection():
    workspace, settings, store, tools, state = setup()
    tools.permissions.register_secret("known-secret-123456789")
    store.sanitize = tools.permissions.redact
    result = present(ToolResult(True, stdout="before\nknown-secret-123456789\nafter\n"), "bash", {}, str(workspace), settings.context)
    archive(store, state, result)
    saved = store.get_result("s1", result.result_id)
    assert "known-secret-123456789" not in json.dumps(saved)
    assert tools.permissions.evaluate("read_file", {"path": ".agent/agent.db"}, workspace, "s1").decision != "allow"


def test_tokenizer_and_budget_environment_are_user_owned():
    workspace, settings, _, _, _ = setup()
    tokenizer = workspace / "tokenizer.json"
    tokenizer.write_text("{}")
    settings.context.tokenizer = str(tokenizer)
    agent = CodingAgent(settings)
    for path, content in [("tokenizer.json", "{}"), (".env", "CONTEXT_WINDOW=999999\n")]:
        assert agent.tools.permission_for(ToolInput("write_file", {"path": path, "content": content}, str(workspace), session_id="s1")).decision == "deny"


def test_bounded_capture_retains_long_line_tail_and_precise_columns():
    from app.tools.shell import _Capture
    capture = _Capture(1000)
    body = "\u4e2d" * 1000
    for offset in range(0, len(body.encode()), 73):
        capture.feed(body.encode()[offset:offset + 73])
    capture.feed(b"", final=True)
    text, parts = capture.finish()
    assert capture.truncated and text.endswith("\u4e2d" * 100)
    assert sum(len(p["body"].encode()) for p in parts) <= 1000
    assert parts[-1]["start_line"] == 1 and parts[-1]["start_column"] > 1
    assert parts[-1]["start_column"] + len(parts[-1]["body"]) == 1001


def test_archive_long_line_gap_and_tail_pagination():
    _, settings, store, _, state = setup()
    store.result_bytes = store.session_bytes = 1000
    result = present(ToolResult(True, stdout="H" * 5000 + "T" * 5000), "bash", {}, "cwd", settings.context)
    archive(store, state, result)
    gap = read_archive(store, "s1", {"result_id": result.result_id, "column_start": 5000}, settings.context)
    assert gap.error_kind == "archive_gap"
    tail = read_archive(store, "s1", {"result_id": result.result_id, "column_start": 9900}, settings.context)
    assert tail.ok and tail.stdout == "T" * 101
    assert tail.output_meta["column_start"] == 9900


def test_result_persistence_failure_pauses_without_replaying_side_effect():
    workspace, settings, _, _, _ = setup()
    agent = CodingAgent(settings, confirm_fn=lambda *_: "approve_once")
    model = FakeLLM(action_responses=[tool_response("write_file", {"path": "a.py", "content": "value = 1\n"}), ActionResponse("Recovered factually.", (), "stop")])
    agent.nodes.llm = model
    original = agent.store.save_action_result
    with patch.object(agent.store, "save_action_result", side_effect=OSError("disk full")):
        state = agent.run("write once", workspace)
    assert state["status"] == "awaiting_context"
    assert (workspace / "a.py").read_text() == "value = 1\n"
    assert state["pending_batch"]["calls"][0]["state"] == "running"
    with patch.object(agent.tools, "execute", side_effect=AssertionError("must not replay")):
        resumed = agent.resume(state["session_id"])
    assert resumed["summary"] == "Recovered factually."
    assert resumed["tool_calls"][0]["execution_status"] == "indeterminate"
    assert resumed["tool_call_count"] == 1


@pytest.mark.parametrize("status", ["passed", "failed", "incomplete"])
def test_latest_static_facts_survive_actual_compaction_in_next_act(status):
    config = small_config()
    config.window, config.target_tokens = 30000, 14000
    workspace, settings, _, _, state = setup(config)
    agent = CodingAgent(settings)
    model = FakeLLM(action_responses=[ActionResponse("Explained.", (), "stop")])
    agent.nodes.llm = model
    agent.nodes.context_manager.compact_client = CompactModel()
    large_history(state)
    state.update(status="acting", static_check={"status": status, "errors": ["actual failure"] if status == "failed" else [],
                                                "snapshot_complete": status != "incomplete"})
    agent.store.upsert_state("s1", state)
    result = agent.resume("s1")
    assert result["status"] == "finished" and result["context_summary"]
    assert f'"status": "{status}"' in model.action_calls[0]["user_prompt"]
    assert result["static_check"]["status"] == status


def test_threshold_exactly_triggers_compaction_and_recent_batch_is_whole():
    _, settings, store, _, state = setup()
    large_history(state, 8)
    model = CompactModel()
    manager = ContextManager(settings, store, model)
    before = manager.count(state, "sys", "current", messages(state["session_history"]), [])
    settings.context.window = before["tokens"] + settings.context.output_reserve + settings.context.safety_margin
    settings.context.target_tokens = before["tokens"] // 2
    settings.context.recent_tokens = 100
    prepared = manager.prepare(state, "sys", "current", [])
    assert len(model.calls) == 1
    assert len(state["session_history"]) == 1
    assert prepared[-1]["tool_call_id"] == prepared[-2]["tool_calls"][0]["id"]


def test_capacity_error_downgrades_older_results_once_then_retries_unchanged():
    from app.llm.client import LLMHTTPError
    _, settings, store, _, state = setup(small_config())
    large_history(state)
    model = CompactModel([LLMHTTPError(400, '{"error":{"code":"context_length_exceeded"}}'), RuntimeError("temporary network failure"), "<summary>ready</summary>"])
    ContextManager(settings, store, model).prepare(state, "sys", "current", [])
    assert len(model.calls) == 3
    assert "Archived body omitted" not in model.calls[0][1]
    assert "Archived body omitted" in model.calls[1][1]
    assert model.calls[1][1] == model.calls[2][1]
    assert state["retry_attempts"] == state["tool_call_count"] == 0


def test_context_cli_does_not_invoke_model_and_hides_summary_body(capsys):
    from app.cli import _print_context
    workspace, settings, _, _, _ = setup()
    agent = CodingAgent(settings)
    sid = agent.start_session(workspace)
    with patch.object(OpenAICompatClient, "complete_action", side_effect=AssertionError("no model")):
        _print_context(agent, sid)
    output = capsys.readouterr().out
    assert "current_request_estimate" in output and "150000" in output


def test_partial_legacy_native_batch_is_an_observation_not_a_fake_pair():
    from app.context.memory import migrate
    from app.llm import ToolCall
    _, _, _, _, state = setup()
    state["context_version"] = 0
    response = ActionResponse("", (ToolCall("one", "list_files", {}), ToolCall("two", "list_files", {})), "tool_calls")
    state["action_history"] = [{"type": "tool_batch", "assistant_message": response.to_message(),
                                "results": [{"tool_call_id": "one", "stdout": "available"}]}]
    migrate(state)
    history = messages(state["session_history"])
    assert not any(m.get("tool_calls") or m["role"] == "tool" for m in history)
    assert "available" in json.dumps(history)


@pytest.mark.parametrize("shell", ["bash", "powershell"])
def test_lru_ttl_replay_keeps_more_than_six_results_and_test_output_tail(shell):
    from app.llm import ToolCall
    import shlex
    import sys
    workspace, settings, _, _, _ = setup()
    agent = CodingAgent(settings, confirm_fn=lambda *_: "approve_once")
    if shell not in agent.permissions.shells:
        pytest.skip("Backend absent")
    implementation = '''from collections import OrderedDict

class Cache:
    def __init__(self, capacity, ttl=None, clock=lambda: 0):
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self.capacity, self.ttl, self.clock = capacity, ttl, clock
        self.data = OrderedDict()

    def get(self, key):
        if key not in self.data:
            return -1
        value, written = self.data[key]
        if self.ttl is not None and self.clock() - written >= self.ttl:
            del self.data[key]
            return -1
        self.data.move_to_end(key)
        return value

    def put(self, key, value):
        self.data[key] = (value, self.clock())
        self.data.move_to_end(key)
        if len(self.data) > self.capacity:
            self.data.popitem(last=False)
'''
    tests = '''import pytest
from cache import Cache

def test_lru():
    c = Cache(2)
    assert c.get("missing") == -1
    c.put("a", 1); c.put("b", 2)
    assert c.get("a") == 1
    c.put("c", 3)
    assert c.get("b") == -1
    c.put("a", 4); c.put("d", 5)
    assert c.get("c") == -1 and c.get("a") == 4
    assert len(c.data) == 2
    with pytest.raises(ValueError):
        Cache(0)
    print("log-prefix:" + "x" * 180000)

def test_ttl():
    now = [0]
    c = Cache(2, ttl=5, clock=lambda: now[0])
    c.put("a", 1)
    now[0] = 4
    assert c.get("a") == 1
    c.put("a", 2)
    now[0] = 8
    assert c.get("a") == 2
    now[0] = 9
    assert c.get("a") == -1
'''
    initial = ActionResponse("", (
        ToolCall("implementation", "write_file", {"path": "cache.py", "content": implementation}),
        ToolCall("tests", "write_file", {"path": "test_cache.py", "content": tests}),
    ), "tool_calls")
    executable = "'" + sys.executable.replace("'", "''") + "'" if shell == "powershell" else shlex.quote(sys.executable.replace("\\", "/"))
    command = ("& " if shell == "powershell" else "") + executable + " -m pytest -q -s test_cache.py"
    model = FakeLLM(action_responses=[initial,
        *[tool_response("read_file", {"path": "cache.py" if i % 2 else "test_cache.py"}, call_id=f"read{i}") for i in range(8)],
        tool_response(shell, {"command": command}, call_id="test"),
        ActionResponse("Implemented and verified.", (), "stop")])
    agent.nodes.llm = model
    state = agent.run("Implement LRU with TTL and selected tests", workspace)
    assert state["status"] == "finished"
    results = [json.loads(m["content"]) for m in model.action_calls[-1]["history"] if m["role"] == "tool"]
    assert len(results) == 11
    assert "2 passed" in results[-1]["stdout"]
    assert len(results[-1]["stdout"]) <= 20000
    assert len(agent.store.get_result(state["session_id"], results[-1]["result_id"])["chunks"][0]["body"]) > 180000
    assert sum(call["name"] in {"bash", "powershell"} for call in state["tool_calls"]) == 1
    assert len([event for event in agent.get_logs(state["session_id"]) if event.node_name == "verify"]) == 1
    assert state["test_results"][-1]["status"] == "passed"
    assert state["test_results"][-1]["scope"]["targets"] == ["test_cache.py"]
