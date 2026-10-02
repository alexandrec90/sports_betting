"""The `collector` service must see every setting the scheduler reads, with the same defaults.

`.dockerignore` keeps `.env` out of the image, so the container's settings are exactly the
compose `environment:` block — a field missing there is a `.env` line the collector silently
ignores. Two shapes of that drift had already happened:

- `BULK_REFRESH_*` were never passed, so `BULK_REFRESH_ENABLED=true` in `.env` could not
  enable the weekly refresh the README documents.
- `BALLDONTLIE_SPORTS` defaulted to `nba,nfl,mlb,epl` here while `Settings` had dropped
  `epl` (it needs a paid plan). A checkout whose `.env` set a key but not the sport list
  made balldontlie `partial` on every run, so `sports-betting health` never exited 0.

Parsed with a regex rather than PyYAML, which is only a transitive dev dependency.
"""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import TypeAdapter

from sports_betting.config import Settings

COMPOSE_FILE = Path(__file__).resolve().parents[1] / "docker-compose.yml"

#: The mirror runs on the host: the image has no sibling lake package to run it with.
HOST_ONLY = {
    "archive_backend",
    "archive_local_dir",
    "archive_s3_bucket",
    "archive_s3_endpoint_url",
    "archive_s3_region",
    "archive_s3_access_key_id",
    "archive_s3_secret_access_key",
    "archive_s3_prefix",
}

#: Pinned to in-container paths on purpose; the host defaults would be wrong there.
CONTAINER_PATHS = {
    "archive_root",
    "scheduler_health_file",
    "provider_quota_file",
    "odds_refresh_file",
}

PASSTHROUGH = re.compile(r"^\$\{(?P<name>[A-Z_]+):-(?P<default>.*)\}$")


def collector_environment(text: str) -> dict[str, str]:
    block = re.search(
        r"^  collector:\n(?:    .*\n|\n)*?    environment:\n(?P<body>(?:      .*\n)+)",
        text,
        re.MULTILINE,
    )
    assert block, "no environment block under the collector service"
    pairs = re.findall(r"^      ([A-Z_]+):\s*(.*)$", block.group("body"), re.MULTILINE)
    return {name: value.strip() for name, value in pairs}


def test_parser_reads_the_collector_block_only():
    text = (
        "services:\n"
        "  collector:\n"
        "    build: .\n"
        "    environment:\n"
        "      A: ${A:-1}\n"
        "      B: /literal\n"
        "    volumes:\n"
        "      - ./x:/x\n"
        "  db:\n"
        "    environment:\n"
        "      C: ${C:-2}\n"
    )
    assert collector_environment(text) == {"A": "${A:-1}", "B": "/literal"}


def test_every_scheduler_setting_reaches_the_container():
    environment = collector_environment(COMPOSE_FILE.read_text(encoding="utf-8"))
    expected = {name.upper() for name in Settings.model_fields} - {
        name.upper() for name in HOST_ONLY
    }
    missing = sorted(expected - environment.keys())
    assert not missing, f"collector environment does not pass {missing}"


def test_container_paths_are_literals():
    environment = collector_environment(COMPOSE_FILE.read_text(encoding="utf-8"))
    for name in CONTAINER_PATHS:
        value = environment[name.upper()]
        assert value.startswith("/"), f"{name.upper()} must be an in-container path: {value}"


def test_compose_defaults_match_settings_defaults():
    environment = collector_environment(COMPOSE_FILE.read_text(encoding="utf-8"))
    mismatched = []
    for name, field in Settings.model_fields.items():
        if name in HOST_ONLY | CONTAINER_PATHS:
            continue
        match = PASSTHROUGH.match(environment.get(name.upper(), ""))
        assert match, f"{name.upper()} must be passed as ${{{name.upper()}:-<default>}}"
        assert match["name"] == name.upper(), f"{name.upper()} interpolates {match['name']}"
        parsed = TypeAdapter(field.annotation).validate_python(match["default"].strip('"'))
        if parsed != field.default:
            mismatched.append(f"{name.upper()}: compose {parsed!r} != Settings {field.default!r}")
    assert not mismatched, mismatched
