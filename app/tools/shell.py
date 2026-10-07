from __future__ import annotations

import ctypes
import codecs
from collections import deque
import hashlib
import os
import shutil
import signal
import subprocess
import threading
from pathlib import Path

from app.tools.protocol import ToolResult

OUTPUT_LIMIT = 10 * 1024 * 1024


class _Capture:
    """Drain continuously, retaining a bounded head and rolling tail with exact coordinates."""
    def __init__(self, limit: int):
        self.limit, self.size, self.total_bytes = limit, 0, 0
        self.parts = deque()
        self.head, self.head_bytes, self.head_done = [], 0, False
        self.line, self.column, self.truncated = 1, 1, False
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")

    def feed(self, raw: bytes, *, final=False):
        self.total_bytes += len(raw)
        text = self.decoder.decode(raw, final=final)
        if not text:
            return
        encoded = text.encode("utf-8")
        if not self.head_done:
            room = max(0, self.limit // 4 - self.head_bytes)
            piece = encoded[:room].decode("utf-8", errors="ignore")
            self.head.append(piece)
            self.head_bytes += len(piece.encode("utf-8"))
            self.head_done = len(encoded) >= room
        self.parts.append(text)
        self.size += len(encoded)
        if self.size > self.limit:
            self.truncated = True
        target = self.limit * 3 // 4 if self.truncated else self.limit
        while self.size > target:
            first = self.parts.popleft()
            encoded_first = first.encode("utf-8")
            drop = min(len(encoded_first), self.size - target)
            keep = encoded_first[drop:].decode("utf-8", errors="ignore")
            removed = first[:-len(keep)] if keep else first
            self.size -= len(removed.encode("utf-8"))
            if keep:
                self.parts.appendleft(keep)
            count = removed.count("\n")
            if count:
                self.line += count
                self.column = len(removed.rsplit("\n", 1)[-1]) + 1
            else:
                self.column += len(removed)

    def finish(self):
        tail = "".join(self.parts)
        if not self.truncated:
            return tail, []
        head = "".join(self.head)
        return head + "\n[capture gap is unrecoverable]\n" + tail, [
            {"start_line": 1, "start_column": 1, "body": head},
            {"start_line": self.line, "start_column": self.column, "body": tail}]
ENV_NAMES = {
    "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "TMPDIR",
    "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA",
    "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMDATA", "LANG", "LC_ALL", "TERM",
    "VIRTUAL_ENV", "USER", "USERNAME", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE",
}


def shell_environment() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k.upper() in ENV_NAMES}
    env.update({"GIT_PAGER": "cat", "PAGER": "cat", "GIT_OPTIONAL_LOCKS": "0", "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "core.fsmonitor", "GIT_CONFIG_VALUE_0": "false"})
    # Do not search the workspace for commands, modules or interpreter startup files.
    env["PATH"] = os.pathsep.join(p for p in env.get("PATH", "").split(os.pathsep) if p and Path(p).is_absolute())
    return env


def discover_shells() -> dict[str, str]:
    result: dict[str, str] = {}
    ps = shutil.which("pwsh") or shutil.which("powershell")
    if ps:
        result["powershell"] = str(Path(ps).resolve())
    bash = shutil.which("bash")
    if os.name == "nt":
        git = shutil.which("git")
        candidates = [Path(git).parent.parent / "bin" / "bash.exe"] if git else []
        candidates += [Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git/bin/bash.exe"]
        # Windows' WSL launcher is not a native Bash backend.
        bash = next((str(p.resolve()) for p in candidates if p.is_file()), None)
    if bash:
        result["bash"] = str(Path(bash).resolve())
    return result


class _WindowsJob:
    def __init__(self) -> None:
        from ctypes import wintypes

        class Basic(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]

        class IO(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount", "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class Extended(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", IO), ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        self.api.CreateJobObjectW.restype = wintypes.HANDLE
        self.api.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        self.api.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.api.CloseHandle.argtypes = [wintypes.HANDLE]
        self.handle = self.api.CreateJobObjectW(None, None)
        info = Extended()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.handle or not self.api.SetInformationJobObject(self.handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            self.close()
            raise OSError("Unable to establish child-process cleanup job")

    def attach(self, process: subprocess.Popen) -> None:
        if not self.api.AssignProcessToJobObject(self.handle, int(process._handle)):
            raise OSError("Unable to attach shell to cleanup job")
        from ctypes import wintypes
        resume = ctypes.WinDLL("ntdll").NtResumeProcess
        resume.argtypes = [wintypes.HANDLE]
        if resume(int(process._handle)) != 0:
            raise OSError("Unable to resume shell")

    def close(self) -> None:
        if getattr(self, "handle", None):
            self.api.CloseHandle(self.handle)
            self.handle = None


def run_shell(executable: str, shell: str, command: str, cwd: Path, timeout_s: int) -> ToolResult:
    if shell == "powershell":
        command = ("$PSModuleAutoLoadingPreference='None'; $OutputEncoding=[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false); "
                   "Import-Module ([System.IO.Path]::Combine($PSHOME,'Modules','Microsoft.PowerShell.Management','Microsoft.PowerShell.Management.psd1')); "
                   "Import-Module ([System.IO.Path]::Combine($PSHOME,'Modules','Microsoft.PowerShell.Utility','Microsoft.PowerShell.Utility.psd1')); "
                   + command + "\n$__agent_ok=$?; $__agent_exit=$LASTEXITCODE; if ($__agent_exit) { exit $__agent_exit }; if (-not $__agent_ok) { exit 1 }")
    argv = ([executable, "--noprofile", "--norc", "-c", command] if shell == "bash" else
            [executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command])
    job = _WindowsJob() if os.name == "nt" else None
    process = None
    captures = [_Capture(OUTPUT_LIMIT), _Capture(OUTPUT_LIMIT)]

    def drain(pipe, index):
        while chunk := pipe.read(8192):
            captures[index].feed(chunk)
        captures[index].feed(b"", final=True)
        pipe.close()

    try:
        process = subprocess.Popen(argv, cwd=cwd, env=shell_environment(), stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   creationflags=(0x00000004 | 0x08000000) if os.name == "nt" else 0,
                                   start_new_session=os.name != "nt")
        if job:
            job.attach(process)
        readers = [threading.Thread(target=drain, args=(pipe, i), daemon=True) for i, pipe in enumerate((process.stdout, process.stderr))]
        for reader in readers:
            reader.start()
        kind = ""
        try:
            code = process.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            kind, code = "timeout", 124
        except KeyboardInterrupt:
            kind, code = "cancelled", 130
        finally:
            if job:
                job.close()
            elif process:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.wait()
            for reader in readers:
                reader.join(timeout=5)
        output, segments = [], {}
        for index, stream in enumerate(("stdout", "stderr")):
            body, parts = captures[index].finish()
            output.append(body)
            if parts:
                segments[stream] = parts
        if kind:
            output[1] += f"\nShell {kind}; child processes cleaned up."
            if segments.get("stderr"):
                segments["stderr"][-1]["body"] += f"\nShell {kind}; child processes cleaned up."
        return ToolResult(ok=code == 0, stdout=output[0], stderr=output[1], exit_code=code,
                          execution_status="executed", error_kind=kind,
                          output_meta={"archive_complete": not any(c.truncated for c in captures), "capture_segments": segments,
                                       "total_bytes": {s: c.total_bytes for s, c in zip(("stdout", "stderr"), captures)}})
    finally:
        if job:
            job.close()
        if process and process.poll() is None:
            process.kill()
            process.wait()


SKIP_DIRS = {".git", ".agent", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "dist", "build", ".ut_tmp"}


def snapshot(workspace: Path, readable) -> tuple[dict[str, str], bool]:
    files: dict[str, str] = {}
    complete = True
    def scan_error(_error):
        nonlocal complete
        complete = False
    for root, dirs, names in os.walk(workspace, followlinks=False, onerror=scan_error):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not (Path(root) / d).is_symlink() and readable(Path(root) / d)]
        for name in names:
            path = Path(root) / name
            if not readable(path):
                continue
            if len(files) >= 10000:
                return files, False
            try:
                if path.stat().st_size > 8 * 1024 * 1024:
                    complete = False
                    continue
                files[str(path.relative_to(workspace))] = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError:
                complete = False
    return files, complete
