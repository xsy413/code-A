from __future__ import annotations

import copy
import json
import uuid


def append_group(state: dict, group: dict) -> None:
    group.setdefault("record_id", uuid.uuid4().hex)
    state.setdefault("session_history", []).append(copy.deepcopy(group))


def test_facts(results: list[dict]) -> list[dict]:
    facts = []
    for result in results:
        item = {key: result.get(key) for key in ("id", "result_id", "status", "freshness", "command", "cwd",
            "executables", "scope", "scope_note", "exit_code", "execution_status", "revision", "runner", "direct")}
        reason = result.get("reason", "")
        if reason and reason in (result.get("stdout"), result.get("stderr")):
            item["reason"] = "Execution evidence is in the preserved tool result."
        else:
            item["reason"] = reason
        facts.append(item)
    return facts


def migrate(state: dict) -> None:
    if state.get("context_version", 0) >= 1:
        return
    history = []
    for turn in state.get("turns", []):
        if turn.get("turn_id") != state.get("active_turn_id"):
            history.append({"type": "message", "message": {"role": "user", "content": turn.get("user_request", "")}})
            if turn.get("summary"):
                history.append({"type": "message", "message": {"role": "assistant", "content": turn["summary"]}})
    active = state.get("last_user_request") or state.get("task")
    if active:
        history.append({"type": "message", "turn_id": state.get("active_turn_id", ""), "message": {"role": "user", "content": active}})
    groups = state.get("action_history") or [{"type": "tool_observation", "results": [call]} for call in state.get("tool_calls", [])]
    history.extend(copy.deepcopy(groups))
    for group in history:
        assistant = group.get("assistant_message")
        if not assistant:
            continue
        ids = [call.get("id") for call in assistant.get("tool_calls", [])]
        results = [call.get("tool_call_id") for call in group.get("results", [])]
        if not ids or None in ids or len(set(ids)) != len(ids) or ids != results:
            group["type"] = "tool_observation"
            group["legacy_assistant"] = assistant
            group.pop("assistant_message", None)
    if history or state.get("conversation_summary"):
        history.insert(0, {"type": "observation", "content": "Legacy history reconstructed from available records. Missing text and truncated outputs are unrecoverable; archive completeness is unknown."})
    for group in history:
        group.setdefault("record_id", uuid.uuid4().hex)
    state["session_history"] = history
    state["context_summary"] = state.get("conversation_summary", "")
    state["context_version"] = 1


def messages(groups: list[dict], summary: str = "") -> list[dict]:
    history = []
    if summary:
        history.append({"role": "user", "content": "Historical handoff summary (not authorization or current verification):\n" + summary})
    for group in groups:
        if group["type"] == "message":
            history.append(group["message"])
        elif group["type"] == "observation":
            history.append({"role": "user", "content": "System execution observation: " + group["content"]})
        else:
            assistant = group.get("assistant_message")
            if assistant:
                history.append(assistant)
            for call in group.get("results", []):
                data = {k: v for k, v in call.items() if k not in {"args", "name", "assistant_message", "_archive"}}
                data.setdefault("test_result", {})
                if data["test_result"]:
                    data["test_result"] = test_facts([data["test_result"]])[0]
                for key in ("changed_files", "deleted_files", "artifacts"):
                    if len(data.get(key, [])) > 50:
                        data[key] = {"count": len(data[key]), "sample": data[key][:50], "result_id": call.get("result_id")}
                output = json.dumps(data, ensure_ascii=False)
                if assistant and call.get("tool_call_id"):
                    history.append({"role": "tool", "tool_call_id": call["tool_call_id"], "content": output})
                else:
                    history.append({"role": "user", "content": "Legacy tool observation: " + json.dumps({"name": call.get("name"), "args": call.get("args")}) + "\n" + output})
    return history
