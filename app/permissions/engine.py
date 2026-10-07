from __future__ import annotations

import fnmatch
import copy
import glob
import hashlib
import json
import os
import re
import shutil
import shlex
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from app.permissions.parser import Analysis, parse_bash, parse_powershell
from app.tools.shell import discover_shells, shell_environment

ApprovalAnswer = Literal["approve_once", "approve_session", "reject", "unavailable"]

@dataclass(frozen=True)
class Rule:
    id: str
    decision: str
    reason: str
    reusable: bool = False


@dataclass
class PermissionDecision:
    decision: str
    rule_id: str
    reason: str
    effects: list[str] = field(default_factory=list)
    reusable: bool = False
    fingerprint: str = ""
    policy_digest: str = ""
    is_test: bool = False
    unsupported: bool = False
    grant_key: str = ""
    executables: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


ALLOW_BASH = ["pwd", "ls", "cat", "head/tail", "rg/grep", "find", "wc", "stat/du", "diff/cmp", "sort/uniq/cut/tr", "sha256sum/sha512sum", "echo/printf", "date/whoami", "command/type", "cd"]
ALLOW_PS = ["get-location", "get-childitem", "get-item/test-path", "get-content", "select-string", "get-filehash", "get-date", "get-command", "get-process", "get-service", "set-location", "select-object/measure-object", "sort-object/group-object/compare-object", "format-table/format-list/out-string", "write-output/write-host"]
ALLOW_GIT = ["status", "diff", "log", "show", "rev-parse/ls-files", "branch/tag", "ls-tree/describe", "versions"]
ASK_SESSION = ["Python tests", "Node tests", "other tests", "build", "lint/type check", "format/fix", "development server", "reviewed script", "structured ordinary file edits"]
ASK_ONCE = ["inline code", "nested shell/dynamic execution", "dependency install", "file write", "file delete", "build cleanup", "Git history/index write", "branch/worktree switch", "discard/history rewrite", "Git network", "HTTP", "remote execution/transfer", "project configuration", "archive", "process/background", "container", "publish/deploy", "external queries", "outside workspace", "network share", "environment changes", "query with execution/write flags", "system diagnostics", "unresolved command"]
DENY = ["root deletion", "disk destruction", "system file modification", "credential read/export", "secret environment output", "credential upload", "download and execute", "permission self-elevation", "agent audit/state modification", "automatic execution entry write", "privilege escalation", "security disabling", "persistent system task/service", "privileged container/host mount", "global process kill/shutdown", "encoded execution", "system account/permission change", "production bulk destruction"]


def builtin_rules() -> dict[str, Rule]:
    rules: dict[str, Rule] = {}
    for prefix, descriptions, decision, reusable in (
        ("A-B", ALLOW_BASH, "allow", False), ("A-P", ALLOW_PS, "allow", False),
        ("A-G", ALLOW_GIT, "allow", False), ("Q-S", ASK_SESSION, "ask", True),
        ("Q-O", ASK_ONCE, "ask", False), ("D-", DENY, "deny", False),
    ):
        for index, reason in enumerate(descriptions, 1):
            rule_id = f"{prefix}{index:02d}"
            rules[rule_id] = Rule(rule_id, decision, reason, reusable)
    return rules


