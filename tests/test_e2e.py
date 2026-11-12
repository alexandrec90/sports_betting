"""`scripts/e2e.py`: the local tier's runner and its `.claude/rules/local-e2e.md` contract.

These run everywhere (no `local` marker): they test the runner's pure functions, never the
tier itself, and stub every subprocess `main` would start.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

MODULE_PATH = Path(__file__).parents[1] / "scripts" / "e2e.py"
SPEC = importlib.util.spec_from_file_location("e2e", MODULE_PATH)
assert SPEC and SPEC.loader
e2e = importlib.util.module_from_spec(SPEC)
sys.modules["e2e"] = e2e  # `@dataclass` resolves its module through sys.modules
SPEC.loader.exec_module(e2e)

JUNIT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest">
<testcase classname="tests.test_a" name="test_ok" file="tests/test_a.py" line="3"/>
<testcase classname="tests.test_a.TestGroup" name="test_bad[a b]" file="tests/test_a.py" line="9">
  <failure message="assert 1 == 2&#10;more">trace</failure>
</testcase>
<testcase classname="tests.test_b" name="test_err" file="tests/test_b.py" line="1">
  <error message="fixture blew up">trace</error>
</testcase>
<testcase classname="tests.test_b" name="test_skip" file="tests/test_b.py" line="5">
  <skipped message="no docker"/>
</testcase>
</testsuite></testsuites>
"""


def test_parse_junit_rebuilds_node_ids_and_statuses():
    outcomes = e2e.parse_junit(JUNIT)

    assert [(o.nodeid, o.status, o.message) for o in outcomes] == [
        ("tests/test_a.py::test_ok", "passed", ""),
        ("tests/test_a.py::TestGroup::test_bad[a_b]", "failed", "assert 1 == 2"),
        ("tests/test_b.py::test_err", "error", "fixture blew up"),
        ("tests/test_b.py::test_skip", "skipped", "no docker"),
    ]


def test_parse_junit_without_a_file_attribute_falls_back_to_the_classname():
    text = '<testsuite><testcase classname="tests.test_c" name="test_x"/></testsuite>'

    assert e2e.parse_junit(text)[0].nodeid == "tests.test_c::test_x"


def test_summary_reports_every_non_pass_as_a_failed_line_and_goes_red():
    code, lines = e2e.summarize(e2e.parse_junit(JUNIT))

    assert code == 1
    failed = [line for line in lines if line.startswith("FAILED ")]
    assert [line.split()[1] for line in failed] == [
        "tests/test_a.py::TestGroup::test_bad[a_b]",
        "tests/test_b.py::test_err",
        "tests/test_b.py::test_skip",  # a skip in the tier is a failure
    ]
    assert lines[-1].startswith("e2e: 1 passed, 3 not passed")


def test_summary_of_an_all_green_run_is_green():
    code, lines = e2e.summarize([e2e.Outcome("tests/test_a.py::test_ok", "passed")])

    assert code == 0
    assert not any(line.startswith("FAILED") for line in lines)


def test_a_run_that_executed_nothing_is_red():
    code, lines = e2e.summarize([])

    assert code == 1
    assert "no test marked `local` ran" in lines[-1]


def test_pytest_command_selects_exactly_the_tier_and_writes_the_artifact():
    command = e2e.pytest_command("py", ["-k", "stack"])

    assert command[:3] == ["py", "-m", "pytest"]
    assert command[command.index("-m", 2) + 1] == "local"
    assert f"--junit-xml={e2e.ARTIFACT}" in command
    assert "junit_family=xunit1" in command
    assert command[-2:] == ["-k", "stack"]


