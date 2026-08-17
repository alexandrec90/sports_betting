"""This repo's two OTLP endpoints must agree, and must be the workspace singleton.

There is **one** OTLP collector in this workspace, not one per project: it runs in
carameli's `monitoring` compose profile and devkit's `ports.toml` pins its port under
`[shared]` rather than `[services]`, because a `[services]` base is offset by slot and a
singleton is not.

Until 2026-08-17 it was a `[services]` base, so this project was scaffolded pointing at
slot 4 of it — port 4322 — where nothing has ever listened. That cost a month of agent
telemetry and produced no error at any point, because an OTLP exporter whose endpoint
refuses the connection retries in the background and the program carries on.

The assertion here is deliberately repo-local. Reading devkit's `ports.toml` would be
the stronger check but it is not reachable from a clone (`$DEVKIT_DIR` is a property of
the machine), and a check that silently no-ops when its input is missing is worse than
one that is narrow: it reports green having compared nothing.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SETTINGS_FILE = REPO_ROOT / ".claude" / "settings.json"
ENV_EXAMPLE = REPO_ROOT / ".env.example"

# The shared collector, mirrored from `[shared] otel_http` in devkit's ports.toml.
SHARED_OTLP_PORT = 4318


def _settings_endpoint() -> str:
    env = json.loads(SETTINGS_FILE.read_text(encoding="utf-8")).get("env") or {}
    return env.get("OTEL_EXPORTER_OTLP_ENDPOINT", "")


def _env_example_endpoint() -> str:
    match = re.search(
        r"^OTEL_EXPORTER_OTLP_ENDPOINT=(.+)$",
        ENV_EXAMPLE.read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    return match.group(1).strip() if match else ""


def test_agent_and_app_export_to_the_same_collector() -> None:
    """`.claude/settings.json` is the agent's exporter; `.env.example` is the app's.

    Two halves of the same decision, in two files nothing else holds together — which
    is exactly the pair that drifts when one of them is updated by hand.
    """
    assert _settings_endpoint() == _env_example_endpoint(), (
        f"{SETTINGS_FILE.name} exports to {_settings_endpoint()!r} but "
        f"{ENV_EXAMPLE.name} exports to {_env_example_endpoint()!r}"
    )


def test_the_endpoint_is_the_shared_collector_port() -> None:
    assert _settings_endpoint() == f"http://localhost:{SHARED_OTLP_PORT}", (
        f"expected the workspace-shared collector on {SHARED_OTLP_PORT}; a per-project "
        f"port here is a socket nothing listens on, and the exporter will never say so"
    )
