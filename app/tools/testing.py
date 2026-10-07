from __future__ import annotations

from pathlib import Path

from app.permissions.parser import Analysis


def describe_test(analysis: Analysis, command: str, cwd: str, executables: list[str]) -> dict:
    """Describe literal test entries without executing or importing project code."""
    entries = []
    for parsed in analysis.commands:
        if not parsed.argv:
            continue
        name = Path(parsed.argv[0].replace("\\", "/")).name.lower().removesuffix(".exe")
        args = parsed.argv[1:]
        lower = [arg.lower() for arg in args]
        runner = ""
        opaque = False
        if name == "pytest":
            runner = "pytest"
        elif name in {"python", "python3"} and lower[:2] == ["-m", "pytest"]:
            runner, args = "pytest", args[2:]
        elif name in {"go", "cargo", "dotnet", "mvn", "gradle"} and lower[:1] == ["test"]:
            runner, args = name, args[1:]
        elif name in {"npm", "pnpm", "yarn"} and (lower[:1] == ["test"] or lower[:1] == ["run"]):
            runner, opaque = "script_entry", True
        elif name.endswith((".py", ".sh", ".ps1", ".js", ".mjs")) or (
            name in {"python", "python3", "node", "bash", "sh", "powershell", "pwsh"}
            and any(arg.lower().endswith((".py", ".sh", ".ps1", ".js", ".mjs")) for arg in args)
        ):
            runner, opaque = "script_entry", True
        if not runner:
            continue
        lower = [arg.lower() for arg in args]
        probe_flags = {"--help", "-h", "--version", "--collect-only", "--co", "--list", "--list-tests", "-list", "--dry-run"}
        if any(arg.split("=", 1)[0] in probe_flags for arg in lower):
            continue
        if runner in {"mvn", "gradle"} and any(arg.startswith(("-dskiptests", "-dmaven.test.skip")) or arg in {"-m", "-x"} for arg in lower):
            opaque = True
        entries.append({"runner": runner, "args": args, "opaque": opaque or parsed.dynamic})
    if not entries:
        return {}
    certain = len(entries) == len(analysis.commands) == 1 and not (
        analysis.uncertain or analysis.test_uncertain or analysis.redirects or analysis.background or entries[0]["opaque"]
    )
    entry = entries[0]
    scope = {"kind": "unknown", "targets": [], "selectors": {}}
    if certain and entry["runner"] == "pytest":
        scope = _pytest_scope(entry["args"])
    elif certain and entry["runner"] == "go":
        scope = _go_scope(entry["args"])
    return {"runner": entry["runner"], "direct": certain, "command": command, "cwd": cwd,
            "executables": executables, "scope": scope,
            "scope_note": "Requested selection only; actual coverage is not established."}


def _pytest_scope(args: list[str]) -> dict:
    targets, selectors = [], {}
    value_flags = {"-k", "-m", "-c", "-o", "--override-ini", "--rootdir", "--confcutdir", "--maxfail", "--tb",
                   "--ignore", "--ignore-glob", "--deselect", "--basetemp", "--junitxml", "--junit-xml", "--log-file"}
    switches = {"-q", "-qq", "-v", "-vv", "-x", "-s", "--lf", "--last-failed", "--ff", "--failed-first",
                "--disable-warnings", "--strict-markers", "--strict-config", "--no-header", "--no-summary"}
    unknown = False
    i = 0
    while i < len(args):
        arg = args[i]
        flag, sep, value = arg.partition("=")
        if flag in value_flags:
            if not sep:
                i += 1
                if i >= len(args):
                    unknown = True
                    break
                value = args[i]
            selectors.setdefault(flag, []).append(value)
        elif arg == "--":
            targets.extend(args[i + 1:])
            break
        elif arg.startswith("-"):
            if arg not in switches:
                unknown = True
            selectors.setdefault(arg, [])
        else:
            targets.append(arg)
        i += 1
    unknown |= any(any(char in target for char in "*?[]$") for target in targets)
    return {"kind": "unknown" if unknown else "targets" if targets else "default_selection",
            "targets": targets, "selectors": selectors}


def _go_scope(args: list[str]) -> dict:
    targets, selectors = [], {}
    i = 0
    unknown = False
    value_flags = {"-run", "-count", "-timeout", "-parallel", "-tags", "-cpu", "-shuffle"}
    while i < len(args):
        arg = args[i]
        flag, sep, value = arg.partition("=")
        if flag in value_flags:
            if not sep:
                i += 1
                if i == len(args):
                    unknown = True
                    break
                value = args[i]
            selectors[flag] = value
        elif arg.startswith("-"):
            unknown |= arg not in {"-v", "-race", "-short", "-cover"}
        else:
            targets.append(arg)
        i += 1
    return {"kind": "unknown" if unknown else "targets" if targets else "default_selection",
            "targets": targets, "selectors": selectors}


def test_outcome(info: dict, result: dict) -> str:
    if result.get("execution_status") in {"denied", "rejected", "not_executed", "needs_approval"}:
        return "not_run"
    code = result.get("exit_code")
    if (result.get("execution_status") == "indeterminate" or result.get("error_kind") in {"timeout", "cancelled"}
            or isinstance(code, int) and code < 0):
        return "interrupted"
    if info.get("direct") and info.get("runner") == "pytest" and result.get("exit_code") == 5:
        return "no_tests"
    if code is None or "ok" not in result:
        return "unknown"
    if not result.get("ok"):
        return "failed"
    return "passed" if info.get("direct") else "unknown"
