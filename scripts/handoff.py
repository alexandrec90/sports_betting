#!/usr/bin/env python3
"""Hand the collector, and everything it has collected, from one PC to the other.

The `collector` service runs on one of two Windows PCs and writes two things that are the
machine's own: its bronze datasets under `ARCHIVE_ROOT` (the pooled tree in the sibling
data-lake checkout) and its scheduler and budget state under `logs/`. Both travel through
the shared S3-compatible store `archive-sync` mirrors into (`ARCHIVE_*` in `.env`). The
Postgres `db` service is not carried: nothing reads it. devkit's `scripts/collectors.py`
drives this; it is stdlib-only and cannot reach the store, so it stops and starts the
container and calls this script for everything in between, from this checkout:

    uv run python scripts/handoff.py export --from PC-A --to PC-B
    uv run python scripts/handoff.py pending --host PC-B
    uv run python scripts/handoff.py import --host PC-B

- **export** brings this disk and the store together (`converge.pull`, then
  `converge.push`), uploads the state files, then the manifest of what the other machine
  must end up holding, and only then `marker.json`, addressed to `--to`.
- **pending** reads the marker; prints it as JSON when it is `pending` and addressed to
  `--host`, nothing otherwise.
- **import** checks the manifest and state files against the marker, stops `collector`,
  pulls under the merge rule in `sports_betting/archive/converge.py` -- a local partition
  is merged into, never written over -- verifies the result against the manifest, restores
  the state files, and marks the handoff `imported`. It never starts `collector`.

Objects, under `handoff/sports_betting/` in the store (its `ARCHIVE_S3_PREFIX` applies):
`marker.json`, `manifest.json`, and `state/<file>` for each state file.

Exit codes: 0 success, 1 failed (the reason is in the artifact), 2 cannot tell -- only
`pending` uses it, for a store it cannot read. Every run overwrites `logs/handoff.json`
with `{"command", "ok", "exit_code", "detail", ...}`, on failure too; stdout is one status
line, or for `pending` the marker JSON when a handoff waits for this host and nothing else.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sports_betting.archive import converge
from sports_betting.archive.events import DATASET as EVENTS_DATASET
from sports_betting.archive.events import NATURAL_KEY as EVENTS_KEY
from sports_betting.archive.events import EventArchive
from sports_betting.archive.odds import DATASET as ODDS_DATASET
from sports_betting.archive.odds import NATURAL_KEY as ODDS_KEY
from sports_betting.archive.odds import OddsArchive
from sports_betting.historical import BULK_DATASETS

REPO_ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = REPO_ROOT / "logs" / "handoff.json"

PROJECT = "sports_betting"
COLLECTOR = "collector"
HANDOFF_PREFIX = f"handoff/{PROJECT}"
MARKER_KEY = f"{HANDOFF_PREFIX}/marker.json"
MANIFEST_KEY = f"{HANDOFF_PREFIX}/manifest.json"
STATE_PREFIX = f"{HANDOFF_PREFIX}/state"
PENDING, IMPORTED = "pending", "imported"
LAKE_INSTALL = "uv pip install -e ../data-lake[archive]"

EXIT_OK, EXIT_FAILED, EXIT_CANNOT_TELL = 0, 1, 2
QUICK_TIMEOUT = 120.0


class HandoffError(Exception):
    """A step failed; the message becomes the artifact's `detail`."""


class CannotTell(HandoffError):
    """The store could not be read, so whether a handoff is pending is unknown."""


def project_scope() -> converge.Scope:
    """The datasets this project writes: the two union-written ones and every bulk one."""
    return converge.Scope(
        merged={
            ODDS_DATASET: converge.MergedDataset(OddsArchive, ODDS_KEY),
            EVENTS_DATASET: converge.MergedDataset(EventArchive, EVENTS_KEY),
        },
        versioned=BULK_DATASETS,
    )


def state_fields() -> tuple[str, ...]:
    """The `Settings` fields naming a state file the scheduler reads back on start.

    Every `*_file` path: the two refresh logs, the provider quota ledger and the job health
    registry. Without them a new machine would re-spend a month's paid credits and report
    every job as never run. Run reports (`logs/archive-ops.json`, this artifact) are not
    state and do not travel.
    """
    from sports_betting.config import Settings

    fields = Settings.model_fields.items()
    return tuple(sorted(n for n, f in fields if f.annotation is Path and n.endswith("_file")))


