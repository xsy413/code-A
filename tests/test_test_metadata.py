from __future__ import annotations

import pytest

from app.permissions.parser import parse_bash
from app.tools.testing import describe_test, test_outcome as outcome


def describe(command):
    return describe_test(parse_bash(command), command, "/workspace", ["/usr/bin/python"])


@pytest.mark.parametrize("command", ["pytest tests/test_a.py -k small", "python -m pytest tests/test_a.py -k small"])
def test_known_pytest_selection_is_literal_and_not_entire_project(command):
    info = describe(command)
    assert info["direct"]
    assert info["scope"] == {"kind": "targets", "targets": ["tests/test_a.py"], "selectors": {"-k": ["small"]}}
    assert info["cwd"] == "/workspace"
    assert info["executables"] == ["/usr/bin/python"]


@pytest.mark.parametrize("command", ["pytest", "pytest -q"])
def test_no_explicit_targets_are_default_selection_not_all_project(command):
    assert describe(command)["scope"]["kind"] == "default_selection"


@pytest.mark.parametrize("command", ["pytest --plugin-specific value", "pytest tests/test_*.py", "pytest -k"])
def test_unknown_selection_is_not_guessed(command):
    assert describe(command)["scope"]["kind"] == "unknown"


@pytest.mark.parametrize("command", ["pytest --version", "pytest --help", "pytest --collect-only", "pytest --co",
                                    "go test -list .", "cargo test -- --list", "dotnet test --list-tests", "gradle test --dry-run"])
def test_listing_and_collection_modes_are_not_test_executions(command):
    assert not describe(command)


@pytest.mark.parametrize("command", ["npm test", "npm run test", "python custom.py", "./tests.sh", "pytest || true",
                                    "! pytest", "if false; then pytest; fi", "pytest | cat", "echo $(pytest)"])
def test_opaque_and_conditional_entries_cannot_prove_tests_passed(command):
    info = describe(command)
    assert not info["direct"]
    assert info["scope"]["kind"] == "unknown"
    assert outcome(info, {"ok": True, "exit_code": 0}) == "unknown"
    assert outcome(info, {"ok": False, "exit_code": 2}) == "failed"


@pytest.mark.parametrize("runner", ["go", "cargo", "dotnet", "mvn", "gradle"])
def test_other_direct_runners_have_no_assumed_full_coverage(runner):
    info = describe(f"{runner} test")
    assert info["direct"]
    assert info["scope"]["kind"] != "all_project"
    assert outcome(info, {"ok": True, "exit_code": 0}) == "passed"


def test_go_package_and_selector_are_recorded():
    info = describe("go test ./cache -run TestLRU")
    assert info["scope"] == {"kind": "targets", "targets": ["./cache"], "selectors": {"-run": "TestLRU"}}


@pytest.mark.parametrize("fields,status", [
    ({"ok": True, "exit_code": 0}, "passed"),
    ({"ok": False, "exit_code": 1}, "failed"),
    ({"ok": False, "exit_code": 5}, "no_tests"),
    ({"ok": False, "exit_code": 124, "error_kind": "timeout"}, "interrupted"),
    ({"ok": False, "exit_code": 1, "error_kind": "cancelled"}, "interrupted"),
    ({"ok": False, "exit_code": 1, "execution_status": "indeterminate"}, "interrupted"),
    ({"ok": False, "exit_code": 13, "execution_status": "denied"}, "not_run"),
    ({"ok": False, "exit_code": 13, "execution_status": "rejected"}, "not_run"),
    ({"ok": False, "exit_code": 0, "execution_status": "not_executed"}, "not_run"),
])
def test_results_use_execution_facts_not_output_keywords(fields, status):
    assert outcome(describe("pytest"), {**fields, "stdout": "all passed assertion failed no tests"}) == status


def test_exit_code_five_is_not_a_universal_no_tests_result():
    assert outcome(describe("go test"), {"ok": False, "exit_code": 5, "stdout": "no tests collected"}) == "failed"


def test_incomplete_old_execution_evidence_has_unknown_conclusion():
    assert outcome(describe("pytest"), {"ok": True}) == "unknown"
    assert outcome(describe("pytest"), {"exit_code": 0}) == "unknown"


@pytest.mark.parametrize("command", ["pwd", "echo 'pytest passed'", "python -c 'print(1)'", "git status"])
def test_ordinary_commands_have_no_test_description(command):
    assert describe(command) == {}