def test_the_default_run_deselects_the_tier():
    """The other half of the contract: every run but this script's leaves `local` out."""
    text = (Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8")

    assert 'addopts = "-p devkit_temproot -m \\"not local\\""' in text
    assert '"local: ' in text


def test_venv_python_prefers_the_checkout_interpreter(tmp_path):
    assert e2e.venv_python(tmp_path) is None
    posix = tmp_path / ".venv" / "bin" / "python"
    posix.parent.mkdir(parents=True)
    posix.write_text("", encoding="utf-8")
    assert e2e.venv_python(tmp_path) == posix
    windows = tmp_path / ".venv" / "Scripts" / "python.exe"
    windows.parent.mkdir(parents=True)
    windows.write_text("", encoding="utf-8")
    assert e2e.venv_python(tmp_path) == windows


def _lake_at(path: Path) -> Path:
    path.mkdir(parents=True)
    (path / "pyproject.toml").write_text("", encoding="utf-8")
    return path


def test_sibling_lake_beside_the_checkout(tmp_path):
    lake = _lake_at(tmp_path / "data-lake")

    assert e2e.sibling_lake(tmp_path / "sports_betting", None) == lake


def test_sibling_lake_from_a_worktree_is_found_beside_the_primary_checkout(tmp_path):
    lake = _lake_at(tmp_path / "data-lake")
    primary = tmp_path / "sports_betting"
    worktree = primary / ".claude" / "worktrees" / "x"
    worktree.mkdir(parents=True)

    assert e2e.sibling_lake(worktree, primary / ".git") == lake


def test_no_sibling_lake_is_none(tmp_path):
    assert (
        e2e.sibling_lake(tmp_path / "sports_betting", tmp_path / "sports_betting" / ".git") is None
    )


def _record_runs(monkeypatch, *, probe_code):
    calls = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=probe_code if command[1] == "-c" else 0)

    monkeypatch.setattr(e2e.subprocess, "run", fake_run)
    return calls


def test_ensure_lake_does_nothing_when_the_package_imports(monkeypatch, tmp_path):
    calls = _record_runs(monkeypatch, probe_code=0)

    e2e.ensure_lake("py", tmp_path)

    assert len(calls) == 1  # the probe, and no install


def test_ensure_lake_installs_the_sibling_when_the_package_is_missing(monkeypatch, tmp_path):
    lake = _lake_at(tmp_path / "data-lake")
    calls = _record_runs(monkeypatch, probe_code=1)
    monkeypatch.setattr(e2e, "_git_common_dir", lambda _root: None)

    e2e.ensure_lake("py", tmp_path / "sports_betting")

    assert calls[-1] == ["uv", "pip", "install", "--python", "py", "-e", f"{lake}[archive]"]


def test_ensure_lake_without_a_sibling_reports_and_carries_on(monkeypatch, tmp_path, capsys):
    calls = _record_runs(monkeypatch, probe_code=1)
    monkeypatch.setattr(e2e, "_git_common_dir", lambda _root: None)

    e2e.ensure_lake("py", tmp_path / "sports_betting")

    assert len(calls) == 1
    assert "no sibling data-lake checkout" in capsys.readouterr().out


def test_main_reads_the_artifact_pytest_wrote(monkeypatch, tmp_path, capsys):
    artifact = tmp_path / "logs" / "e2e-junit.xml"
    monkeypatch.setattr(e2e, "ARTIFACT", artifact)
    monkeypatch.setattr(e2e, "ensure_lake", lambda *_args: None)

    def fake_run(command, **_kwargs):
        artifact.write_text(JUNIT, encoding="utf-8")
        return subprocess.CompletedProcess(command, 1)

    monkeypatch.setattr(e2e.subprocess, "run", fake_run)

    assert e2e.main([]) == 1
    assert "FAILED tests/test_b.py::test_skip - skipped: no docker" in capsys.readouterr().out


def test_main_is_red_when_pytest_wrote_no_artifact(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(e2e, "ARTIFACT", tmp_path / "logs" / "e2e-junit.xml")
    monkeypatch.setattr(e2e, "ensure_lake", lambda *_args: None)
    monkeypatch.setattr(
        e2e.subprocess, "run", lambda command, **_kw: subprocess.CompletedProcess(command, 4)
    )

    assert e2e.main([]) == 4
    assert "without writing" in capsys.readouterr().out


def test_git_common_dir_is_none_outside_a_repository(monkeypatch, tmp_path):
    monkeypatch.setattr(
        e2e.subprocess,
        "run",
        lambda command, **_kw: subprocess.CompletedProcess(command, 128, "", ""),
    )

    assert e2e._git_common_dir(tmp_path) is None


def test_lake_import_fails_naming_the_install_rather_than_skipping(lake_module):
    """`tests/conftest.py`'s `lake_module`: the tier's tests fail where CI's would have skipped."""
    with pytest.raises(pytest.fail.Exception, match=r"uv pip install -e .*scripts/e2e\.py"):
        lake_module("data_lake_that_is_not_installed.catalog")


def test_lake_import_returns_the_module(lake_module):
    assert lake_module("json") is json


def test_lake_import_reraises_a_missing_dependency_of_the_package(monkeypatch, lake_module):
    def broken(_name):
        raise ModuleNotFoundError("No module named 'boto3'", name="boto3")

    monkeypatch.setattr(importlib, "import_module", broken)

    with pytest.raises(ModuleNotFoundError):
        lake_module("data_lake.archive.catalog")
