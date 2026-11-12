"""End to end: the shipped collector image, run through `docker-compose.yml` on this machine.

Local tier only (`-m local`, run by `scripts/e2e.py`): it needs the Docker daemon, which no
hosted runner here has. It builds the `prod` stage exactly as `docker compose up` would and
runs the CLI in it, so it covers what no unit test reaches -- the image installs, the
`sports-betting` entry point exists, the non-root `appuser` can write to the bind mounts,
and the compose `environment` block points the health artifact at the mounted `logs/`,
where the host reads it back.

Isolated from the running stack: its own compose project name, and an override file that
remaps both bind mounts to `tmp_path`, so it never touches the pooled lake or this
checkout's `logs/`. The collector job it runs is `historical-bulk` with refresh forced off,
which records a skip without a network call or a provider key.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PROJECT = "sports_betting_e2e"
BUILD_TIMEOUT_SECONDS = 1200
RUN_TIMEOUT_SECONDS = 180


def override_document(lake: Path, logs: Path) -> dict:
    """A compose override remapping the collector's mounts (merged by target) to `lake`/`logs`."""

    def bind(source: Path, target: str) -> dict:
        return {"type": "bind", "source": source.as_posix(), "target": target}

    return {
        "services": {
            "collector": {
                "environment": {"BULK_REFRESH_ENABLED": "false"},
                "volumes": [bind(lake, "/data-lake"), bind(logs, "/app/logs")],
            }
        }
    }


def _compose(override: Path, *args: str, timeout: int) -> subprocess.CompletedProcess[str]:
    command = ["docker", "compose", "-p", PROJECT, "-f", "docker-compose.yml", "-f", str(override)]
    return subprocess.run(  # noqa: S603 - a fixed docker compose argv this test builds
        [*command, *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def _require(result: subprocess.CompletedProcess[str], what: str) -> None:
    if result.returncode != 0:
        pytest.fail(
            f"{what} exited {result.returncode}:\n{result.stdout[-3000:]}\n{result.stderr[-3000:]}"
        )


@pytest.fixture
def stack(tmp_path: Path) -> Iterator[tuple[Path, Path]]:
    """The built image and an override file; yields `(override, logs)` and tears the project down."""
    if shutil.which("docker") is None:
        pytest.fail("the `docker` CLI is not on PATH; this tier needs Docker Desktop")
    daemon = subprocess.run(
        ["docker", "info"],  # noqa: S607 - docker from PATH on purpose; checked just above
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if daemon.returncode != 0:
        pytest.fail(
            f"the Docker daemon is not reachable; start Docker Desktop:\n{daemon.stderr[-1000:]}"
        )
    lake, logs = tmp_path / "lake", tmp_path / "logs"
    lake.mkdir()
    logs.mkdir()
    override = tmp_path / "compose.override.json"
    override.write_text(json.dumps(override_document(lake, logs)), encoding="utf-8")
    _require(
        _compose(override, "build", "collector", timeout=BUILD_TIMEOUT_SECONDS), "compose build"
    )
    try:
        yield override, logs
    finally:
        _compose(override, "down", "--remove-orphans", timeout=RUN_TIMEOUT_SECONDS)


def _run(override: Path, *cli: str) -> subprocess.CompletedProcess[str]:
    return _compose(
        override,
        "run",
        "--rm",
        "--no-deps",
        "-T",
        "collector",
        "sports-betting",
        *cli,
        timeout=RUN_TIMEOUT_SECONDS,
    )


@pytest.mark.local
def test_collector_image_collects_and_reports_health_through_the_mounts(stack):
    override, logs = stack

    collect = _run(override, "collect", "--provider", "historical-bulk")
    _require(collect, "sports-betting collect")
    payload = json.loads(collect.stdout.strip().splitlines()[-1])
    assert payload["jobs"]["historical-bulk"]["status"] == "skipped", payload

    # The compose environment block points the artifact at /app/logs, mounted from the host.
    health_file = logs / "scheduler-health.json"
    assert health_file.is_file(), f"no health artifact on the host mount: {sorted(logs.iterdir())}"
    recorded = json.loads(health_file.read_text(encoding="utf-8"))
    assert recorded["jobs"]["historical-bulk"]["skip_reason"] == "bulk refresh not enabled"
    assert (logs / "ingest-events.json").is_file(), "the run report did not reach the mount"

    health = _run(override, "health")
    _require(health, "sports-betting health")
    assert "historical-bulk" in health.stdout
    assert "skipped" in health.stdout


def test_override_remaps_both_mounts_by_target(tmp_path):
    """The override must name the same targets as `docker-compose.yml`, or compose would add
    a mount beside the real one instead of replacing it -- and write into the pooled lake."""
    compose = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    document = override_document(tmp_path / "lake", tmp_path / "logs")

    targets = [volume["target"] for volume in document["services"]["collector"]["volumes"]]

    for target in targets:
        assert f":{target}\n" in compose, f"{target} is not a collector mount target"
    assert all(
        volume["source"].startswith(tmp_path.as_posix())
        for volume in document["services"]["collector"]["volumes"]
    )
