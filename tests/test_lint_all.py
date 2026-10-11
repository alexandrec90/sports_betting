from __future__ import annotations

import importlib
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


MODULE_PATH = Path(__file__).parents[1] / "scripts" / "lint-all.py"
SPEC = importlib.util.spec_from_file_location("lint_all", MODULE_PATH)
assert SPEC and SPEC.loader
lint_all = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(lint_all)


def test_explicit_paths_lint_the_committed_branch_diff(monkeypatch, tmp_path):
    python_file = tmp_path / "sports_betting" / "example.py"
    workflow = tmp_path / ".github" / "workflows" / "ci.yml"
    env_file = tmp_path / ".env.example"
    for path in (python_file, workflow, env_file):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")

    monkeypatch.setattr(lint_all, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(lint_all, "ARTIFACT", tmp_path / "logs" / "lint-errors.log")
    monkeypatch.setattr(
        lint_all.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    checks = []

    def record_check(name, command, _fix_hint):
        checks.append((name, command))
        return ""

    monkeypatch.setattr(lint_all, "run_tool", record_check)

    assert (
        lint_all.main(
            [
                "--paths",
                "sports_betting/example.py",
                ".github/workflows/ci.yml",
                ".env.example",
                "README.md",
            ]
        )
        == 0
    )

    commands = dict(checks)
    assert "sports_betting/example.py" in commands["ruff"]
    assert "sports_betting/example.py" in commands["mypy"]
    assert commands["actionlint"][-1] == ".github/workflows/ci.yml"
    assert commands["dotenv-linter"][-1] == ".env.example"
    assert all("README.md" not in command for command in commands.values())


def test_paths_and_changed_are_mutually_exclusive():
    with pytest.raises(SystemExit):
        lint_all.main(["--changed", "--paths", "sports_betting/example.py"])


def test_explicit_missing_or_unlinted_paths_are_a_clean_noop(monkeypatch, tmp_path):
    monkeypatch.setattr(lint_all, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(lint_all, "ARTIFACT", tmp_path / "logs" / "lint-errors.log")

    assert lint_all.main(["--paths", "deleted.py", "README.md"]) == 0
    assert lint_all.ARTIFACT.read_text(encoding="utf-8") == ""


def test_main_hands_the_run_to_the_tree_venv_when_this_interpreter_has_no_ruff(monkeypatch):
    """Run with an interpreter lacking the linters, it said "ruff: not installed",
    "mypy: not installed" and then "lint-all: clean" -- having linted nothing."""
    repo_root = MODULE_PATH.parents[1]
    # By name, as main() imports it: from `scripts/hooks`, which is not on mypy's path.
    monkeypatch.syspath_prepend(str(repo_root / "scripts" / "hooks"))
    toolchain = importlib.import_module("toolchain")
    calls = []

    def rerun(root, module, script, argv):
        calls.append((root, module, script, argv))
        return 7

    monkeypatch.setattr(toolchain, "rerun_in_venv", rerun)
    monkeypatch.setattr(lint_all, "python_sections", pytest.fail)

    assert lint_all.main(["--changed"]) == 7
    assert calls == [(repo_root, "ruff", MODULE_PATH.resolve(), ["--changed"])]


def test_rerun_in_tree_venv_carries_on_here_without_the_vendored_toolchain(monkeypatch, tmp_path):
    monkeypatch.setattr(lint_all, "REPO_ROOT", tmp_path)

    assert lint_all.rerun_in_tree_venv("ruff", []) is None


def test_selection_narrows_to_the_named_paths_or_the_diff_and_otherwise_not_at_all(monkeypatch):
    monkeypatch.setattr(lint_all, "changed_paths", lambda: ["diff.py"])

    assert lint_all.selection(SimpleNamespace(paths=["a.py"], changed=False)) == ["a.py"]
    assert lint_all.selection(SimpleNamespace(paths=None, changed=True)) == ["diff.py"]
    assert lint_all.selection(SimpleNamespace(paths=None, changed=False)) is None


def test_python_sections_fixes_then_reports_ruff_and_mypy(monkeypatch):
    fixes, checks = [], []
    monkeypatch.setattr(lint_all.subprocess, "run", lambda cmd, **_kw: fixes.append(cmd[2:4]))
    monkeypatch.setattr(
        lint_all, "run_tool", lambda name, cmd, _hint: checks.append((name, cmd)) or f"[{name}]"
    )

    assert lint_all.python_sections(["a.py"], ["a.py"]) == "[ruff][mypy]"
    assert fixes == [["ruff", "check"], ["ruff", "format"]]
    assert [name for name, _ in checks] == ["ruff", "mypy"]
    assert checks[1][1][3:4] == ["a.py"]
