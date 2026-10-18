"""Bring this machine's bronze tree and the shared store to the same contents, losing no row.

`archive-sync` only uploads what the store lacks and `archive-restore` only re-fetches pruned
sources, so neither can put a dataset onto a machine that does not hold it. And `sync` never
re-uploads a key the store already has, while the collector rewrites its odds and events
partitions on every run, so the store's copy of a busy partition is whatever was sent first.
This module is the missing direction (`pull`) and the missing refresh (`push`), for
`scripts/handoff.py`, which moves the collector between machines.

## The merge rule -- why a pull cannot clobber

Each key under a carried dataset is one of three kinds (`Scope.kind`):

- **merged** -- a partition of a dataset the collector writes by union (`sports_odds`,
  `sports_events`): `<dataset>/.../event_date=<day>/<file>.parquet`, rewritten in place. Two
  machines that each collected for a while hold different, each-partial copies of one key.
  The store's copy is never written over the local one: its rows go through the dataset's
  own `merge_rows`, keyed by `(source, external_id, payload_hash)` -- one exact observation
  per key -- so the result is the union, and no local row is dropped or changed.
- **versioned** -- a bulk artifact: `<dataset>/.../version=<source sha>/`, a directory
  named by the publisher's bytes. A local file is **never overwritten**: a missing one is
  fetched, a present one stays. Two copies of one key carry the same source, so the only
  legitimate differences are `data.parquet`'s writer metadata (another pyarrow) and
  `provenance.json`'s prune flags -- neither worth a byte of the local copy. A `source-*`
  whose provenance says it was pruned to the store stays there (`archive-restore` fetches
  it on demand), and a fetched file is checked against its own provenance, not against
  another machine's bytes.
- **catalog** -- `_catalog/<dataset>.json`, derived from the partitions. Never pulled: it is
  rebuilt here from the merged tree, so it describes what is on this disk.

Nothing outside the carried datasets -- `_staging`, the other projects' datasets in the
pooled tree, their `_catalog/` entries -- is listed, fetched, uploaded or written.

Every file lands through a temp file and a rename, so an interrupted pull leaves the old
file or the new one, never half of one, and the next pull carries on where it stopped.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import pyarrow.parquet as pq

from sports_betting.archive.bulk import BulkArchive
from sports_betting.archive.manifest import CATALOG_DIRNAME
from sports_betting.archive.sync import (
    EXCLUDED_DIRS,
    ArchiveSync,
    ObjectStore,
    iter_pruned,
    scan,
    sha256_file,
)

MERGED, VERSIONED, CATALOG = "merged", "versioned", "catalog"
PROVENANCE = "provenance.json"
_SOURCE_PREFIX = "source-"


class ConvergeError(RuntimeError):
    """The tree and the store could not be brought together; the message says where."""


class Listed(Protocol):
    """One entry of `list_objects`: `data_lake.archive.store.StoredObject` satisfies it."""

    @property
    def key(self) -> str: ...

    @property
    def size(self) -> int: ...


class ListingStore(ObjectStore, Protocol):
    """The store `sync` uses, plus the listing a pull needs."""

    def list_objects(self, prefix: str = "") -> list[Any]: ...


class Merger(Protocol):
    """A dataset written by union: `OddsArchive` and `EventArchive`."""

    def merge_rows(self, relative: Path, incoming: list[dict[str, Any]]) -> int: ...

    def rebuild_catalog(self) -> None: ...


@dataclass(frozen=True)
class MergedDataset:
    """How to open a union-written dataset at a root, and the columns its rows are keyed by."""

    archive: Callable[[Path], Merger]
    key: tuple[str, ...]


@dataclass(frozen=True)
class Scope:
    """The datasets a handoff carries, by how each is written."""

    merged: Mapping[str, MergedDataset]
    versioned: frozenset[str]

    @property
    def datasets(self) -> frozenset[str]:
        return frozenset(self.merged) | self.versioned

    def kind(self, key: str) -> str | None:
        """`MERGED`, `VERSIONED`, `CATALOG`, or None for a key this scope does not own."""
        parts = Path(key).parts
        if not parts or EXCLUDED_DIRS.intersection(parts) or key.endswith((".tmp", ".part")):
            return None
        if len(parts) == 2 and parts[0] == CATALOG_DIRNAME:
            return CATALOG if parts[1].removesuffix(".json") in self.datasets else None
        if len(parts) < 2 or parts[0] not in self.datasets:
            return None
        if parts[0] in self.merged and key.endswith(".parquet"):
            return MERGED
        return VERSIONED


@dataclass
class PullReport:
    fetched: list[str] = field(default_factory=list)
    merged: dict[str, int] = field(default_factory=dict)
    kept: int = 0
    remote_only: list[str] = field(default_factory=list)
    checked: set[str] = field(default_factory=set)
    catalogs: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "fetched": len(self.fetched),
            "merged": self.merged,
            "kept": self.kept,
            "remote_only": len(self.remote_only),
            "catalogs_rebuilt": self.catalogs,
            "fetched_keys": self.fetched,
        }


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_atomic(path: Path, data: bytes) -> None:
    """`data` at `path` through a temp file and a rename: all of it or none of it.

    The temp name ends `.tmp`, which `sync.scan` never uploads, and it is removed on any
    failure -- an interrupt included -- so nothing half-written is left beside the target.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_bytes(data)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _target(base: Path, key: str) -> Path:
    path = (base / key).resolve()
    if not path.is_relative_to(base):
        raise ConvergeError(f"store key {key!r} escapes ARCHIVE_ROOT")
    return path


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _pull_order(listed: Listed) -> tuple[str, int, str]:
    """Each version directory's provenance first: it is what its other files are checked by."""
    path = Path(listed.key)
    return (path.parent.as_posix(), 0 if path.name == PROVENANCE else 1, path.name)