ALIASES = {"gci": "get-childitem", "ls": "get-childitem", "dir": "get-childitem", "gc": "get-content", "cat": "get-content", "type": "get-content", "rm": "remove-item", "del": "remove-item", "ri": "remove-item", "cd": "set-location", "pwd": "get-location", "echo": "write-output", "iex": "invoke-expression", "iwr": "invoke-webrequest", "irm": "invoke-restmethod", "%": "foreach-object", "?": "where-object"}
CONFIG_NAMES = {"pyproject.toml", "package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "uv.lock", "poetry.lock", "requirements.txt", "pytest.ini", "tox.ini", "setup.cfg", "setup.py", "conftest.py", "cargo.toml", "cargo.lock", "go.mod", "go.sum", "makefile", "agents.md", "tsconfig.json", ".npmrc"}
SECRET_NAME = re.compile(r"(?i)(?:api[_-]?key|token|password|secret|credential|private[_-]?key)")


class PermissionEngine:
    def __init__(self, workspace: Path, config_path: Path | None = None, shells: dict[str, str] | None = None) -> None:
        self.workspace = workspace.resolve()
        self.config_path = (config_path or Path.home() / ".coding-agent/permissions.json").resolve()
        self.shells = discover_shells() if shells is None else shells
        self.grants: set[tuple[str, str]] = set()
        self.protected_context_paths: set[Path] = set()
        self.rules = builtin_rules()
        self.custom: list[dict] = []
        self.digest = ""
        self.config_error = ""
        self._program_cache: dict[tuple, str] = {}
        self._ast_cache: dict[tuple, Any] = {}
        self._registered_programs: dict[str, str] = {}
        self._registered_identities: dict[str, tuple[int, int]] = {}
        self._shell_identities = {k: (Path(p).stat().st_size, Path(p).stat().st_mtime_ns) for k, p in self.shells.items()}
        for shell in self.shells:
            names = ({n for row in ALLOW_BASH for n in row.split("/")} if shell == "bash" else set()) | {"git", "python", "python3", "node", "rg"}
            for name in names:
                program = self._program(name, shell, self.workspace)
                if program and not self.inside(Path(program)):
                    self._registered_programs[f"{shell}:{name}"] = program
                    stat = Path(program).stat()
                    self._registered_identities[f"{shell}:{name}"] = (stat.st_size, stat.st_mtime_ns)
        self._secrets = [v for k, v in os.environ.items() if SECRET_NAME.search(k) and len(v) >= 6
                         and not k.startswith("CONTEXT_") and k not in {"COMPACT_OUTPUT_TOKENS", "COMPACT_SUMMARY_TOKENS", "COMPACT_TOKENIZER"}]
        self.reload()

    def redact(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {k: self.redact(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.redact(v) for v in value]
        if not isinstance(value, str):
            return value
        value = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]|[\x00-\x08\x0b\x0c\x0e-\x1f]", "", value)
        for secret in self._secrets:
            value = value.replace(secret, "[REDACTED]")
        value = re.sub(r"(?i)((?:api[_-]?key|token|password|secret)\s*[=:]\s*)[^\s,;]+", r"\1[REDACTED]", value)
        return value

    def register_secret(self, value: str) -> None:
        if value and len(value) >= 6 and value not in self._secrets:
            self._secrets.append(value)

    def reload(self) -> None:
        self.rules, self.custom, self.config_error = builtin_rules(), [], ""
        try:
            exists = self.config_path.exists()
            raw = self.config_path.read_bytes() if exists else b""
            self.digest = hashlib.sha256(raw).hexdigest()
            if not exists:
                return
            def unique_pairs(pairs):
                obj = {}
                for key, value in pairs:
                    if key in obj:
                        raise ValueError("Duplicate JSON key")
                    obj[key] = value
                return obj
            data = json.loads(raw, object_pairs_hook=unique_pairs)
            if not isinstance(data, dict) or set(data) - {"version", "rules", "overrides"} or type(data.get("version")) is not int or data.get("version") != 1:
                raise ValueError("Expected permissions JSON version 1")
            overrides = data.get("overrides", [])
            custom = data.get("rules", [])
            if not isinstance(overrides, list) or not isinstance(custom, list):
                raise ValueError("rules and overrides must be arrays")
            ids: set[str] = set()
            for item in [*overrides, *custom]:
                if not isinstance(item, dict) or item.get("decision") not in {"allow", "ask", "deny"}:
                    raise ValueError("Invalid permission rule")
                if set(item) - {"id", "decision", "reason", "tool", "argv_prefix", "path", "workspace"}:
                    raise ValueError("Unknown rule field")
                if not isinstance(item.get("id"), str) or not item["id"] or item["id"] in ids:
                    raise ValueError("Rule IDs must be unique strings")
                ids.add(item["id"])
                if any(k in item and not isinstance(item[k], str) for k in ("reason", "tool", "path", "workspace")):
                    raise ValueError("Invalid string rule field")
                if "argv_prefix" in item and (not isinstance(item["argv_prefix"], list) or not item["argv_prefix"] or not all(isinstance(a, str) for a in item["argv_prefix"])):
                    raise ValueError("argv_prefix must be a nonempty string array")
            for item in overrides:
                if set(item) - {"id", "decision", "reason"} or item["id"] not in self.rules:
                    raise ValueError("Overrides require a known built-in rule ID")
                old = self.rules[item["id"]]
                if old.id in {"D-08", "D-09"} and item["decision"] != "deny":
                    raise ValueError("Agent permission/audit protections cannot be relaxed")
                self.rules[old.id] = Rule(old.id, item["decision"], item.get("reason", old.reason), old.reusable)
            for item in custom:
                if item["id"] in self.rules or not (item.get("argv_prefix") or item.get("path")):
                    raise ValueError("Custom rules need a selector and a new ID")
            self.custom = custom
        except (OSError, ValueError, TypeError) as exc:
            self.digest = "invalid"
            self.config_error = f"Invalid permission configuration: {type(exc).__name__}"

    def inside(self, path: Path) -> bool:
        return path.resolve().is_relative_to(self.workspace)

    def path(self, value: str, cwd: Path) -> Path:
        if value.startswith(("\\\\", "//")):
            return Path(value)
        # Git Bash's drive notation is not a native Windows relative pathname.
        if os.name == "nt" and re.match(r"^/[a-zA-Z]/", value):
            value = value[1].upper() + ":/" + value[3:]
        if os.name == "nt" and re.match(r"^/mnt/[a-zA-Z]/", value):
            raise ValueError("WSL paths are not supported by the native shell backend")
        return (cwd / Path(value).expanduser()).resolve()

    def path_rule(self, path: Path, write: bool = False, delete: bool = False) -> str | None:
        if str(path).startswith(("\\\\", "//")):
            return "Q-O20"
        path = path.resolve()
        parts = {p.lower() for p in path.parts}
        name = path.name.lower()
        if ".agent" in parts:
            return "D-09"
        if write and path in self.protected_context_paths:
            return "D-08"
        if write and path == self.workspace / ".env" and path.is_file():
            try:
                if re.search(r"(?m)^\s*(?:CONTEXT_|COMPACT_)\w+\s*=", path.read_text(encoding="utf-8")):
                    return "D-08"
            except (OSError, UnicodeError):
                return "D-08"
        if write and (path == self.config_path or path.is_relative_to(self.config_path.parent)):
            return "D-08"
        if write and (".agent" in parts or path.is_relative_to(self.workspace / ".git")):
            return "D-10" if "hooks" in parts else "D-09"
        if write and (name in {".bashrc", ".bash_profile", ".profile"} or "profile.ps1" in name or "startup" in parts):
            return "D-10"
        roots = {self.workspace, Path.home().resolve(), Path(path.anchor)}
        if write and (path in roots or name in {"system32", "windows", "users"}):
            return "D-01" if delete else "D-03"
        system = Path(os.environ.get("SYSTEMROOT", "C:/Windows")).resolve() if os.name == "nt" else Path("/etc")
        if write and (path.is_relative_to(system) or os.name != "nt" and any(path.is_relative_to(Path(p)) for p in ("/boot", "/usr", "/bin", "/sbin"))):
            return "D-03"
        template = name in {".env.example", ".env.sample", ".env.template"}
        credential = (name == ".env" or name.startswith(".env.") and not template or
                      name in {"id_rsa", "id_ed25519", "id_ecdsa", "credentials", "credentials.json", "token.json", "login data", "cookies"} or
                      "secrets" in parts or ".aws" in parts and name == "credentials" or name.endswith((".key", ".p12", ".pfx")))
        if not write and credential:
            return "D-04"
        if not write and name.endswith(".pem") and path.is_file():
            try:
                with path.open("rb") as source:
                    if b"PRIVATE KEY" in source.read(4096):
                        return "D-04"
            except OSError:
                return "Q-O24"
        if str(path).startswith("\\\\"):
            return "Q-O20"
        if not self.inside(path):
            return "Q-O19"
        if write:
            if credential or name in CONFIG_NAMES or "lock" in name or name.startswith(("eslint", ".eslintrc", "vite.config", "webpack.config")):
                return "Q-O13"
            return "Q-O05" if delete else "Q-S09"
        return None

    def readable(self, path: Path) -> bool:
        rule = self.path_rule(path)
        return (self.inside(path) and ".agent" not in {p.lower() for p in path.parts}
                and (not rule or self.rules[rule].decision == "allow")
                and all(h.decision == "allow" for h in self._custom_hits("", [], [path.resolve()])))

    def _rule(self, rule_id: str) -> Rule:
        return self.rules[rule_id]

    def _program(self, name: str, shell: str, cwd: Path) -> str:
        if "/" in name or "\\" in name:
            path = self.path(name, cwd)
            return str(path) if path.is_file() else ""
        if os.name == "nt" and (cwd / (name if name.endswith(".exe") else name + ".exe")).is_file():
            return str((cwd / (name if name.endswith(".exe") else name + ".exe")).resolve())
        if os.name == "nt" and shell == "bash" and name not in {"git", "python", "python3", "node"}:
            bundled = Path(self.shells[shell]).parent.parent / "usr/bin" / (name.removesuffix(".exe") + ".exe")
            if bundled.is_file():
                return str(bundled.resolve())
        path_env = shell_environment().get("PATH", "")
        cache_key = (name, shell, path_env)
        if cache_key in self._program_cache:
            return self._program_cache[cache_key]
        candidate = shutil.which(name, path=path_env)
        if shell == "bash" and os.name == "nt" and not candidate:
            base = Path(self.shells[shell]).parent.parent
            candidate = next((str(p) for p in (base / "usr/bin" / (name + ".exe"), base / "bin" / (name + ".exe")) if p.is_file()), None)
        if candidate:
            path = Path(candidate).resolve()
            self._program_cache[cache_key] = str(path)
            return str(path)
        self._program_cache[cache_key] = ""
        return ""

    def execution_command(self, shell: str, command: str, cwd: Path) -> str:
        analysis = self._parse(shell, command)
        replacements: list[tuple[int, int, str]] = []
        for item in analysis.commands:
            if not item.argv or item.dynamic:
                continue
            name = Path(item.argv[0]).name.lower().removesuffix(".exe")
            canonical = item.canonical or ALIASES.get(name, name) if shell == "powershell" else name
            program = "" if shell == "powershell" and any(canonical in names.split("/") for names in ALLOW_PS) else self._program(item.argv[0], shell, cwd)
            if program:
                end = item.end
                if shell == "bash":
                    if os.name == "nt":
                        program = "/" + program[0].lower() + "/" + program[3:].replace("\\", "/")
                    replacement = shlex.quote(program)
                else:
                    replacement = ("" if item.invocation == "Ampersand" else "& ") + "'" + program.replace("'", "''") + "'"
                if name == "git":
                    replacement += " --no-pager -c core.fsmonitor=false"
                    if item.argv[1:2] and item.argv[1] in {"diff", "show"}:
                        # Disable content conversion/external diff even when inherited Git config enables it.
                        replacement += " -c diff.external= -c diff.trustExitCode=false"
                        replacement += f" {item.argv[1]} --no-ext-diff --no-textconv"
                        end = item.second_end
                replacements.append((item.start, end, replacement))
            elif shell == "powershell" and any(canonical in names.split("/") for names in ALLOW_PS):
                module = "Microsoft.PowerShell.Core" if canonical == "get-command" else "Microsoft.PowerShell.Management" if canonical in {"get-location", "get-childitem", "get-item", "test-path", "get-content", "get-process", "get-service", "set-location"} else "Microsoft.PowerShell.Utility"
                replacements.append((item.start, item.end, f"{module}\\{canonical}"))
        for start, end, replacement in sorted(replacements, reverse=True):
            command = command[:start] + replacement + command[end:]
        return command

    def _custom_hits(self, tool: str, argv: list[str], paths: list[Path]) -> list[Rule]:
        hits = []
        for rule in self.custom:
            if rule.get("workspace") and str(self.workspace) != str(Path(rule["workspace"]).expanduser().resolve()):
                continue
            if rule.get("tool") and rule["tool"] != tool:
                continue
            prefix = rule.get("argv_prefix")
            if prefix and argv[:len(prefix)] != prefix:
                continue
            pattern = rule.get("path", "").replace("\\", "/")
            if os.name == "nt":
                pattern = pattern.lower()
            if pattern and not any(fnmatch.fnmatchcase(p.as_posix().lower() if os.name == "nt" else p.as_posix(), pattern) for p in paths):
                continue
            hits.append(Rule(rule["id"], rule["decision"], rule.get("reason", "User permission rule")))
        return hits

    def _dependencies(self, argv: list[list[str]], cwd: Path, *, script_entry: bool = False) -> list[tuple[str, str]]:
        entries: list[tuple[str, str]] = []
        for root, dirs, names in os.walk(self.workspace):
            dirs[:] = [d for d in dirs if d not in {".git", ".agent", ".venv", "node_modules", "__pycache__", ".ut_tmp"}]
            for name in names:
                lower = name.lower()
                if lower in CONFIG_NAMES or lower.startswith((".eslintrc", "eslint.config", "vite.config", "webpack.config")):
                    p = Path(root) / name
                    if self.readable(p):
                        try:
                            entries.append((str(p), hashlib.sha256(p.read_bytes()).hexdigest()))
                        except OSError:
                            entries.append((str(p), "unreadable"))
        for command in argv:
            for word in command:
                if word.endswith((".sh", ".ps1", ".js", ".mjs", ".py")) and (script_entry or not Path(word).name.startswith("test")):
                    p = self.path(word, cwd)
                    if self.readable(p) and p.is_file():
                        entries.append((str(p), hashlib.sha256(p.read_bytes()).hexdigest()))
        gitdir = self.workspace / ".git"
        if gitdir.is_file():
            entries.append((str(gitdir), hashlib.sha256(gitdir.read_bytes()).hexdigest()))
            text = gitdir.read_text(encoding="utf-8").strip()
            if text.startswith("gitdir:"):
                gitdir = (self.workspace / text[len("gitdir:"):].strip()).resolve()
        git_files = [gitdir / "config", gitdir / "config.worktree", *list((gitdir / "hooks").glob("*"))]
        for path in git_files:
            if path.is_file():
                entries.append((str(path), hashlib.sha256(path.read_bytes()).hexdigest()))
        return sorted(entries)

    def _analyze(self, shell: str, command: str, depth: int = 0):
        result = self._parse(shell, command)
        if depth >= 4:
            result.uncertain = True
            return result
        for item in list(result.commands):
            if item.dynamic or not item.argv:
                continue
            name = Path(item.argv[0]).name.lower().removesuffix(".exe")
            args = item.argv[1:]
            nested_shell, code = "", ""
            if name in {"bash", "sh"} and "-c" in args and args.index("-c") + 1 < len(args):
                nested_shell, code = "bash", args[args.index("-c") + 1]
            elif name in {"pwsh", "powershell"} and "powershell" in self.shells:
                flag = next((i for i, a in enumerate(args) if a.lower() in {"-command", "-c"}), None)
                if flag is not None and flag + 1 < len(args):
                    nested_shell, code = "powershell", args[flag + 1]
            elif name in {"eval", "iex", "invoke-expression"} and args:
                nested_shell, code = shell, args[0]
            if nested_shell:
                nested = self._analyze(nested_shell, code, depth + 1)
                result.commands.extend(nested.commands)
                result.redirects.extend(nested.redirects)
                result.uncertain |= nested.uncertain
                result.background |= nested.background
        return result

    def _parse(self, shell: str, command: str):
        stat = Path(self.shells[shell]).stat()
        if shell == "powershell" and (self.inside(Path(self.shells[shell])) or self._shell_identities[shell] != (stat.st_size, stat.st_mtime_ns)):
            return Analysis(uncertain=True, errors="Shell backend is not trusted at its current identity")
        key = (shell, command, stat.st_size, stat.st_mtime_ns)
        if key not in self._ast_cache:
            if len(self._ast_cache) >= 128:
                self._ast_cache.clear()
            analysis = parse_bash(command) if shell == "bash" else parse_powershell(command, self.shells[shell], shell_environment())
            for item in analysis.commands:
                item.shell = shell
            self._ast_cache[key] = analysis
        return copy.deepcopy(self._ast_cache[key])

    def evaluate(self, tool: str, args: dict, cwd: Path, session_id: str = "") -> PermissionDecision:
        self.reload()
        if self.config_error:
            return PermissionDecision("deny", "config_error", self.config_error, policy_digest=self.digest)
        requested_cwd = str(cwd)
        network_cwd = requested_cwd.startswith(("\\\\", "//"))
        cwd = self.workspace if network_cwd else cwd.resolve()
        hits: list[Rule] = []
        paths: list[Path] = []
        commands: list[list[str]] = []
        executables: list[tuple[str, int, int]] = []
        is_test, unsupported = False, False
        if network_cwd:
            hits.append(self._rule("Q-O20"))
        cwd_rule = self.path_rule(cwd)
        if cwd_rule:
            hits.append(self._rule(cwd_rule))
        if tool not in {"bash", "powershell"}:
            if tool in {"read_file", "write_file", "patch_file", "delete_file"}:
                target = self.path(str(args.get("path", "")), cwd)
                paths.append(target)
                rule = self.path_rule(target, tool != "read_file", tool == "delete_file")
                if rule:
                    hits.append(self._rule(rule))
                if tool in {"write_file", "patch_file"} and target == self.workspace / ".env":
                    proposed = str(args.get("content", "")) + str(args.get("new_str", ""))
                    if re.search(r"(?m)^\s*(?:CONTEXT_|COMPACT_)\w+\s*=", proposed):
                        hits.append(self._rule("D-08"))
                # patch_file reads the old content, so it may not bypass secret read protection.
                if tool == "patch_file" and self.path_rule(target) == "D-04":
                    hits.append(self._rule("D-04"))
            elif tool == "read_tool_result":
                if args.get("_source_path"):
                    target = self.path(args["_source_path"], cwd)
                    paths.append(target)
                    rule = self.path_rule(target)
                    if rule:
                        hits.append(self._rule(rule))
            elif tool in {"inspect_workspace", "list_files", "search_text"}:
                pattern = str(args.get("include_glob", args.get("pattern", "*"))) if tool != "search_text" else str(args.get("include_glob", "*"))
                if ".." in Path(pattern).parts or Path(pattern).is_absolute():
                    hits.append(self._rule("Q-O19"))
            else:
                hits.append(self._rule("Q-O24"))
            if not hits:
                hits.append(Rule("A-F01", "allow", "Ordinary workspace file read"))
            hits += self._custom_hits(tool, [], paths)
        else:
            if tool not in self.shells:
                return PermissionDecision("deny", "backend_unavailable", f"{tool} backend is not installed")
            command = str(args.get("command", ""))
            analysis = self._analyze(tool, command)
            unsupported = analysis.background
            if analysis.uncertain or not analysis.commands:
                hits.append(self._rule("Q-O24"))
            # Literal secret variables remain protected even inside a dynamic expression.
            if re.search(r"(?i)(?:\$env:|\$\{?)(?:[\w]*TOKEN|[\w]*PASSWORD|[\w]*SECRET|[\w]*API_KEY)", command):
                hits.append(self._rule("D-05"))
            if re.search(r"(?i)\b(?:curl|wget|invoke-webrequest|iwr|invoke-restmethod)\b", command) and re.search(r"(?i)\|\s*(?:bash|sh|pwsh|powershell|iex|invoke-expression)\b", command):
                hits.append(self._rule("D-07"))
            if re.search(r"(?i)(?:frombase64string|base64\s+-d).*?(?:iex|invoke-expression|eval|\|\s*(?:sh|bash))", command):
                hits.append(self._rule("D-16"))
            if re.search(r"(?i)(?:frombase64string|base64\s+-d)", command) and re.search(r"(?i)\b(?:iex|invoke-expression|eval)\b", command):
                hits.append(self._rule("D-16"))
            if re.search(r"(?i)\b(?:invoke-webrequest|iwr|invoke-restmethod)\b", command) and re.search(r"(?i)\b(?:iex|invoke-expression)\b", command):
                hits.append(self._rule("D-07"))
            if ":(){:|:&};:" in re.sub(r"\s+", "", command):
                hits.append(self._rule("D-15"))
            if re.search(r"(?i)\bprod(?:uction)?\b", command) and re.search(r"(?i)\b(?:kubectl\s+delete.*--all|terraform\s+destroy|aws\s+s3\s+rm.*--recursive)\b", command):
                hits.append(self._rule("D-18"))
            if re.search(r"(?i)\b(?:drop\s+database|truncate\s+table)\b", command) and re.search(r"(?i)\bprod(?:uction)?\b", command):
                hits.append(self._rule("D-18"))
            for op, value in analysis.redirects:
                if value in {"&1", "&2", "/dev/null", "$null"}:
                    continue
                if value:
                    path = self.path(value, cwd)
                    paths.append(path)
                    rule = self.path_rule(path, ">" in op)
                    if rule:
                        hits.append(self._rule(rule))
                hits.append(self._rule("Q-O04" if ">" in op else "Q-O24"))
            evaluation_cwd = cwd
            for parsed in analysis.commands:
                argv = parsed.argv
                if not argv:
                    continue
                name = Path(argv[0]).name.lower().removesuffix(".exe")
                command_shell = parsed.shell or tool
                if command_shell == "powershell":
                    name = parsed.canonical or ALIASES.get(name, name)
                argv = [name, *argv[1:]]
                commands.append([parsed.argv[0], *argv[1:]])
                command_hits, command_paths, test = self._command(argv, command_shell, evaluation_cwd)
                paths += command_paths
                hits += command_hits + self._custom_hits(tool, argv, command_paths)
                is_test |= test
                if parsed.dynamic:
                    hits.append(self._rule("Q-O24"))
                if name in {"cd", "set-location"} and not parsed.dynamic:
                    targets = [a for a in argv[1:] if not a.startswith("-")]
                    if len(targets) == 1:
                        evaluation_cwd = self.path(targets[0], evaluation_cwd)
                    if len(analysis.commands) > 1:
                        hits.append(self._rule("Q-O24"))
                if name in {"start-process", "start-job", "start-threadjob", "nohup", "disown"}:
                    unsupported = True
                program = "" if tool == "powershell" and any(name in names.split("/") for names in ALLOW_PS) else self._program(parsed.argv[0], tool, cwd)
                if program:
                    stat = Path(program).stat()
                    executables.append((program, stat.st_size, stat.st_mtime_ns))
                    registered = self._registered_programs.get(f"{tool}:{name}")
                    identity = self._registered_identities.get(f"{tool}:{name}")
                    if self.inside(Path(program)) or any(h.id.startswith("A-") for h in command_hits) and (program != registered or identity != (stat.st_size, stat.st_mtime_ns)):
                        hits = [h for h in hits if not h.id.startswith("A-")]
                        if not any(h.decision == "ask" for h in command_hits):
                            hits.append(self._rule("Q-O24"))
                elif tool == "bash" and name not in {"pwd", "cd", "echo", "printf", "command", "type"} or tool == "powershell" and not name.startswith(("get-", "set-location", "test-path", "select-", "measure-", "sort-", "group-", "compare-", "format-", "out-string", "write-")):
                    hits.append(self._rule("Q-O24"))
            stat = Path(self.shells[tool]).stat()
            if self.inside(Path(self.shells[tool])) or self._shell_identities[tool] != (stat.st_size, stat.st_mtime_ns):
                hits.append(self._rule("Q-O24"))
            executables.append((self.shells[tool], stat.st_size, stat.st_mtime_ns))
            # Chains ending in echo/another command do not establish a test result.
            is_test = is_test and len(commands) == 1 and not analysis.uncertain and not analysis.redirects
        if not hits:
            hits.append(self._rule("Q-O24"))
        rank = {"allow": 0, "ask": 1, "deny": 2}
        selected = max(hits, key=lambda h: (rank[h.decision], h.id != "Q-O24"))
        asking = [h for h in hits if h.decision == "ask"]
        reusable = selected.decision == "ask" and bool(asking) and all(h.reusable for h in asking) and not unsupported
        scope = {"tool": tool, "cwd": str(cwd), "requested_cwd": requested_cwd, "workspace": str(self.workspace), "policy": self.digest, "programs": executables}
        scope["environment"] = hashlib.sha256(json.dumps(shell_environment(), sort_keys=True).encode()).hexdigest()
        ordinary_edit = tool in {"write_file", "patch_file"} and asking and all(h.id == "Q-S09" for h in asking)
        scope["action"] = "workspace_file_edit" if ordinary_edit else commands if commands else args
        if ordinary_edit:
            scope["tool"] = "structured_file_edit"
        if reusable and not ordinary_edit:
            scope["dependencies"] = self._dependencies(commands, cwd, script_entry=any(h.id == "Q-S08" for h in asking))
        grant_key = hashlib.sha256(json.dumps(scope, sort_keys=True, default=str).encode()).hexdigest()
        fingerprint = hashlib.sha256(json.dumps([scope, tool, args], sort_keys=True, default=str).encode()).hexdigest()
        decision = PermissionDecision(selected.decision, selected.id, selected.reason,
                                      [str(p) for p in dict.fromkeys(paths)], reusable, fingerprint, self.digest, is_test, unsupported, grant_key)
        decision.executables = list(dict.fromkeys(p[0] for p in executables))
        if decision.decision == "ask" and reusable and (session_id, grant_key) in self.grants:
            decision.decision = "allow"
            decision.reason += " (current-process session approval)"
        return decision

    def approve_session(self, session_id: str, decision: PermissionDecision) -> None:
        if decision.decision != "ask" or not decision.reusable:
            raise ValueError("This operation only supports one-time approval")
        self.grants.add((session_id, decision.grant_key))

    def _command(self, argv: list[str], shell: str, cwd: Path) -> tuple[list[Rule], list[Path], bool]:
        name, args = argv[0], argv[1:]
        low = [a.lower() for a in args]
        hits: list[Rule] = []
        paths: list[Path] = []
        test = False

        def add(rule_id):
            hits.append(rule_id if isinstance(rule_id, Rule) else self._rule(rule_id))

        write = name in {"rm", "rmdir", "remove-item", "set-content", "add-content", "out-file", "tee", "mkdir", "touch", "cp", "mv", "copy-item", "move-item", "rename-item", "new-item", "sed"} or name == "find" and "-delete" in low
        delete = name in {"rm", "rmdir", "remove-item"} or name == "find" and "-delete" in low
        for word in args:
            if word.startswith("-"):
                continue
            # Explicit filename arguments and Git revision:path targets are inspected even for opaque commands.
            pathword = word.split(":", 1)[1] if name == "git" and ":" in word and not re.match(r"^[A-Za-z]:", word) else word.lstrip("@")
            provider = re.match(r"(?i)^([a-z]{2,}):", pathword)
            if shell == "powershell" and provider:
                add("D-05" if provider[1].lower() == "env" else "Q-O23")
                continue
            if write or any(c in pathword for c in ("/", "\\", ".")) or (cwd / pathword).exists() or pathword.lower() in {"secrets", "credentials", "id_rsa", "id_ed25519"}:
                if re.match(r"^[a-z]+://", pathword, re.I):
                    continue
                try:
                    path = self.path(pathword, cwd)
                    network = str(path).startswith(("\\\\", "//"))
                    matches = [Path(p) for p in glob.glob(str(path))] if glob.has_magic(str(path)) and not network else [path]
                    for p in matches:
                        paths.append(p if str(p).startswith(("\\\\", "//")) else p.resolve())
                        rule = self.path_rule(p, write, delete)
                        if rule:
                            add(rule)
                        # Shell file writes always require their own, non-reusable approval.
                        if write and rule == "Q-S09":
                            add("Q-O05" if delete else "Q-O04")
                except ValueError:
                    add("Q-O24")
        if name in {"sudo", "su", "doas", "runas"} or name == "start-process" and any("runas" in a for a in low):
            add("D-11")
        if name in {"mkfs", "wipefs", "format", "format-volume", "clear-disk"} or name.startswith("mkfs.") or name == "diskpart" and "clean" in low or name == "dd" and any("/dev/" in a for a in args):
            add("D-02")
        if name in {"env", "printenv"} and not args or name == "get-childitem" and any(a.lower().startswith("env:") for a in args) or name == "printenv" and any(SECRET_NAME.search(a) for a in args):
            add("D-05")
        if name in {"shutdown", "reboot", "stop-computer", "restart-computer"} or name == "kill" and "-1" in args or name == "stop-process" and ("-id" not in low or "*" in args):
            add("D-15")
        if name in {"pwsh", "powershell"} and any(a in {"-e", "-en", "-ec"} or a.startswith("-enc") for a in low):
            add("D-16")
        if name in {"register-scheduledtask", "new-service"} or name == "schtasks" and "/create" in low or name == "crontab" and "-l" not in args:
            add("D-13")
        if name in {"set-mppreference", "set-netfirewallprofile", "set-executionpolicy"} or name in {"netsh", "ufw", "iptables"} and any(a in {"off", "disable", "-f"} for a in low):
            add("D-12")
        if name in {"useradd", "userdel", "usermod", "new-localuser", "remove-localuser", "set-localuser", "chmod", "chown", "icacls"} or name == "net" and low[:1] == ["user"]:
            add("D-17")
        if name in {"docker", "podman"} and ("--privileged" in low or any(a in {"/:/host", "/:/", "/var/run/docker.sock:/var/run/docker.sock"} or "docker.sock" in a or re.match(r"^[a-z]:[\\/]?:", a, re.I) for a in args)):
            add("D-14")
        if hits and any(h.decision == "deny" for h in hits):
            if name in {"curl", "wget", "scp", "invoke-webrequest", "invoke-restmethod"} and any(h.id == "D-04" for h in hits):
                add("D-06")
            return hits, paths, test
        if name in {"pytest", "python", "python3"} and (name == "pytest" or low[:2] == ["-m", "pytest"]):
            test = not any(a in {"--version", "--help", "--collect-only"} for a in low)
            add("Q-S01")
        elif name in {"npm", "pnpm", "yarn"} and (low[:1] == ["test"] or low[:2] == ["run", "test"]):
            test = True
            add("Q-S02")
        elif name in {"cargo", "go", "dotnet", "mvn", "gradle"} and low[:1] == ["test"]:
            test = True
            add("Q-S03")
        elif name in {"npm", "pnpm", "yarn", "cargo", "dotnet", "make", "go"} and "build" in low:
            add("Q-S04")
        elif name in {"ruff", "eslint", "tsc", "mypy", "black"}:
            add("Q-S06" if name == "black" or "format" in low or "--fix" in low else "Q-S05")
        elif name in {"npm", "pnpm", "yarn"} and "dev" in low or name in {"python", "python3"} and "http.server" in low:
            add("Q-S07")
        elif name in {"python", "python3", "node", "ruby", "perl"} and any(a in {"-c", "-e", "--eval"} for a in low) or name in {"add-type", "invoke-expression"}:
            add("Q-O01")
        elif name in {"bash", "sh", "pwsh", "powershell", "eval", "source", "."}:
            add("Q-O02")
        elif name in {"python", "python3", "node"} and args and args[0].endswith((".py", ".js", ".mjs")) or name.endswith((".sh", ".ps1")):
            add("Q-S08")
        elif name in {"pip", "pip3", "uv", "npm", "pnpm", "yarn", "npx", "winget", "install-module", "install-package"} and any(a in {"install", "uninstall", "update", "sync", "add", "remove", "dlx"} for a in low) or name == "npx":
            add("Q-O03")
        elif write:
            add("Q-O06" if delete and any(Path(a).name in {"dist", "build", ".pytest_cache"} for a in args) else "Q-O05" if delete else "Q-O04")
        elif name in {"curl", "wget", "invoke-webrequest", "invoke-restmethod"}:
            add("Q-O11")
        elif name in {"ssh", "scp", "rsync", "enter-pssession", "invoke-command", "new-pssession"}:
            add("Q-O12")
        elif name in {"zip", "unzip", "tar", "expand-archive", "compress-archive"}:
            add("Q-O14")
        elif name in {"kill", "stop-process", "start-process", "start-job", "start-threadjob", "nohup", "disown"}:
            add("Q-O15")
        elif name in {"docker", "podman", "wsl"}:
            add("Q-O16")
        elif name in {"terraform", "kubectl", "twine", "gh"} or name == "npm" and "publish" in low:
            add("Q-O17")
        elif name in {"aws", "az", "gcloud", "psql", "mysql"} or name in {"npm", "pip"} and any(a in {"view", "index"} for a in low):
            add("Q-O18")
        elif name in {"export", "env", "set-alias", "import-module"}:
            add("Q-O21")
        elif name in {"get-computerinfo", "systemctl", "mount"}:
            add("Q-O23")
        elif name == "git":
            self._git(args, add)
        elif args == ["--version"] and name in {"python", "python3", "node", "git", "rg"}:
            add("A-G08")
        else:
            self._read_command(name, args, shell, cwd, paths, add)
        return hits, paths, test

    def _git(self, args: list[str], add) -> None:
        if not args:
            add("Q-O24")
            return
        sub = args[0].lower()
        if args == ["--version"]:
            add("A-G08")
            return
        if sub in {"fetch", "pull", "clone", "push"}:
            add("Q-O10")
        elif sub in {"reset", "restore", "clean", "filter-repo"} or sub == "branch" and "-D" in args:
            add("Q-O09")
        elif sub in {"switch", "checkout", "worktree"} or sub in {"branch", "tag"} and "--list" not in args:
            add("Q-O08")
        elif sub in {"add", "commit", "merge", "rebase", "cherry-pick", "stash"}:
            add("Q-O07")
        elif sub == "config":
            add("Q-O13")
        elif any(a.startswith(("-c", "--output", "--ext-diff", "--textconv", "--exec", "--paginate")) for a in args):
            add("Q-O22")
        else:
            mapping = {"status": "A-G01", "diff": "A-G02", "log": "A-G03", "show": "A-G04", "rev-parse": "A-G05", "ls-files": "A-G05", "branch": "A-G06", "tag": "A-G06", "ls-tree": "A-G07", "describe": "A-G07"}
            options = {"--short", "--porcelain", "--stat", "--name-only", "--name-status", "--no-ext-diff", "--no-textconv", "--oneline", "--max-count", "-n", "--show-toplevel", "--verify", "--list", "--cached", "--staged", "--abbrev-ref", "--tags", "--always", "--"}
            if sub not in mapping or any(a.startswith("-") and a not in options and not re.fullmatch(r"-\d+", a) for a in args[1:]):
                add("Q-O24")
            elif sub == "log" and any(a in {"-p", "--patch"} for a in args):
                add("Q-O24")
            elif sub in {"diff", "show"}:
                # Broad content queries could expose secrets from current or historical trees.
                has_safe_path = "--" in args and args.index("--") < len(args) - 1 or sub == "show" and any(":" in a for a in args[1:])
                add(mapping[sub] if has_safe_path or "--stat" in args or "--name-only" in args else "Q-O24")
            else:
                add(mapping[sub])

    def _read_command(self, name, args, shell, cwd, paths, add):
        table = ALLOW_BASH if shell == "bash" else ALLOW_PS
        entry = next((i for i, names in enumerate(table, 1) if name in names.split("/")), None)
        if not entry:
            add("Q-O24")
            return
        flags = {"-a", "-l", "-la", "-al", "-n", "-q", "-i", "-r", "-v", "-c", "-h", "--", "--files", "--hidden", "-f", "-name", "-type", "-maxdepth", "-print", "-size", "--short", "--bytes", "--lines", "--words"} if shell == "bash" else {
            "-path", "-literalpath", "-recurse", "-file", "-directory", "-force", "-name", "-filter", "-include", "-exclude", "-totalcount", "-tail", "-raw", "-encoding", "-pattern", "-simplematch", "-casesensitive", "-list", "-quiet", "-algorithm", "-property", "-first", "-last", "-unique", "-sum", "-average", "-minimum", "-maximum", "-descending", "-autosize", "-wrap", "-width"}
        # Flags belong to individual programs, not a global query whitelist.
        allowed_flags = {
            "pwd": set(), "ls": {"-a", "-l", "-la", "-al", "-h", "--"},
            "cat": {"-n", "--"}, "head": {"-n", "--"}, "tail": {"-n", "--"},
            "rg": {"-n", "-i", "-l", "--files", "--hidden", "--"}, "grep": {"-n", "-i", "-l", "--"},
            "find": {"-name", "-type", "-maxdepth", "-print"}, "wc": {"-l", "-w", "-c", "--"},
            "date": set(), "whoami": set(), "cd": set(), "echo": {"-n"}, "printf": set(),
            "command": {"-v"}, "type": set(), "diff": {"-q", "--"}, "cmp": {"-s", "--"},
            "stat": set(), "du": {"-h", "-s"}, "sort": {"-n", "-r", "--"}, "uniq": {"-c", "--"},
            "cut": {"-d", "-f", "--"}, "tr": set(), "sha256sum": {"--"}, "sha512sum": {"--"},
            "get-location": set(), "get-date": set(), "get-command": set(), "get-process": set(), "get-service": set(),
            "write-output": set(), "write-host": set(), "set-location": {"-path", "-literalpath"},
        }
        permitted = allowed_flags.get(name, flags)
        normalized = [a.lower() if shell == "powershell" else a for a in args]
        if any(a.startswith("-") and a not in permitted and not re.fullmatch(r"-\d+", a) for a in normalized):
            add("Q-O22" if any(a in {"-exec", "-execdir", "-delete", "--pre", "-o", "--output"} for a in normalized) else "Q-O24")
            return
        if name == "command" and (not args or args[0] != "-v") or name == "date" and args:
            add("Q-O24")
            return
        if shell == "powershell" and any(re.match(r"(?i)^(env|registry|hklm|hkcu|cert):", a) for a in args):
            add("D-05" if any(a.lower().startswith("env:") for a in args) else "Q-O23")
            return
        if name in {"cd", "set-location"}:
            targets = [a for a in args if not a.startswith("-")]
            if len(targets) != 1 or not self.inside(self.path(targets[0], cwd)):
                add("Q-O19")
                return
            add(f"A-{'B' if shell == 'bash' else 'P'}{entry:02d}")
            return
        if name in {"cat", "head", "tail", "grep", "rg", "get-content", "select-string", "sort", "uniq", "cut", "tr", "sha256sum", "sha512sum", "diff", "cmp"}:
            if any(str(p).startswith(("\\\\", "//")) for p in paths):
                add("Q-O20")
                return
            targets = [p for p in paths if p.exists()]
            for target in targets:
                if target.is_dir():
                    count = 0
                    for root, dirs, names in os.walk(target, followlinks=False):
                        dirs[:] = [d for d in dirs if d not in {".git", ".venv", "node_modules", "__pycache__"}]
                        for filename in names:
                            path = Path(root) / filename
                            rule = self.path_rule(path)
                            if rule:
                                add(rule)
                            for custom in self._custom_hits("", [], [path.resolve()]):
                                if custom.decision != "allow":
                                    add(custom)
                            count += 1
                            if count >= 10000:
                                break
                        if count >= 10000:
                            break
            if any(p.is_dir() for p in targets) or not targets and name not in {"sort", "uniq", "cut", "tr"}:
                add("Q-O24")
                return
        if name == "get-command" and args:
            add("Q-O24")
            return
        add(f"A-{'B' if shell == 'bash' else 'P'}{entry:02d}")
