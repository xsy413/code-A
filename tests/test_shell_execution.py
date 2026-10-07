from __future__ import annotations

import ctypes
import os
import shlex
import sys
from pathlib import Path

import pytest

from app.permissions import PermissionEngine
from app.sandbox import SandboxPolicy
from app.tools import ToolExecutor, ToolInput
from app.tools.shell import OUTPUT_LIMIT
from tests import make_workspace


def executor():
    workspace = make_workspace()
    e = PermissionEngine(workspace, workspace / ".user/permissions.json")
    return ToolExecutor(SandboxPolicy(workspace), permissions=e)


def python_command(shell, code):
    quote = (lambda s: "'" + s.replace("'", "''") + "'") if shell == "powershell" else shlex.quote
    return ("& " if shell == "powershell" else "") + quote(sys.executable) + " -c " + quote(code)


@pytest.mark.parametrize("shell", ["bash", "powershell"])
def test_real_shell_foreground_output_and_exit_code(shell):
    t = executor()
    if shell not in t.permissions.shells:
        pytest.skip("Backend absent")
    result = t.execute(ToolInput(shell, {"command": python_command(shell, "print('hello'); raise SystemExit(7)")}, str(t.policy.workspace)), approval="approve_once")
    assert result.execution_status == "executed"
    assert result.stdout.strip() == "hello"
    assert result.exit_code == 7
    assert not result.ok


@pytest.mark.parametrize("shell", ["bash", "powershell"])
def test_real_shell_changes_preserved_after_failure(shell):
    t = executor()
    if shell not in t.permissions.shells:
        pytest.skip("Backend absent")
    (t.policy.workspace / "old.txt").write_text("original")
    code = "from pathlib import Path; Path('new.py').write_text('value = 1\\n'); Path('old.txt').unlink(); raise SystemExit(3)"
    result = t.execute(ToolInput(shell, {"command": python_command(shell, code)}, str(t.policy.workspace)), approval="approve_once")
    assert result.exit_code == 3
    assert result.changed_files == ["new.py", "old.txt"]
    assert result.deleted_files == ["old.txt"]
    assert result.snapshot_complete


@pytest.mark.parametrize("shell", ["bash", "powershell"])
def test_shell_environment_and_output_cap(shell, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "do-not-inherit-this-key")
    t = executor()
    if shell not in t.permissions.shells:
        pytest.skip("Backend absent")
    code = "import os; print(os.getenv('OPENAI_API_KEY', 'missing')); print('x' * 200000)"
    result = t.execute(ToolInput(shell, {"command": python_command(shell, code)}, str(t.policy.workspace)), approval="approve_once")
    assert result.ok
    assert result.stdout.startswith("missing")
    assert "do-not-inherit" not in result.stdout
    assert len(result.stdout) <= OUTPUT_LIMIT + 40
    assert "archived output omitted" in result.stdout
    assert len(result.stdout) + len(result.stderr) <= 20000
    assert len(result.archive_streams["stdout"]) > 200000


@pytest.mark.parametrize("shell", ["bash", "powershell"])
def test_timeout_cleans_up_child_process(shell):
    t = executor()
    if shell not in t.permissions.shells:
        pytest.skip("Backend absent")
    code = "import subprocess,sys,time; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); print(p.pid,flush=True); time.sleep(60)"
    result = t.execute(ToolInput(shell, {"command": python_command(shell, code), "timeout_s": 2}, str(t.policy.workspace)), approval="approve_once")
    assert result.error_kind == "timeout"
    assert result.exit_code == 124
    pid = int(result.stdout.strip())
    if os.name == "nt":
        from ctypes import wintypes
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.OpenProcess.restype = wintypes.HANDLE
        api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = api.OpenProcess(0x00100000, False, pid)
        if handle:
            try:
                assert api.WaitForSingleObject(handle, 2000) == 0
            finally:
                api.CloseHandle(handle)
    else:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


def test_powershell_nonterminating_error_is_failure():
    t = executor()
    if "powershell" not in t.permissions.shells:
        pytest.skip("Backend absent")
    result = t.execute(ToolInput("powershell", {"command": "Get-Content missing.txt"}, str(t.policy.workspace)), approval="approve_once")
    assert not result.ok
    assert result.exit_code == 1


def test_approval_recheck_does_not_execute_changed_request():
    t = executor()
    first = ToolInput("write_file", {"path": "first.py", "content": "a=1"}, str(t.policy.workspace), session_id="s1")
    permission = t.permission_for(first)
    first.args["path"] = "second.py"
    result = t.execute(first, approval="approve_once", expected_fingerprint=permission.fingerprint)
    assert result.execution_status == "needs_approval"
    assert not (t.policy.workspace / "second.py").exists()


def test_deny_prevents_entire_compound_command():
    t = executor()
    if "bash" not in t.permissions.shells:
        pytest.skip("Backend absent")
    result = t.execute(ToolInput("bash", {"command": "echo yes > marker.txt; rm -rf ."}, str(t.policy.workspace)), approval="approve_once")
    assert result.execution_status == "denied"
    assert not (t.policy.workspace / "marker.txt").exists()