def listing(store: ListingStore, scope: Scope) -> list[Listed]:
    """Every store object under the scope's dataset directories, in pull order."""
    objects: list[Listed] = []
    for dataset in sorted(scope.datasets):
        objects += [o for o in store.list_objects(f"{dataset}/") if scope.kind(o.key)]
    return sorted(objects, key=_pull_order)


def _fetch(store: ListingStore, listed: Listed, expected: Mapping[str, str]) -> bytes:
    data = store.get_bytes(listed.key)
    if len(data) != listed.size:
        raise ConvergeError(f"{listed.key}: read {len(data)} bytes, the store lists {listed.size}")
    want = expected.get(listed.key)
    if want and sha256_bytes(data) != want:
        raise ConvergeError(
            f"{listed.key}: the store holds sha256 {sha256_bytes(data)[:16]}, "
            f"the export recorded {want[:16]}"
        )
    return data


def check_versioned(target: Path, data: bytes) -> None:
    """A fetched bulk file against the provenance beside it, or raise.

    The provenance is the content address: a source must hash to `source_sha256` and the
    Parquet must hold `rows` rows, whichever machine wrote the copy in the store.
    """
    provenance = _read_json(target.parent / PROVENANCE)
    if target.name == PROVENANCE:
        if not _parses_as_object(data):
            raise ConvergeError(f"{target.name} in {target.parent.name} is not a JSON object")
        return
    if target.name.startswith(_SOURCE_PREFIX) and provenance.get("source_sha256"):
        if sha256_bytes(data) != provenance["source_sha256"]:
            raise ConvergeError(f"{target}: sha256 does not match its provenance")
    if target.suffix == ".parquet" and isinstance(provenance.get("rows"), int):
        rows = pq.ParquetFile(io.BytesIO(data)).metadata.num_rows
        if rows != provenance["rows"]:
            raise ConvergeError(f"{target}: {rows} rows, its provenance says {provenance['rows']}")


def _parses_as_object(data: bytes) -> bool:
    try:
        return isinstance(json.loads(data), dict)
    except ValueError:
        return False


def _pruned_here(target: Path) -> bool:
    """A source whose provenance on this disk says it lives only in the store now."""
    return target.name.startswith(_SOURCE_PREFIX) and bool(
        _read_json(target.parent / PROVENANCE).get("source_pruned")
    )


def pull(
    root: Path | str,
    store: ListingStore,
    scope: Scope,
    *,
    expected: Mapping[str, str] | None = None,
    remote_only: Collection[str] = (),
) -> PullReport:
    """Bring every scoped store object onto this disk under the merge rule (module doc).

    `expected` maps a key to the sha256 its store copy must have (an export's record);
    `remote_only` names pruned sources to leave in the store. Catalogs of every dataset the
    pull changed, or that has data here and no catalog, are rebuilt at the end.
    """
    base = Path(root).resolve()
    want = expected or {}
    report = PullReport()
    touched: set[str] = set()
    for listed in listing(store, scope):
        dataset = Path(listed.key).parts[0]
        target = _target(base, listed.key)
        if scope.kind(listed.key) == MERGED:
            data = _fetch(store, listed, want)
            report.checked.add(listed.key)
            rows = pq.read_table(io.BytesIO(data)).to_pylist()
            added = scope.merged[dataset].archive(base).merge_rows(Path(listed.key), rows)
            if added:
                report.merged[listed.key] = added
                touched.add(dataset)
            else:
                report.kept += 1
        elif target.is_file():
            report.kept += 1
        elif listed.key in remote_only or _pruned_here(target):
            report.remote_only.append(listed.key)
        else:
            data = _fetch(store, listed, want)
            report.checked.add(listed.key)
            check_versioned(target, data)
            write_atomic(target, data)
            report.fetched.append(listed.key)
            touched.add(dataset)
    uncatalogued = {
        dataset
        for dataset in scope.datasets
        if (base / dataset).is_dir() and not (base / CATALOG_DIRNAME / f"{dataset}.json").is_file()
    }
    report.catalogs = rebuild_catalogs(base, scope, touched | uncatalogued)
    return report