@dataclass(frozen=True)
class Checkout:
    """Where this machine keeps what a handoff carries."""

    archive_root: Path
    state_files: dict[str, Path]
    backend: str = "s3"


Store = converge.ListingStore


def _absolute(path: Path) -> Path:
    return path if path.is_absolute() else REPO_ROOT / path


def checkout_from_settings(settings: Any) -> Checkout:
    """Paths from `settings`, relative ones read against the checkout as `.env` means them."""
    files = {
        getattr(settings, name).name: _absolute(getattr(settings, name)) for name in state_fields()
    }
    return Checkout(_absolute(settings.archive_root), files, settings.archive_backend)


def store_from_settings(settings: Any) -> Store:
    """The shared store `.env` configures, or CannotTell naming what is missing."""
    if settings.archive_backend != "s3":
        raise CannotTell(
            f"ARCHIVE_BACKEND is {settings.archive_backend!r} in .env; a handoff travels "
            "through the shared S3 store, so set it to 's3' with the ARCHIVE_S3_* keys"
        )
    try:
        from data_lake.archive.store import store_from_settings as lake_store
    except ImportError as exc:
        raise CannotTell(
            f"the sibling lake package is not installed in this checkout's .venv: run "
            f"`{LAKE_INSTALL}` (again after any `uv sync`, which removes it)"
        ) from exc
    try:
        store: Store = lake_store(settings)
    except (ImportError, RuntimeError, ValueError) as exc:
        raise CannotTell(f"cannot build the store client: {exc}") from exc
    return store


def read_marker(store: Store) -> dict[str, Any] | None:
    """The marker, or None when there is none. One GET; CannotTell on anything else."""
    try:
        body = store.get_bytes(MARKER_KEY)
    except KeyError:
        return None
    except Exception as exc:
        raise CannotTell(f"cannot read the marker: {type(exc).__name__}: {exc}") from exc
    try:
        marker = json.loads(body)
    except ValueError as exc:
        raise CannotTell(f"the marker is not JSON: {exc}") from exc
    if not isinstance(marker, dict):
        raise CannotTell("the marker is not a JSON object")
    return marker


def put_verified(store: Store, key: str, data: bytes, what: str) -> str:
    """Upload `data` and read it back; returns its sha256."""
    digest = converge.sha256_bytes(data)
    try:
        store.put_bytes(key, data)
        back = store.get_bytes(key)
    except Exception as exc:
        raise HandoffError(f"uploading {what} failed: {type(exc).__name__}: {exc}") from exc
    if converge.sha256_bytes(back) != digest:
        raise HandoffError(f"{what} did not read back intact from the store")
    return digest


def write_marker(store: Store, marker: dict[str, Any]) -> None:
    put_verified(store, MARKER_KEY, json.dumps(marker, indent=2).encode(), "the marker")


def fetch_verified(store: Store, key: str, sha256: Any, what: str) -> bytes:
    """`key`'s bytes, which must hash to `sha256`."""
    try:
        data = store.get_bytes(key)
    except Exception as exc:
        raise HandoffError(f"downloading {what} failed: {type(exc).__name__}: {exc}") from exc
    if converge.sha256_bytes(data) != sha256:
        raise HandoffError(f"{what} sha256 does not match the marker's")
    return data


def pending_reason(marker: dict[str, Any] | None, host: str) -> str | None:
    """None when `marker` is a handoff waiting for `host`, else why it is not."""
    if marker is None:
        return "no handoff marker"
    if marker.get("state") != PENDING:
        return f"last handoff already {marker.get('state')} by {marker.get('by')}"
    if str(marker.get("to", "")).casefold() != host.casefold():
        return f"handoff addressed to {marker.get('to')}, not {host}"
    return None


def iso_utc(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def stop_collector() -> None:
    """`docker compose stop collector` in this checkout: the archive has one writer."""
    argv = ["docker", "compose", "stop", COLLECTOR]
    try:
        result = subprocess.run(
            argv,
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=QUICK_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HandoffError(f"stopping {COLLECTOR} failed: {exc}") from exc
    if result.returncode != 0:
        tail = " | ".join(result.stderr.strip().splitlines()[-5:])
        raise HandoffError(f"stopping {COLLECTOR} failed (exit {result.returncode}): {tail}")


@dataclass
class Deps:
    """Everything with a side effect, so a test can replace each one."""

    checkout: Callable[[], Checkout]
    store_factory: Callable[[Checkout], Store]
    stop_collector: Callable[[], None] = stop_collector
    now: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))


