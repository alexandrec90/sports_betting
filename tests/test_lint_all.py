from __future__ import annotations

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
