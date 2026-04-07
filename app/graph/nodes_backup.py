from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable

from app.config import Settings
from app.graph.state import AgentState
from app.llm import LLMClient
from app.prompts import ACTION_PROMPT, PLAN_PROMPT, REFLECT_PROMPT, SUMMARY_PROMPT, SYSTEM_PROMPT
from app.store import SQLiteStore
from app.tools import ToolExecutor, ToolInput


class AgentNodes:
    def __init__(
        self,
        llm: LLMClient,
        store: SQLiteStore,
        tools: ToolExecutor,
        settings: Settings,
        confirm_fn: Callable[[str, dict], bool] | None = None,
        stream_fn: Callable[[str], None] | None = None,
    ) -> None:
        self.llm = llm
        self.store = store
        self.tools = tools
        self.settings = settings
        # 交互确认回调：(tool_name, args) -> bool
        # 为 None 时退回 auto_confirm_risky_writes 配置
        self.confirm_fn = confirm_fn
        # 流式输出回调：(text_chunk) -> None  为 None 时退回 complete_text
        self.stream_fn = stream_fn

    def _record(self, state: AgentState, node_name: str, started: float, out: str, error: str = "") -> None:
        session_id = state["session_id"]
        duration_ms = int((time.time() - started) * 1000)
        self.store.add_event(
            session_id=session_id,
            node_name=node_name,
            duration_ms=duration_ms,
            input_summary=state.get("status", ""),
            output_summary=out[:500],
            error=error[:500],
        )
        self.store.upsert_state(session_id, state)

    def _normalize_failure(self, text: str) -> str:
        lowered = (text or "").lower().strip()
        if "invalid_write_path" in lowered:
            return "real_failure:invalid_path"
        if "no_code_change_yet" in lowered:
            return "no_code_change_yet"
        if "no tests collected" in lowered:
            return "verification_unavailable:no_tests"
        if "syntaxerror" in lowered or "traceback" in lowered:
            return "real_failure:runtime"
        if "permission" in lowered:
            return "real_failure:permission"
        return lowered[:200]

    def _conversation_summary(self, state: AgentState) -> str:
        summary = str(state.get("conversation_summary", "")).strip()
        return summary if summary else "(none)"

    def _recent_turns_text(self, state: AgentState) -> str:
        turns = list(state.get("turns", []))
        if not turns:
            return "(none)"

        active_turn_id = str(state.get("active_turn_id", ""))
        previous_turns = [t for t in turns if str(t.get("turn_id", "")) != active_turn_id]
        if not previous_turns:
            return "(none)"

        max_turns = max(1, self.settings.max_context_turns)
        recent = previous_turns[-max_turns:]
        lines: list[str] = []
        for turn in recent:
            req = str(turn.get("user_request", "")).replace("\n", " ").strip()
            status = str(turn.get("status", ""))
            summary = str(turn.get("summary", "")).replace("\n", " ").strip()
            if len(req) > 120:
                req = req[:117] + "..."
            if len(summary) > 120:
                summary = summary[:117] + "..."
            lines.append(f"- [{status}] user: {req} | summary: {summary}")
        return "\n".join(lines) if lines else "(none)"

    def _current_request(self, state: AgentState) -> str:
        return str(state.get("last_user_request") or state.get("task") or "")

    def _has_test_entry(self, workspace: Path) -> bool:
        if (workspace / "tests").exists():
            return True
        for p in workspace.rglob("test_*.py"):
            if p.is_file():
                return True
        for p in workspace.rglob("*_test.py"):
            if p.is_file():
                return True
        return False

    def _validate_finish(self, state: AgentState, changed_files: list[str], reason: str) -> str | None:
        if not changed_files:
            return "finish requires args.changed_files."
        if not reason.strip():
            return "finish requires args.completion_reason."

        workspace = Path(state["workspace"]).resolve()
        # 已被 delete_file 删除的文件不需要检查存在性
        deleted = set(state.get("deleted_files", []))

        for rel_path in changed_files:
            path = (workspace / rel_path).resolve()
            try:
                path.relative_to(workspace)
            except ValueError:
                return f"finish changed file outside workspace: {rel_path}"

            # 已删除文件跳过后续检查
            if rel_path in deleted:
                continue

            if not path.exists() or not path.is_file():
                return f"finish changed file not found: {rel_path}"
            if path.suffix == ".py":
                try:
                    source = path.read_text(encoding="utf-8")
                    compile(source, str(path), "exec")
                except Exception as exc:
                    return f"py_compile check failed for {rel_path}: {exc}"
        return None

    def _bump_tool_budget(self, state: AgentState) -> bool:
        count = int(state.get("tool_call_count", 0)) + 1
        state["tool_call_count"] = count
        if count > self.settings.max_tool_calls:
            state["status"] = "failed"
            state["error"] = f"Reached MAX_TOOL_CALLS={self.settings.max_tool_calls}."
            return False
        return True

    def _track_failure(self, state: AgentState, failure: str) -> bool:
        signature = self._normalize_failure(failure)
        recent = list(state.get("recent_failures", []))
        recent.append(signature)
        state["recent_failures"] = recent[-3:]
        if len(state["recent_failures"]) >= 2 and state["recent_failures"][-1] == state["recent_failures"][-2]:
            state["status"] = "failed"
            state["error"] = (
                "Detected repeated identical failure twice; short-circuiting. "
                f"latest={state['recent_failures'][-1]}"
            )
            return False
        return True

    def _is_exploration_tool(self, tool_name: str) -> bool:
        return tool_name in {"list_files", "read_file", "search_text"}

    def _is_write_like_tool(self, tool_name: str) -> bool:
        """写入或破坏性修改类工具（执行后应进入 verify 或标记 modified）。"""
        return tool_name in {"write_file", "patch_file", "delete_file"}

    # ── 最多保留最近 N 条工具调用作为对话历史注入给 LLM ──────────────
    _MAX_ACTION_HISTORY = 6  # 条（即 3 轮 assistant+user 对）

    def _build_action_history(self, state: AgentState) -> list[dict] | None:
        """将本轮已执行的工具调用转换为 assistant/user 多轮消息，注入给 act 节点。

        结构：
          assistant: 当初 LLM 做出的 JSON 决策
          user:      工具实际返回的结果

        这样 LLM 能以「自己的视角」看到完整的行动链，
        而不仅仅是靠 last_tool_output 这一条平铺文本。
        """
        tool_calls = list(state.get("tool_calls", []))
        if not tool_calls:
            return None

        # 取最后 N 条（避免 token 爆炸）
        recent = tool_calls[-self._MAX_ACTION_HISTORY :]
        history: list[dict] = []

        for call in recent:
            name = call.get("name", "")
            ok = call.get("ok", True)

            # assistant 角色：还原 LLM 当时的 JSON 决策
            llm_decision = call.get("llm_decision")
            if llm_decision:
                assistant_content = json.dumps(llm_decision, ensure_ascii=False)
            else:
                # 兼容旧记录：用工具名 + args 重建
                assistant_content = json.dumps(
                    {"tool": name, "args": call.get("args", {})},
                    ensure_ascii=False,
                )
            history.append({"role": "assistant", "content": assistant_content})

            # user 角色：工具返回的结果
            stdout = (call.get("stdout") or "")[:800]
            stderr = (call.get("stderr") or "")[:400]
            output = stdout if ok else (stderr or stdout)
            status_icon = "✓" if ok else "✗"
            history.append(
                {
                    "role": "user",
                    "content": f"[{status_icon} {name}]\n{output}",
                }
            )

        return history if history else None

    def _session_backup_dir(self, state: AgentState) -> str:
        """返回本 session 的备份根目录：<workspace>/.agent/backups/<session_id>。"""
        workspace = Path(state["workspace"]).resolve()
        session_id = str(state.get("session_id") or "")
        if not session_id:
            return ""
        return str(workspace / ".agent" / "backups" / session_id)

    def _make_payload(self, state: AgentState, name: str, args: dict) -> ToolInput:
        """封装 ToolInput 创建，自动填入 backup_dir。

        write_file / patch_file / delete_file 会读取 backup_dir 并在操作前备份。
        其他工具则忽略 backup_dir（不会触发备份逻辑）。
        """
        return ToolInput(
            name=name,
            args=args,
            cwd=state["workspace"],
            backup_dir=self._session_backup_dir(state),
        )

    def _accumulate_usage(self, state: AgentState, node_name: str) -> None:
        """LLM 调用完毕后调用：把 last_usage 累加到 state 并写入 SQLite。

        使用 getattr 而非直接属性访问，兼容任何实现了 LLMClient Protocol 的客户端。
        """
        usage = getattr(self.llm, "last_usage", None)
        if usage is None:
            return

        tu = dict(state.get("token_usage") or {})
        tu["prompt_tokens"] = tu.get("prompt_tokens", 0) + usage.prompt_tokens
        tu["completion_tokens"] = tu.get("completion_tokens", 0) + usage.completion_tokens
        tu["total_tokens"] = tu.get("total_tokens", 0) + usage.total_tokens
        tu["llm_calls"] = tu.get("llm_calls", 0) + 1
        state["token_usage"] = tu

        # 写入数据库（不抓异常，避免统计失败导致主流程中断）
        try:
            self.store.record_token_usage(
                session_id=state["session_id"],
                node_name=node_name,
                model=usage.model,
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
                total_tokens=usage.total_tokens,
            )
        except Exception:
            pass

    def _stream_or_complete_text(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        temperature: float = 0,
        history: list[dict] | None = None,
    ) -> str:
        """若 stream_fn 已设置且 LLM 支持 stream_text，就流式输出并收集文本；
        否则退回普通 complete_text。
        """
        _stream = getattr(self.llm, "stream_text", None)
        if self.stream_fn is not None and callable(_stream):
            chunks: list[str] = []
            for chunk in _stream(
                system_prompt,
                user_prompt,
                temperature=temperature,
                history=history,
            ):
                self.stream_fn(chunk)
                chunks.append(chunk)
            return "".join(chunks)
        return self.llm.complete_text(
            system_prompt, user_prompt, temperature=temperature, history=history
        )

    def intake(self, state: AgentState) -> AgentState:
        started = time.time()
        state["status"] = "planning"
        self._record(state, "intake", started, "Accepted task and moved to planning.")
        return state

    def plan(self, state: AgentState) -> AgentState:
        started = time.time()
        try:
            prompt = PLAN_PROMPT.format(
                current_request=self._current_request(state),
                workspace=state["workspace"],
                conversation_summary=self._conversation_summary(state),
                recent_turns=self._recent_turns_text(state),
            )
            if self.stream_fn:
                self.stream_fn("\n\U0001f4cb Planning...\n")
            plan_text = self._stream_or_complete_text(SYSTEM_PROMPT, prompt)
            if self.stream_fn:
                self.stream_fn("\n")
            self._accumulate_usage(state, "plan")
            state["plan"] = plan_text
            state["status"] = "acting"
            self._record(state, "plan", started, "Plan generated.")
            return state
        except Exception as exc:
            state["status"] = "failed"
            state["error"] = f"planning failed: {exc}"
            self._record(state, "plan", started, "Plan failed.", error=state["error"])
            return state

    def act(self, state: AgentState) -> AgentState:
        started = time.time()
        try:
            prompt = ACTION_PROMPT.format(
                current_request=self._current_request(state),
                plan=state.get("plan", ""),
                conversation_summary=self._conversation_summary(state),
                recent_turns=self._recent_turns_text(state),
                last_tool_output=state.get("last_tool_output", ""),
                reflection=state.get("last_reflection", ""),
            )
            if self.stream_fn:
                # 展示当前要调用哪个工具（在 LLM 决策前就显示）
                tc = state.get("tool_call_count", 0)
                self.stream_fn(f"\n\u001b[2m[act #{tc + 1}] deciding...\u001b[0m\n")
            decision = self.llm.complete_json(
                SYSTEM_PROMPT,
                prompt,
                history=self._build_action_history(state),
            )
            self._accumulate_usage(state, "act")
            tool_name = str(decision.get("tool", "finish"))
            args = decision.get("args", {})

            self.store.add_tool_call(
                state["session_id"],
                "act_decision",
                tool_name,
                args if isinstance(args, dict) else {},
                {"ok": None, "phase": "planned"},
            )

            if tool_name == "write_file":
                path = str(args.get("path", ""))
                cwd = Path(state["workspace"])
                if self.tools.is_risky_write(cwd, path) and not self.settings.auto_confirm_risky_writes:
                    state["pending_action"] = decision
                    state["status"] = "awaiting_human_confirm"
                    self._record(state, "act", started, f"Pending human confirmation for write: {path}")
                    return state

            if not self._bump_tool_budget(state):
                self._record(state, "act", started, "Tool budget exceeded.", error=state["error"])
                return state

            if tool_name == "finish":
                changed_files = args.get("changed_files") or state.get("changed_files", [])
                completion_reason = str(args.get("completion_reason", "")).strip()
                if isinstance(changed_files, str):
                    changed_files = [changed_files]
                if not isinstance(changed_files, list):
                    changed_files = []

                validation_error = self._validate_finish(
                    state,
                    [str(p) for p in changed_files],
                    completion_reason,
                )
                if validation_error:
                    state["status"] = "reflecting"
                    state["error"] = validation_error
                    state["needs_more_action"] = True
                    self._record(state, "act", started, "Finish self-check failed.", error=state["error"])
                    return state

                payload = self._make_payload(state, tool_name, args)
                result = self.tools.execute(payload)
                call = {
                    "name": tool_name,
                    "args": args,
                    "ok": result.ok,
                    "stdout": result.stdout[:2000],
                    "stderr": result.stderr[:2000],
                    "exit_code": result.exit_code,
                }
                state["tool_calls"].append(call)
                self.store.add_tool_call(state["session_id"], "act", tool_name, args, call)

                state["finish_reason"] = completion_reason
                state["changed_files"] = [str(p) for p in changed_files]
                state["needs_more_action"] = False
                state["status"] = "finished"
                self._record(state, "act", started, "Executed finish with self-check.")
                return state

            payload = self._make_payload(state, tool_name, args)
            result = self.tools.execute(payload)
            call = {
                "name": tool_name,
                "args": args,
                "ok": result.ok,
                "stdout": result.stdout[:2000],
                "stderr": result.stderr[:2000],
                "exit_code": result.exit_code,
                "llm_decision": decision,  # 保存原始决策，供历史注入时还原 assistant 消息
            }
            state["tool_calls"].append(call)
            state["artifacts"] = list({*state.get("artifacts", []), *result.artifacts})
            state["last_tool_output"] = (result.stdout or result.stderr)[:2000]
            self.store.add_tool_call(state["session_id"], "act", tool_name, args, call)

            if not result.ok:
                state["status"] = "reflecting"
                state["needs_more_action"] = True
                state["error"] = (result.stderr or result.stdout or f"tool failed: {tool_name}")[:2000]
                self._record(state, "act", started, f"Tool failed: {tool_name}", error=state["error"])
                return state

            if tool_name == "write_file":
                state["write_count"] = int(state.get("write_count", 0)) + 1
                path = str(args.get("path", "")).strip()
                if path:
                    changed = list(state.get("changed_files", []))
                    if path not in changed:
                        changed.append(path)
                    state["changed_files"] = changed

                state["turn_progress"] = "modified"
                state["explore_streak"] = 0
                state["needs_more_action"] = False
                state["status"] = "verifying"
                self._record(state, "act", started, f"Executed tool: {tool_name}; progress=modified")
                return state

            if tool_name == "patch_file":
                state["write_count"] = int(state.get("write_count", 0)) + 1
                path = str(args.get("path", "")).strip()
                if path:
                    changed = list(state.get("changed_files", []))
                    if path not in changed:
                        changed.append(path)
                    state["changed_files"] = changed

                state["turn_progress"] = "modified"
                state["explore_streak"] = 0
                state["needs_more_action"] = False
                state["status"] = "verifying"
                self._record(state, "act", started, f"Executed tool: {tool_name}; progress=modified")
                return state

            if tool_name == "delete_file":
                state["write_count"] = int(state.get("write_count", 0)) + 1
                path = str(args.get("path", "")).strip()
                if path:
                    # 记录已删除文件（供 _validate_finish 跳过存在性检查）
                    deleted = list(state.get("deleted_files", []))
                    if path not in deleted:
                        deleted.append(path)
                    state["deleted_files"] = deleted
                    # 同时记录在 changed_files 中，以便 finish 汇报
                    changed = list(state.get("changed_files", []))
                    if path not in changed:
                        changed.append(path)
                    state["changed_files"] = changed

                state["turn_progress"] = "modified"
                state["explore_streak"] = 0
                state["needs_more_action"] = False
                state["status"] = "verifying"
                self._record(state, "act", started, f"Executed tool: {tool_name}; progress=modified")
                return state

            if tool_name == "run_command":
                # run_command 不属于探索也不属于写入，执行后重置 explore_streak 并继续 acting
                state["explore_streak"] = 0
                state["needs_more_action"] = True
                state["status"] = "acting"
                self._record(state, "act", started, f"Executed run_command; continue acting")
                return state

            if self._is_exploration_tool(tool_name):
                current_progress = str(state.get("turn_progress", "none"))
                if current_progress != "modified":
                    state["turn_progress"] = "explored"

                streak = int(state.get("explore_streak", 0)) + 1
                state["explore_streak"] = streak
                state["needs_more_action"] = True

                if int(state.get("write_count", 0)) == 0 and streak > self.settings.max_explore_steps_before_write:
                    state["status"] = "reflecting"
                    state["error"] = (
                        "no_code_change_yet: too many exploration steps without write_file. "
                        "Create/modify code or finish with valid evidence."
                    )
                    self._record(state, "act", started, "Exploration threshold exceeded.", error=state["error"])
                    return state

                state["status"] = "acting"
                self._record(state, "act", started, f"Executed exploration tool: {tool_name}; continue acting")
                return state

            state["needs_more_action"] = True
            state["status"] = "acting"
            self._record(state, "act", started, f"Executed tool: {tool_name}; continue acting")
            return state
        except Exception as exc:
            state["status"] = "reflecting"
            state["needs_more_action"] = True
            state["error"] = f"act failed: {exc}"
            self._record(state, "act", started, "Action failed.", error=state["error"])
            return state

    def human_confirm(self, state: AgentState) -> AgentState:
        started = time.time()
        pending = state.get("pending_action") or {}
        if not pending:
            state["status"] = "failed"
            state["error"] = "No pending action for human_confirm."
            self._record(state, "human_confirm", started, "Missing pending action.", error=state["error"])
            return state

        tool_name = str(pending.get("tool", ""))
        args = pending.get("args", {}) or {}

        # ── 确认决策：优先用交互回调，其次用配置项 ───────────────────
        if self.confirm_fn is not None:
            try:
                approved = self.confirm_fn(tool_name, args)
            except (KeyboardInterrupt, EOFError):
                # Ctrl+C / 非交互环境：拒绝
                approved = False
        else:
            # 无交互回调时退回 auto_confirm_risky_writes
            approved = self.settings.auto_confirm_risky_writes

        if not approved:
            # 用户拒绝：清除 pending_action 并进入 reflecting，
            # 让 agent 尝试其他方法完成任务而不是直接就失败
            state["pending_action"] = {}
            state["status"] = "reflecting"
            state["needs_more_action"] = True
            state["error"] = (
                f"User declined {tool_name} on {args.get('path', '?')}. "
                "Find an alternative approach that does not require overwriting existing files, "
                "or finish with what's already done."
            )
            self._record(state, "human_confirm", started, "User declined.", error=state["error"])
            return state

        if not self._bump_tool_budget(state):
            self._record(state, "human_confirm", started, "Tool budget exceeded.", error=state["error"])
            return state

        payload = self._make_payload(state, str(pending.get("tool", "")), pending.get("args", {}))
        result = self.tools.execute(payload)
        call = {
            "name": payload.name,
            "args": payload.args,
            "ok": result.ok,
            "stdout": result.stdout[:2000],
            "stderr": result.stderr[:2000],
            "exit_code": result.exit_code,
        }
        state["tool_calls"].append(call)
        state["pending_action"] = {}
        self.store.add_tool_call(state["session_id"], "human_confirm", payload.name, payload.args, call)

        if payload.name == "write_file" and result.ok:
            state["write_count"] = int(state.get("write_count", 0)) + 1
            path = str(payload.args.get("path", "")).strip()
            if path:
                changed = list(state.get("changed_files", []))
                if path not in changed:
                    changed.append(path)
                state["changed_files"] = changed
            state["turn_progress"] = "modified"
            state["explore_streak"] = 0

        if result.ok:
            if payload.name == "write_file":
                state["needs_more_action"] = False
                state["status"] = "verifying"
            else:
                state["needs_more_action"] = True
                state["status"] = "acting"
            self._record(state, "human_confirm", started, f"Approved and executed: {payload.name}")
        else:
            state["status"] = "reflecting"
            state["needs_more_action"] = True
            state["error"] = (result.stderr or result.stdout or "human_confirm tool failed")[:2000]
            self._record(state, "human_confirm", started, "Approved action failed.", error=state["error"])
        return state

    def verify(self, state: AgentState) -> AgentState:
        started = time.time()
        try:
            if str(state.get("turn_progress", "none")) != "modified" and int(state.get("write_count", 0)) == 0:
                state["status"] = "reflecting"
                state["needs_more_action"] = True
                state["error"] = "no_code_change_yet: verification requires a successful write_file or explicit finish."
                self._record(state, "verify", started, "Skipped verify due to no code change.", error=state["error"])
                return state

            workspace = Path(state["workspace"]).resolve()
            verify_mode = self.settings.verify_mode if self.settings.verify_mode in {"auto", "required"} else "auto"
            state["verification_mode"] = verify_mode
            has_tests = self._has_test_entry(workspace)

            if verify_mode == "auto" and not has_tests:
                if self.settings.allow_finish_without_tests and int(state.get("write_count", 0)) > 0:
                    state["verification_note"] = "No test entry found; accepted completion with static self-check only."
                    state["needs_more_action"] = False
                    state["status"] = "finished"
                    self._record(state, "verify", started, "No tests found; accepted completion in auto mode.")
                    return state

                if int(state.get("write_count", 0)) == 0:
                    state["status"] = "reflecting"
                    state["needs_more_action"] = True
                    state["error"] = "no_code_change_yet: no file changes detected; cannot verify."
                    self._record(state, "verify", started, "No code changes to verify.", error=state["error"])
                    return state

                state["status"] = "reflecting"
                state["needs_more_action"] = True
                state["error"] = "no_tests_available: completion without tests is disabled."
                self._record(state, "verify", started, "No tests found and verification is required.", error=state["error"])
                return state

            if not self._bump_tool_budget(state):
                self._record(state, "verify", started, "Tool budget exceeded.", error=state["error"])
                return state

            payload = self._make_payload(state, "run_tests", {})
            result = self.tools.execute(payload)
            call = {
                "name": "run_tests",
                "args": {},
                "ok": result.ok,
                "stdout": result.stdout[:2000],
                "stderr": result.stderr[:2000],
                "exit_code": result.exit_code,
            }
            state["tool_calls"].append(call)
            state["last_tool_output"] = (result.stdout or result.stderr)[:2000]
            self.store.add_tool_call(state["session_id"], "verify", "run_tests", {}, call)

            if result.ok:
                state["verification_note"] = "Tests passed."
                state["needs_more_action"] = False
                state["status"] = "finished"
                self._record(state, "verify", started, "Tests passed.")
                return state

            output = (result.stderr or result.stdout or "").lower()
            if (
                verify_mode == "auto"
                and self.settings.allow_finish_without_tests
                and result.exit_code == 5
                and "no tests collected" in output
            ):
                state["verification_note"] = "No tests collected (pytest exit code 5); accepted completion in auto mode."
                state["needs_more_action"] = False
                state["status"] = "finished"
                self._record(state, "verify", started, "No tests collected; accepted completion.")
                return state

            state["status"] = "reflecting"
            state["needs_more_action"] = True
            state["error"] = (result.stderr or result.stdout or "tests failed")[:2000]
            self._record(state, "verify", started, "Tests failed.", error=state["error"])
            return state
        except Exception as exc:
            state["status"] = "reflecting"
            state["needs_more_action"] = True
            state["error"] = f"verify failed: {exc}"
            self._record(state, "verify", started, "Verify crashed.", error=state["error"])
            return state

    def reflect(self, state: AgentState) -> AgentState:
        started = time.time()
        if not self._track_failure(state, state.get("error", "")):
            self._record(state, "reflect", started, "Repeated failure short-circuit.", error=state["error"])
            return state

        retry_attempts = int(state.get("retry_attempts", 0)) + 1
        state["retry_attempts"] = retry_attempts
        if retry_attempts >= self.settings.max_retry_steps:
            state["status"] = "failed"
            state["error"] = f"Reached MAX_RETRY_STEPS={self.settings.max_retry_steps}."
            self._record(state, "reflect", started, "Max retry steps reached.", error=state["error"])
            return state

        try:
            prompt = REFLECT_PROMPT.format(
                current_request=self._current_request(state),
                plan=state.get("plan", ""),
                conversation_summary=self._conversation_summary(state),
                recent_turns=self._recent_turns_text(state),
                failure=state.get("error", ""),
            )
            if self.stream_fn:
                self.stream_fn("\n\U0001f504 Reflecting...\n")
            reflection = self._stream_or_complete_text(SYSTEM_PROMPT, prompt, temperature=0.4)
            if self.stream_fn:
                self.stream_fn("\n")
            self._accumulate_usage(state, "reflect")
            state["last_reflection"] = reflection
            state["status"] = "acting"
            state["needs_more_action"] = True
            self._record(state, "reflect", started, "Reflected and retrying.")
            return state
        except Exception as exc:
            state["status"] = "failed"
            state["error"] = f"reflection failed: {exc}"
            self._record(state, "reflect", started, "Reflection failed.", error=state["error"])
            return state

    def finish(self, state: AgentState) -> AgentState:
        started = time.time()
        try:
            summary_prompt = SUMMARY_PROMPT.format(
                current_request=self._current_request(state),
                plan=state.get("plan", ""),
                conversation_summary=self._conversation_summary(state),
                recent_turns=self._recent_turns_text(state),
                tool_calls=state.get("tool_calls", []),
                status=state.get("status", ""),
                verification_note=state.get("verification_note", ""),
                error=state.get("error", ""),
            )
            if self.stream_fn:
                self.stream_fn("\n\U0001f4dd Summary...\n")
            state["summary"] = self._stream_or_complete_text(SYSTEM_PROMPT, summary_prompt)
            if self.stream_fn:
                self.stream_fn("\n")
            self._accumulate_usage(state, "finish")
        except Exception:
            state["summary"] = (
                f"status={state.get('status')} retry_attempts={state.get('retry_attempts')} "
                f"tool_calls={state.get('tool_call_count')} verification_note={state.get('verification_note', '')}"
            )
        self._record(state, "finish", started, "Run finished.")
        return state