def rebuild_catalogs(root: Path | str, scope: Scope, datasets: Collection[str]) -> list[str]:
    """Rewrite the catalog of each of `datasets` that has data here; returns those rebuilt."""
    base = Path(root).resolve()
    rebuilt: list[str] = []
    for dataset in sorted(datasets):
        if dataset in scope.merged:
            if not any((base / dataset).rglob("*.parquet")):
                continue
            scope.merged[dataset].archive(base).rebuild_catalog()
        elif any((base / dataset).rglob(PROVENANCE)):
            BulkArchive(base).rebuild_catalog(dataset)
        else:
            continue
        rebuilt.append(dataset)
    return rebuilt


def _assert_superset(stored: bytes, local: Path, key: str, columns: tuple[str, ...]) -> None:
    def keys(table: Any) -> set[tuple[str, ...]]:
        return {tuple(str(row[c]) for c in columns) for row in table.to_pylist()}

    missing = keys(pq.read_table(io.BytesIO(stored))) - keys(pq.read_table(local))
    if missing:
        raise ConvergeError(
            f"{key}: the store holds {len(missing)} row(s) this disk lacks, so uploading "
            "would drop them; something wrote to the store after the pull -- run it again"
        )


def push(root: Path | str, store: ListingStore, scope: Scope, *, backend: str) -> dict[str, Any]:
    """Make the store hold this disk's copy of every scoped key, verified by reading back.

    `ArchiveSync` sends what the store lacks. The merged partitions and catalogs it skips as
    already present are then compared byte for byte and replaced where they differ -- a
    partition only when the store's rows are a subset of this disk's, so the store loses
    nothing either. Run it after `pull`, which is what makes that subset hold.
    """
    base = Path(root).resolve()
    summary = ArchiveSync(base, store, backend=backend, datasets=scope.datasets).run()
    if not summary.ok:
        first = summary.failures[0]
        raise ConvergeError(
            f"archive-sync: {len(summary.failures)} failed, first {first.key}: {first.detail}"
        )
    refreshed: list[str] = []
    for candidate in scan(base, scope.datasets):
        kind = scope.kind(candidate.key)
        if kind not in (MERGED, CATALOG):
            continue
        local = candidate.path.read_bytes()
        try:
            stored: bytes | None = store.get_bytes(candidate.key)
        except KeyError:
            stored = None
        if stored is not None and sha256_bytes(stored) == sha256_bytes(local):
            continue
        if kind == MERGED and stored is not None:
            columns = scope.merged[candidate.dataset].key
            _assert_superset(stored, candidate.path, candidate.key, columns)
        store.put_bytes(candidate.key, local)
        if sha256_bytes(store.get_bytes(candidate.key)) != sha256_bytes(local):
            raise ConvergeError(f"{candidate.key}: the store's copy does not read back intact")
        refreshed.append(candidate.key)
    return {
        "uploaded": summary.uploaded,
        "present": summary.already_present,
        "refreshed": refreshed,
    }


def inventory(root: Path | str, scope: Scope) -> dict[str, dict[str, Any]]:
    """What a machine taking this tree over must end up holding, by key.

    Merged partitions and catalogs carry the sha256 that `push` made the store hold too, so
    a pull can check the store's copy against it. A versioned file carries only its size: it
    is checked by its provenance instead, since the store's copy may be another machine's
    byte-different twin. Sources pruned to the store are listed `remote_only`.
    """
    base = Path(root).resolve()
    objects: dict[str, dict[str, Any]] = {}
    for candidate in scan(base, scope.datasets):
        kind = scope.kind(candidate.key)
        if kind is None:
            continue
        entry: dict[str, Any] = {"kind": kind, "bytes": candidate.size}
        if kind != VERSIONED:
            entry["sha256"] = sha256_file(candidate.path)
        objects[candidate.key] = entry
    for _provenance, payload in iter_pruned(base):
        key = str(payload.get("source_remote_key") or payload.get("source_file", ""))
        if scope.kind(key) == VERSIONED and key not in objects:
            objects[key] = {"kind": VERSIONED, "remote_only": True}
    return objects


def verify(
    root: Path | str, objects: Mapping[str, Mapping[str, Any]], report: PullReport
) -> list[str]:
    """Every way this disk falls short of an export's `inventory`, after `pull`; [] when none."""
    base = Path(root).resolve()
    problems: list[str] = []
    for key, entry in sorted(objects.items()):
        if entry.get("remote_only"):
            continue
        if not (base / key).is_file():
            problems.append(f"{key}: not on this disk after the pull")
        elif entry.get("kind") == MERGED and key not in report.checked:
            problems.append(f"{key}: the store does not hold the partition the export recorded")
    return problems
