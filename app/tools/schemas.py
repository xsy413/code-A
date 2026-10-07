from __future__ import annotations


def _function(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


TOOL_SCHEMAS = [
    _function("inspect_workspace", "Inspect top-level files, source roots and test entrypoints.", {}, []),
    _function("list_files", "List workspace files matching a glob.", {
        "pattern": {"type": "string", "description": "Glob pattern; defaults to *."},
        "cursor": {"type": "string"},
    }, []),
    _function("read_file", "Read a workspace file, optionally limited to a line range.", {
        "path": {"type": "string"},
        "line_start": {"type": "integer"},
        "line_end": {"type": "integer"},
        "column_start": {"type": "integer"},
        "expected_version": {"type": "string"},
    }, ["path"]),
    _function("search_text", "Search workspace file contents.", {
        "pattern": {"type": "string"},
        "is_regex": {"type": "boolean"},
        "include_glob": {"type": "string"},
        "cursor": {"type": "string"},
    }, ["pattern"]),
    _function("read_tool_result", "Read a completed, redacted result from this session. Archived source is historical, not evidence of current file contents.", {
        "result_id": {"type": "string"}, "stream": {"type": "string", "enum": ["stdout", "stderr"]},
        "line_start": {"type": "integer"}, "line_end": {"type": "integer"}, "column_start": {"type": "integer"},
    }, ["result_id"]),
    _function("write_file", "Create or overwrite a file with the full content.", {
        "path": {"type": "string"},
        "content": {"type": "string"},
    }, ["path", "content"]),
    _function("patch_file", "Replace an exact, unique occurrence in a file.", {
        "path": {"type": "string"},
        "old_str": {"type": "string"},
        "new_str": {"type": "string"},
    }, ["path", "old_str", "new_str"]),
    _function("delete_file", "Delete a workspace file, backing up its original content.", {
        "path": {"type": "string"},
    }, ["path"]),
    *[_function(shell, "Execute a foreground shell command subject to allow/ask/deny permissions. Approved code runs as the current user without OS isolation.", {
        "command": {"type": "string"},
        "cwd": {"type": "string"},
        "timeout_s": {"type": "integer", "minimum": 1, "maximum": 600},
    }, ["command"]) for shell in ("bash", "powershell")],
]


def tool_schemas(shells) -> list[dict]:
    return [s for s in TOOL_SCHEMAS if s["function"]["name"] not in {"bash", "powershell"} or s["function"]["name"] in shells]


def validate_tool_args(name: str, args: dict) -> None:
    schemas = {item["function"]["name"]: item["function"]["parameters"] for item in TOOL_SCHEMAS}
    if name not in schemas:
        raise ValueError(f"Unsupported tool: {name}")
    schema = schemas[name]
    for key in schema["required"]:
        if key not in args:
            raise ValueError(f"{name}: missing required argument {key}")
    types = {"string": str, "integer": int, "boolean": bool}
    for key, value in args.items():
        if key not in schema["properties"]:
            raise ValueError(f"{name}: unknown argument {key}")
        expected = schema["properties"][key]["type"]
        if type(value) is not types[expected]:
            raise ValueError(f"{name}.{key}: expected {expected}")
        if "enum" in schema["properties"][key] and value not in schema["properties"][key]["enum"]:
            raise ValueError(f"{name}.{key}: invalid choice")
        if key in {"line_start", "line_end", "column_start"} and value < 1:
            raise ValueError(f"{name}.{key}: must be positive")
    if args.get("line_end", args.get("line_start", 1)) < args.get("line_start", 1):
        raise ValueError("line_end precedes line_start")
    if name in {"bash", "powershell"}:
        if not args["command"].strip():
            raise ValueError(f"{name}: command must not be empty")
        if len(args["command"]) > 65536:
            raise ValueError(f"{name}: command exceeds 65536 characters")
        if not 1 <= args.get("timeout_s", 60) <= 600:
            raise ValueError(f"{name}: timeout_s must be between 1 and 600")
