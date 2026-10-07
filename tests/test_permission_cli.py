from __future__ import annotations

from types import SimpleNamespace

from typer.testing import CliRunner

from app import cli
from app.permissions import PermissionEngine
from tests import make_workspace


def test_noninteractive_confirmation_never_auto_approves(monkeypatch):
    workspace = make_workspace()
    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(isatty=lambda: False))
    confirm = cli._make_confirm_fn(workspace)
    assert confirm("bash", {"command": "anything", "_permission": {"reusable": True}}) == "unavailable"


def test_yes_is_deprecated_not_a_permission_bypass(capsys):
    workspace = make_workspace()
    agent = cli._build_agent(workspace, yes=True)
    assert not agent.settings.auto_confirm_risky_writes
    assert agent.nodes.confirm_fn is not None
    assert "does not bypass" in capsys.readouterr().err


def test_permission_inspection_does_not_execute_command():
    workspace = make_workspace()
    result = CliRunner().invoke(cli.app, ["permissions", "--cwd", str(workspace), "--shell", "bash", "--command", "echo hello > marker.txt"])
    assert result.exit_code == 0
    assert "ask:" in result.stdout
    assert not (workspace / "marker.txt").exists()


def test_approval_preview_redacts_and_has_scope_and_program(monkeypatch, capsys):
    workspace = make_workspace()
    monkeypatch.setenv("OPENAI_API_KEY", "hidden-preview-key")
    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(cli.typer, "prompt", lambda *_args, **_kwargs: "session")
    answer = cli._make_confirm_fn(workspace)("bash", {
        "command": "echo hidden-preview-key", "_permission": {"reusable": True, "rule_id": "Q-S01",
        "reason": "tests", "executables": ["trusted-python"], "effects": []},
    })
    output = capsys.readouterr().out
    assert answer == "approve_session"
    assert "hidden-preview-key" not in output
    assert "trusted-python" in output
    assert "no OS" in output
    assert "expires" in output