def _converging(step: str, action: Callable[[], Any]) -> Any:
    try:
        return action()
    except HandoffError:
        raise
    except Exception as exc:
        raise HandoffError(f"{step} failed: {type(exc).__name__}: {exc}") from exc


def upload_state(store: Store, checkout: Checkout) -> dict[str, dict[str, Any]]:
    """Each state file that exists here, uploaded and read back: `{name: {key, sha256, bytes}}`."""
    state: dict[str, dict[str, Any]] = {}
    for name, path in sorted(checkout.state_files.items()):
        if not path.is_file():
            continue
        data = path.read_bytes()
        key = f"{STATE_PREFIX}/{name}"
        state[name] = {
            "key": key,
            "sha256": put_verified(store, key, data, name),
            "bytes": len(data),
        }
    return state


def export(deps: Deps, from_host: str, to_host: str) -> dict[str, Any]:
    """Converge, upload state and manifest, then -- only then -- address the marker."""
    if from_host.casefold() == to_host.casefold():
        raise HandoffError(f"--from and --to are the same host ({from_host})")
    checkout = deps.checkout()
    store = deps.store_factory(checkout)
    scope = project_scope()
    root = checkout.archive_root
    pulled = _converging("pulling the store", lambda: converge.pull(root, store, scope))
    pushed = _converging(
        "pushing to the store",
        lambda: converge.push(root, store, scope, backend=checkout.backend),
    )
    objects = _converging("listing the archive", lambda: converge.inventory(root, scope))
    state = upload_state(store, checkout)
    at = iso_utc(deps.now())
    manifest = {"project": PROJECT, "from": from_host, "to": to_host, "at": at, "objects": objects}
    body = json.dumps(manifest, indent=2, sort_keys=True).encode()
    marker = {
        "state": PENDING,
        "project": PROJECT,
        "from": from_host,
        "to": to_host,
        "at": at,
        "key": MANIFEST_KEY,
        "sha256": put_verified(store, MANIFEST_KEY, body, "the manifest"),
        "bytes": len(body),
        "objects": len(objects),
        "data_bytes": sum(int(entry.get("bytes", 0)) for entry in objects.values()),
        "state_files": state,
    }
    write_marker(store, marker)
    return {"marker": marker, "pulled": pulled.as_dict(), "pushed": pushed}


def load_manifest(store: Store, marker: dict[str, Any]) -> dict[str, dict[str, Any]]:
    body = fetch_verified(
        store, str(marker.get("key") or MANIFEST_KEY), marker.get("sha256"), "the manifest"
    )
    try:
        objects = json.loads(body)["objects"]
    except (ValueError, KeyError, TypeError) as exc:
        raise HandoffError(f"the manifest has no object list: {exc}") from exc
    if not isinstance(objects, dict):
        raise HandoffError("the manifest's object list is not a mapping")
    return objects


def fetch_state(store: Store, marker: dict[str, Any], checkout: Checkout) -> dict[Path, bytes]:
    """Each state file the marker lists, verified, keyed by where it goes on this machine.

    Only names this checkout knows are written: the marker says which files, never where.
    """
    restored: dict[Path, bytes] = {}
    listed = marker.get("state_files") or {}
    for name, entry in sorted(listed.items()):
        target = checkout.state_files.get(name)
        if target is None or not isinstance(entry, dict):
            raise HandoffError(
                f"the marker carries a state file this checkout does not know: {name}"
            )
        restored[target] = fetch_verified(store, str(entry.get("key")), entry.get("sha256"), name)
    return restored


def import_handoff(deps: Deps, host: str) -> dict[str, Any]:
    """Pull and restore the handoff addressed to `host`, then mark it imported."""
    checkout = deps.checkout()
    store = deps.store_factory(checkout)
    marker = read_marker(store)
    reason = pending_reason(marker, host)
    if marker is None or reason is not None:
        raise HandoffError(f"nothing to import: {reason}")
    objects = load_manifest(store, marker)
    state = fetch_state(store, marker, checkout)
    deps.stop_collector()
    expected = {k: str(e["sha256"]) for k, e in objects.items() if e.get("sha256")}
    remote_only = {k for k, e in objects.items() if e.get("remote_only")}
    root = checkout.archive_root
    scope = project_scope()
    report = _converging(
        "pulling the archive",
        lambda: converge.pull(root, store, scope, expected=expected, remote_only=remote_only),
    )
    problems = converge.verify(root, objects, report)
    if problems:
        raise HandoffError(f"{len(problems)} object(s) did not arrive, first: {problems[0]}")
    for target, data in state.items():
        converge.write_atomic(target, data)
    updated = {**marker, "state": IMPORTED, "by": host, "imported_at": iso_utc(deps.now())}
    write_marker(store, updated)
    restored = sorted(path.name for path in state)
    return {"marker": updated, "pulled": report.as_dict(), "state_restored": restored}


