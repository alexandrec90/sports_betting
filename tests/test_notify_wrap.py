"""Tests for the VS Code task notification wrapper under `scripts/`.

The wrapper's whole contract is the two things a task layer depends on and that break
silently: it must exit with the wrapped command's code, because that is what makes VS
Code draw a green check or a red X, and it must toast exactly once with a pass/fail
verdict. Everything here drives `main()` through a fake clock and a fake
`subprocess.run` so no toast is sent and no child process is spawned.

Commands in the argv fixtures below are deliberately generic (`python -m pytest`,
`npm run dev`). Naming a real script path here would put this file in that script's
coverage corpus, where the `.main()` call below would read as a test of *its* `main`.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


MODULE_PATH = Path(__file__).parents[1] / "scripts" / "notify-wrap.py"
SPEC = importlib.util.spec_from_file_location("notify_wrap", MODULE_PATH)
assert SPEC and SPEC.loader
notify_wrap = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(notify_wrap)


def drive(monkeypatch, argv, *, returncode=0, elapsed=0.0, launcher_is_a_shim=False):
    """Run `main()` on `argv` (without the program name) and report what it did.

    Returns the exit code, the `subprocess.run` calls as `(command, kwargs)`, and the
    toasts as `(title, message)`. `elapsed` is the wall time the fake clock reports;
    `launcher_is_a_shim` makes the first spawn raise `FileNotFoundError`, which is how
    Windows refuses a `.cmd` launcher such as npm.
    """
    runs: list[tuple[list[str], dict]] = []
    toasts: list[tuple[str, str]] = []

    def fake_run(command, **kwargs):
        runs.append((list(command), kwargs))
        if launcher_is_a_shim and len(runs) == 1:
            raise FileNotFoundError(command[0])
        return SimpleNamespace(returncode=returncode)

    ticks = iter([0.0, elapsed])
    monkeypatch.setattr(notify_wrap.sys, "argv", ["notify-wrap.py", *argv])
    monkeypatch.setattr(notify_wrap.subprocess, "run", fake_run)
    monkeypatch.setattr(notify_wrap, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    monkeypatch.setattr(
        notify_wrap, "notify", lambda title, message: toasts.append((title, message))
    )
    return notify_wrap.main(), runs, toasts


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["Test: Run pytest"],  # no separator
        ["--", "python", "-m", "pytest"],  # no title
        ["Test: Run pytest", "--"],  # no command
    ],
)
def test_a_malformed_invocation_exits_two_without_running_anything(monkeypatch, argv):
    code, runs, toasts = drive(monkeypatch, argv)
    assert code == 2
    assert runs == [], "spawned a command it could not parse"
    assert toasts == [], "toasted a verdict on a task that never ran"


def test_a_passing_command_exits_zero_and_toasts_the_elapsed_time(monkeypatch):
    code, runs, toasts = drive(
        monkeypatch, ["Test: Run pytest", "--", "python", "-m", "pytest"], elapsed=5.4
    )
    assert code == 0
    assert runs[0][0] == ["python", "-m", "pytest"]
    assert toasts == [("Test: Run pytest", "Passed in 5s")]


def test_a_failing_command_propagates_its_exit_code_so_the_task_icon_is_red(monkeypatch):
    code, _runs, toasts = drive(
        monkeypatch, ["Test: Run pytest", "--", "python", "-m", "pytest"], returncode=3, elapsed=1.0
    )
    assert code == 3
    assert toasts == [("Test: Run pytest", "Failed (1s)")]


def test_an_unquoted_multi_word_title_is_rejoined(monkeypatch):
    _code, _runs, toasts = drive(
        monkeypatch, ["Ship:", "Sweep", "Workspace", "--", "python", "-m", "pytest"], elapsed=2.0
    )
    assert toasts[0][0] == "Ship: Sweep Workspace"


def test_a_run_over_a_minute_is_reported_in_minutes_and_seconds(monkeypatch):
    _code, _runs, toasts = drive(
        monkeypatch, ["Test: Run pytest", "--", "python", "-m", "pytest"], elapsed=125.9
    )
    assert toasts == [("Test: Run pytest", "Passed in 2m 5s")]


def test_a_batch_launcher_is_retried_through_the_shell(monkeypatch):
    """Windows cannot `CreateProcess` the `.cmd` shims that npm, npx and vite install,
    so the wrapper retries the same command with `shell=True` rather than reporting a
    failure the task never had."""
    code, runs, toasts = drive(
        monkeypatch, ["Web: Dev", "--", "npm", "run", "dev"], elapsed=1.0, launcher_is_a_shim=True
    )
    assert [kwargs.get("shell", False) for _command, kwargs in runs] == [False, True]
    assert runs[1][0] == ["npm", "run", "dev"], "retried something other than the task's command"
    assert code == 0
    assert toasts == [("Web: Dev", "Passed in 1s")]
