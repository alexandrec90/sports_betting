#!/usr/bin/env python3
"""Run the local end-to-end tier: the tests only this workstation can run.

These are the tests marked `local` -- the Docker stack, the sibling `data-lake` checkout --
which `pyproject.toml`'s `addopts` deselects from every other run, CI's included. devkit's
nightly `devkit-local-e2e` job runs this script in the checkout root with `DEVKIT_E2E=1`;
the contract it keeps is `.claude/rules/local-e2e.md`'s:

- exit 0 green, anything else red;
- one `FAILED <id>` line per failure, error *and skip* -- a skip in this tier is a failure,
  since the checkout is the one the tests were written for -- with no space in the id;
- a run that executed nothing is red, because a green from zero tests is a skip.

Before pytest starts it installs the sibling `data-lake` package into the interpreter when
that interpreter lacks it and the sibling is on disk: `uv sync` prunes it (it is not a
declared dependency -- see `pyproject.toml`), so a fresh worktree's `.venv` never has it.

Stdlib only until pytest runs under the checkout's `.venv` interpreter: the scheduler, or a
VS Code task, may start this under the machine's Python. The failure artifact is the JUnit
XML at `logs/e2e-junit.xml`, overwritten per run.

Usage:
    python scripts/e2e.py            # the whole tier
    python scripts/e2e.py -k stack   # extra arguments go to pytest
"""

from __future__ import annotations

import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ARTIFACT = REPO_ROOT / "logs" / "e2e-junit.xml"
MARKER = "local"
LAKE_PACKAGE = "data_lake"
LAKE_DIRNAME = "data-lake"


@dataclass(frozen=True)
class Outcome:
    """One test case's result, read back from the JUnit artifact."""

    nodeid: str
    status: str  # "passed", "failed", "error" or "skipped"
    message: str = ""


def venv_python(root: Path) -> Path | None:
    """The checkout's own `.venv` interpreter, or None where the checkout has none."""
    for candidate in (root / ".venv" / "Scripts" / "python.exe", root / ".venv" / "bin" / "python"):
        if candidate.is_file():
            return candidate
    return None


def sibling_lake(root: Path, git_common_dir: Path | None) -> Path | None:
    """The `data-lake` checkout beside this project's primary checkout, if there is one.

    A worktree lives under `.claude/worktrees/`, so `../data-lake` from it is nowhere; the
    primary checkout is the parent of the shared `.git` directory, and the sibling is beside
    that.
    """
    bases = [root]
    if git_common_dir is not None:
        bases.append(git_common_dir.resolve().parent)
    for base in bases:
        candidate = base.parent / LAKE_DIRNAME
        if (candidate / "pyproject.toml").is_file():
            return candidate
    return None


def pytest_command(python: Path | str, extra: Sequence[str]) -> list[str]:
    """The pytest run that selects exactly the tier, writing its JUnit artifact."""
    return [
        str(python),
        "-m",
        "pytest",
        "-m",
        MARKER,
        "-q",
        "-rN",  # the summary is this script's, from the artifact, in the contract's shape
        "-o",
        "junit_family=xunit1",  # carries each case's `file`, which the node id needs
        f"--junit-xml={ARTIFACT}",
        *extra,
    ]


def _nodeid(case: ET.Element) -> str:
    """Rebuild pytest's node id from an xunit1 test case, with no whitespace in it."""
    name = case.get("name", "")
    classname = case.get("classname", "")
    path = case.get("file", "")
    if not path:
        nodeid = f"{classname}::{name}" if classname else name
    else:
        module = path.removesuffix(".py").replace("\\", ".").replace("/", ".")
        inner = classname.removeprefix(module).strip(".")
        parts = [path.replace("\\", "/"), *([inner.replace(".", "::")] if inner else []), name]
        nodeid = "::".join(parts)
    return "_".join(nodeid.split())


def parse_junit(text: str) -> list[Outcome]:
    """Every test case in a JUnit XML document, with its status and first message line."""
    outcomes = []
    # The XML is the artifact this script's own pytest run just wrote, not outside input.
    for case in ET.fromstring(text).iter("testcase"):  # noqa: S314 - our own artifact
        status, message = "passed", ""
        for tag in ("failure", "error", "skipped"):
            child = case.find(tag)
            if child is not None:
                status = "failed" if tag == "failure" else tag
                text_lines = (child.get("message") or child.text or "").strip().splitlines()
                message = text_lines[0] if text_lines else ""
                break
        outcomes.append(Outcome(_nodeid(case), status, message))
    return outcomes


def summarize(outcomes: Sequence[Outcome]) -> tuple[int, list[str]]:
    """The exit code and the report lines: `FAILED <id>` for every non-pass, then a count."""
    lines = [
        f"FAILED {o.nodeid} - {o.status}" + (f": {o.message}" if o.message else "")
        for o in outcomes
        if o.status != "passed"
    ]
    passed = sum(o.status == "passed" for o in outcomes)
    if not outcomes:
        lines.append(f"e2e: no test marked `{MARKER}` ran; a tier that runs nothing is red")
        return 1, lines
    lines.append(f"e2e: {passed} passed, {len(outcomes) - passed} not passed; artifact: {ARTIFACT}")
    return (0 if passed == len(outcomes) else 1), lines


def _git_common_dir(root: Path) -> Path | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    return Path(result.stdout.strip()) if result.returncode == 0 and result.stdout.strip() else None


def ensure_lake(python: Path | str, root: Path) -> None:
    """Install the sibling lake package into `python` when it lacks it and it is on disk.

    A miss is reported, not fatal: the tests that need the package then fail naming it.
    """
    probe = [
        str(python),
        "-c",
        f"import importlib.util as u; raise SystemExit(u.find_spec({LAKE_PACKAGE!r}) is None)",
    ]
    if subprocess.run(probe, cwd=root, check=False).returncode == 0:
        return
    lake = sibling_lake(root, _git_common_dir(root))
    if lake is None:
        print(f"e2e: no sibling {LAKE_DIRNAME} checkout found; its tests will fail naming it")
        return
    command = ["uv", "pip", "install", "--python", str(python), "-e", f"{lake}[archive]"]
    print(f"e2e: installing the sibling lake package: {' '.join(command)}")
    try:
        subprocess.run(command, cwd=root, check=False)
    except OSError as exc:
        print(f"e2e: could not run uv ({exc}); the lake tests will fail naming the package")


def main(argv: Sequence[str] | None = None) -> int:
    extra = list(sys.argv[1:] if argv is None else argv)
    python = venv_python(REPO_ROOT) or Path(sys.executable)
    ensure_lake(python, REPO_ROOT)
    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT.unlink(missing_ok=True)
    env = {**os.environ, "PYTHONUTF8": "1"}
    pytest_code = subprocess.run(
        pytest_command(python, extra), cwd=REPO_ROOT, env=env, check=False
    ).returncode
    if not ARTIFACT.is_file():
        print(f"e2e: pytest exited {pytest_code} without writing {ARTIFACT}; the run is red")
        return pytest_code or 1
    code, lines = summarize(parse_junit(ARTIFACT.read_text(encoding="utf-8")))
    print("\n".join(lines))
    return code or (pytest_code if pytest_code not in (0, 5) else 0)


if __name__ == "__main__":
    # Line-buffered, so this script's lines land between its children's output in order;
    # block-buffered (a pipe, the scheduler's log) they all arrived after pytest's.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    sys.exit(main())