def write_artifact(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")


def run(args: argparse.Namespace, deps: Deps) -> tuple[int, dict[str, Any], str]:
    """One subcommand: (exit code, artifact payload, stdout line or "")."""
    command = args.command
    try:
        if command == "pending":
            checkout = deps.checkout()
            marker = read_marker(deps.store_factory(checkout))
            reason = pending_reason(marker, args.host)
            line = json.dumps(marker) if reason is None else ""
            detail = reason or f"handoff pending for {args.host}"
            payload = {"ok": True, "pending": reason is None, "marker": marker, "detail": detail}
            return EXIT_OK, payload, line
        if command == "export":
            result = export(deps, args.from_host, args.to_host)
            marker = result["marker"]
            detail = f"exported {marker['objects']} objects ({marker['data_bytes']} bytes) for {args.to_host}"
            return EXIT_OK, {"ok": True, "detail": detail, **result}, f"handoff: {detail}"
        result = import_handoff(deps, args.host)
        pulled = result["pulled"]
        detail = (
            f"imported from {result['marker'].get('from')}: fetched {pulled['fetched']}, "
            f"merged into {len(pulled['merged'])}, kept {pulled['kept']}, "
            f"{len(result['state_restored'])} state file(s)"
        )
        return EXIT_OK, {"ok": True, "detail": detail, **result}, f"handoff: {detail}"
    except HandoffError as exc:
        unknown = command == "pending" and isinstance(exc, CannotTell)
        code = EXIT_CANNOT_TELL if unknown else EXIT_FAILED
        return code, {"ok": False, "detail": str(exc)}, f"handoff {command} FAILED: {exc}"


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    exp = sub.add_parser("export", help="converge with the store, mark the handoff for --to")
    exp.add_argument("--from", dest="from_host", required=True)
    exp.add_argument("--to", dest="to_host", required=True)
    pend = sub.add_parser("pending", help="print the marker if a handoff waits for --host")
    pend.add_argument("--host", required=True)
    imp = sub.add_parser("import", help="pull and restore the handoff addressed to --host")
    imp.add_argument("--host", required=True)
    return parser.parse_args(argv)


def _settings() -> Any:
    from sports_betting.config import Settings

    try:
        # pydantic-settings ranks the process environment above this file, as elsewhere.
        return Settings(_env_file=REPO_ROOT / ".env")  # type: ignore[call-arg]  # runtime-only init arg
    except ValueError as exc:
        raise CannotTell(f".env does not configure the store: {exc}") from exc


def default_deps() -> Deps:
    return Deps(
        checkout=lambda: checkout_from_settings(_settings()),
        store_factory=lambda _checkout: store_from_settings(_settings()),
    )


def main(
    argv: list[str] | None = None,
    deps: Deps | None = None,
    artifact: Path = ARTIFACT,
) -> int:
    args = parse_args(argv)
    deps = deps or default_deps()
    head = {"command": args.command, **{k: v for k, v in vars(args).items() if k != "command"}}
    try:
        code, payload, line = run(args, deps)
    except Exception as exc:
        # A bug, not a reported failure: leave the artifact, then let the traceback out --
        # except from `pending`, whose caller reads any exit but 0 and 1 as "cannot tell".
        code = EXIT_CANNOT_TELL if args.command == "pending" else EXIT_FAILED
        detail = f"{type(exc).__name__}: {exc}"
        write_artifact(artifact, {**head, "exit_code": code, "ok": False, "detail": detail})
        if code == EXIT_CANNOT_TELL:
            print(f"handoff pending FAILED: {detail} ({artifact})", file=sys.stderr)
            raise SystemExit(code) from exc
        raise
    write_artifact(artifact, {**head, "exit_code": code, **payload})
    if code == EXIT_OK:
        if line:
            print(line)
    elif args.command == "pending":
        print(f"{line} ({artifact})", file=sys.stderr)
    else:
        print(f"{line} ({artifact})")
    return code


if __name__ == "__main__":
    sys.exit(main())
