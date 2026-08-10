"""Mirror the local bronze tree into the shared object store, then reclaim local disk.

Three phases, always in this order and never independently:

1. **upload** every local object the mirror does not already hold;
2. **verify** by reading the uploaded object *back out of the store* and matching it against
   the local file — sha256 for every object, and additionally the Parquet row count for
   Parquet, because that is what `.claude/rules/data-lake.md` requires before a delete;
3. **prune** the local copy — and only the copy whose own verification passed.

The ordering is the whole safety property. "Uploaded" is not "durable": a put that returned
200 can still have stored a truncated object, and the only way to know is to read it back.
Nothing here deletes a local byte that has not been round-tripped in the same run.

What gets pruned is deliberately narrow: **source artifacts only** (`source-*`, the pristine
publisher download). The derived `data.parquet` stays local because it is the queryable copy
`BulkArchive._write_catalog` re-opens on every subsequent import — pruning it would make the
next bulk import fail rather than save anything durable. This also happens to be where the
disk actually goes: for StatsBomb the source JSON is ~12 GB against ~2 GB of Parquet.

The lake package is imported lazily. It is not a declared dependency at all — it lives in a
sibling checkout that CI and the container image do not have, and `pyproject.toml` explains
at length why declaring it breaks `uv sync` for both. Importing it at module scope would turn
its absence into a collection-time crash in the one place it is never needed.
"""

from __future__ import annotations

import hashlib
import io
import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import pyarrow.parquet as pq

from sports_betting.archive.manifest import CATALOG_DIRNAME

#: Whole-object ceiling. The `ObjectStore` seam is bytes-in/bytes-out, so mirroring one file
#: peaks at roughly **twice** its size: once for the PUT body, once for the verification
#: read-back. The collector container is capped at 512 MB (`docker-compose.yml`), so refusing
#: an oversize object with a named reason beats an OOM kill that reports nothing.
#:
#: This is not a placeholder for a streaming API that is coming. A streaming `put_file` was
#: considered upstream and declined, correctly: it would widen a Protocol every consumer and
#: test fake implements, while removing only one of the copies its *own* writers make (see
#: `data_lake.archive.parquet_io.merge_into_partition`, which decodes, concats, dedupes and
#: re-serializes a whole partition). This mirror never goes through that path — it uploads
#: files already on disk — but its objects are small: the largest artifact any configured
#: source produces is ~20 MB (nflverse play-by-play, MoneyPuck's season zip), so the ceiling
#: is bounding a case that does not currently arise. Revisit only if a source starts emitting
#: objects near this size, not on general principle.
DEFAULT_MAX_OBJECT_BYTES = 256 * 1024 * 1024

#: Directories that never mirror. `_staging` holds partial downloads and their resume
#: metadata — transient by construction, and uploading a `.part` would publish a truncated
#: artifact under a name that looks complete.
EXCLUDED_DIRS = frozenset({"_staging"})

#: Datasets whose *source* artifact is read back by a later run, so pruning it breaks a
#: resume rather than saving space. StatsBomb re-reads the matches list to enumerate match
#: ids and the competitions list to enumerate targets; both are small (7 MB total against
#: 12 GB of events), so exempting them costs nothing and removes a whole class of failure.
SOURCE_READBACK_DATASETS = frozenset({"statsbomb_matches", "statsbomb_competitions"})

_SOURCE_PREFIX = "source-"


class ObjectStore(Protocol):
    """The slice of `data_lake.archive.store.ObjectStore` this module uses.

    Declared locally so the type-checker and the tests need no sibling checkout — the real
    store satisfies it structurally, exactly as this project's `Settings` satisfies the
    lake's `ArchiveSettings`.
    """

    def put_bytes(self, key: str, data: bytes) -> None: ...

    def get_bytes(self, key: str) -> bytes: ...

    def exists(self, key: str) -> bool: ...


@dataclass(frozen=True)
class Candidate:
    """One local file considered for mirroring."""

    key: str
    path: Path
    size: int
    dataset: str
    is_source: bool
    is_parquet: bool

    @property
    def prunable(self) -> bool:
        return self.is_source and self.dataset not in SOURCE_READBACK_DATASETS


@dataclass(frozen=True)
class Outcome:
    key: str
    status: str
    detail: str = ""
    freed_bytes: int = 0


@dataclass
class SyncSummary:
    backend: str
    dry_run: bool
    scanned: int = 0
    uploaded: int = 0
    already_present: int = 0
    verified: int = 0
    pruned: int = 0
    planned: int = 0
    freed_bytes: int = 0
    outcomes: list[Outcome] = field(default_factory=list)

    @property
    def failures(self) -> list[Outcome]:
        return [o for o in self.outcomes if o.status in {"failed", "oversize"}]

    @property
    def ok(self) -> bool:
        return not self.failures

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "backend": self.backend,
            "dry_run": self.dry_run,
            "scanned": self.scanned,
            "uploaded": self.uploaded,
            "already_present": self.already_present,
            "verified": self.verified,
            "pruned": self.pruned,
            "planned": self.planned,
            "freed_bytes": self.freed_bytes,
            "failures": [
                {"key": o.key, "status": o.status, "detail": o.detail} for o in self.failures
            ],
            "outcomes": [
                {"key": o.key, "status": o.status, "detail": o.detail, "freed": o.freed_bytes}
                for o in self.outcomes
            ],
        }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _dataset_of(relative: Path) -> str:
    parts = relative.parts
    return parts[0] if parts else ""


def scan(root: Path | str) -> list[Candidate]:
    """Every mirrorable file under `root`, deepest-first-stable and deterministic.

    Pure and filesystem-only so the planning half can be tested without a store.
    """
    base = Path(root).resolve()
    if not base.is_dir():
        return []
    candidates: list[Candidate] = []
    for path in sorted(base.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(base)
        if EXCLUDED_DIRS.intersection(relative.parts):
            continue
        if path.name.endswith((".tmp", ".part")):
            continue
        dataset = _dataset_of(relative)
        candidates.append(
            Candidate(
                key=relative.as_posix(),
                path=path,
                size=path.stat().st_size,
                dataset="" if dataset == CATALOG_DIRNAME else dataset,
                is_source=path.name.startswith(_SOURCE_PREFIX),
                is_parquet=path.suffix == ".parquet",
            )
        )
    return candidates


def _parquet_rows(data: bytes) -> int:
    return pq.ParquetFile(io.BytesIO(data)).metadata.num_rows


class ArchiveSync:
    """Upload → verify → prune over one archive root and one object store."""

    def __init__(
        self,
        root: Path | str,
        store: ObjectStore,
        *,
        backend: str = "unknown",
        max_object_bytes: int = DEFAULT_MAX_OBJECT_BYTES,
    ):
        self.root = Path(root).resolve()
        self.store = store
        self.backend = backend
        self.max_object_bytes = max_object_bytes

    def run(self, *, prune: bool = False, dry_run: bool = False) -> SyncSummary:
        summary = SyncSummary(backend=self.backend, dry_run=dry_run)
        pruned_provenance: set[Path] = set()
        pruned_by_dataset: dict[str, set[str]] = {}
        for candidate in scan(self.root):
            summary.scanned += 1
            outcome = self._process(candidate, prune=prune, dry_run=dry_run)
            summary.outcomes.append(outcome)
            if outcome.status == "uploaded":
                summary.uploaded += 1
                summary.verified += 1
            elif outcome.status == "present":
                summary.already_present += 1
            elif outcome.status in {"would-upload", "would-prune"}:
                summary.planned += 1
            elif outcome.status == "pruned":
                summary.verified += 1
                summary.pruned += 1
                summary.freed_bytes += outcome.freed_bytes
                if outcome.detail == "uploaded":
                    summary.uploaded += 1
                pruned_by_dataset.setdefault(candidate.dataset, set()).add(candidate.key)
                provenance = candidate.path.parent / "provenance.json"
                if provenance.is_file():
                    self._mark_pruned(provenance, candidate)
                    pruned_provenance.add(provenance)

        # The catalog's `artifacts` rows are copies of provenance taken at import time, so
        # after a prune they still advertise a `source_file` that is no longer local. Patch
        # them in place rather than rebuilding: a rebuild re-opens every Parquet in the
        # dataset, which for a StatsBomb-sized import costs more than the whole sync.
        for dataset, keys in sorted(pruned_by_dataset.items()):
            patched = self._patch_catalog(dataset, keys, dry_run=dry_run)
            if patched is not None:
                summary.outcomes.append(Outcome(key=patched, status="refreshed"))

        # Provenance files rewritten by the prune pass were mirrored earlier in this same
        # run, so their uploaded copies are now stale. Re-push them rather than leave the
        # mirror asserting a source is present locally when it is not.
        for provenance in sorted(pruned_provenance):
            key = provenance.relative_to(self.root).as_posix()
            if not dry_run:
                self.store.put_bytes(key, provenance.read_bytes())
            summary.outcomes.append(Outcome(key=key, status="refreshed"))
        return summary

    def _patch_catalog(self, dataset: str, pruned_keys: set[str], *, dry_run: bool) -> str | None:
        """Flag every catalogued artifact whose source this run pruned. Returns the key."""
        path = self.root / CATALOG_DIRNAME / f"{dataset}.json"
        if dry_run or not path.is_file():
            return None
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        changed = False
        for row in manifest.get("artifacts", []):
            if isinstance(row, dict) and str(row.get("source_file", "")) in pruned_keys:
                row["source_pruned"] = True
                row["source_remote_key"] = str(row["source_file"])
                changed = True
        if not changed:
            return None
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
        temporary.replace(path)
        key = path.relative_to(self.root).as_posix()
        self.store.put_bytes(key, path.read_bytes())
        return key

    def _process(self, candidate: Candidate, *, prune: bool, dry_run: bool) -> Outcome:
        if candidate.size > self.max_object_bytes:
            return Outcome(
                key=candidate.key,
                status="oversize",
                detail=(
                    f"{candidate.size} bytes exceeds the {self.max_object_bytes}-byte "
                    "whole-object ceiling; the store seam is bytes-in/bytes-out, so "
                    "mirroring this would peak at about twice its size (PUT body plus "
                    "verification read-back). Raise --max-object-mb if the host has room."
                ),
            )
        want_prune = prune and candidate.prunable
        try:
            present = self.store.exists(candidate.key)
            # An object the mirror already holds is normally left alone — re-reading every
            # artifact on every run would download the whole lake. The exception is a prune:
            # the delete is only safe if *this* run read the object back, so a resumed
            # `--prune` after an interrupted upload must verify before it removes anything.
            if present and not want_prune:
                return Outcome(key=candidate.key, status="present")
            if dry_run:
                return Outcome(
                    key=candidate.key,
                    status="would-prune" if want_prune else "would-upload",
                    detail="present" if present else "absent",
                    freed_bytes=candidate.size if want_prune else 0,
                )
            if not present:
                self.store.put_bytes(candidate.key, candidate.path.read_bytes())
            self._verify(candidate)
        except Exception as exc:
            return Outcome(
                key=candidate.key, status="failed", detail=f"{type(exc).__name__}: {exc}"
            )

        if not want_prune:
            return Outcome(key=candidate.key, status="uploaded")
        size = candidate.size
        candidate.path.unlink()
        return Outcome(
            key=candidate.key,
            status="pruned",
            detail="uploaded" if not present else "already-present",
            freed_bytes=size,
        )

    def _verify(self, candidate: Candidate) -> None:
        """Read the object back and match it, or raise. Never called after a delete."""
        stored = self.store.get_bytes(candidate.key)
        local_digest = sha256_file(candidate.path)
        stored_digest = hashlib.sha256(stored).hexdigest()
        if stored_digest != local_digest:
            raise RuntimeError(
                f"verification failed for {candidate.key}: mirror holds sha256 "
                f"{stored_digest[:16]}, local file is {local_digest[:16]}"
            )
        if candidate.is_parquet:
            with candidate.path.open("rb") as handle:
                expected = pq.ParquetFile(handle).metadata.num_rows
            actual = _parquet_rows(stored)
            if actual != expected:
                raise RuntimeError(
                    f"verification failed for {candidate.key}: mirror holds {actual} rows, "
                    f"local file has {expected}"
                )

    def _mark_pruned(self, provenance: Path, candidate: Candidate) -> None:
        """Record on the artifact that its source now lives only in the mirror."""
        payload = json.loads(provenance.read_text(encoding="utf-8"))
        payload["source_pruned"] = True
        payload["source_remote_key"] = candidate.key
        temporary = provenance.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        temporary.replace(provenance)


def iter_pruned(root: Path | str) -> Iterator[tuple[Path, dict[str, Any]]]:
    """Every provenance record whose source artifact has been pruned to the mirror."""
    base = Path(root).resolve()
    for provenance in sorted(base.rglob("provenance.json")):
        try:
            payload = json.loads(provenance.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(payload, dict) and payload.get("source_pruned"):
            yield provenance, payload


def restore_sources(root: Path | str, store: ObjectStore) -> SyncSummary:
    """Pull every pruned source artifact back out of the mirror and re-verify it.

    Prune without restore is data loss with extra steps, so this is not optional scaffolding:
    it is what makes the prune reversible. Each restored file is checked against the sha256
    the publisher's download originally hashed to, which is recorded in the same provenance
    record — so a corrupted round-trip is caught here rather than by a model months later.
    """
    base = Path(root).resolve()
    summary = SyncSummary(backend="restore", dry_run=False)
    for provenance, payload in iter_pruned(base):
        key = str(payload.get("source_remote_key") or payload.get("source_file", ""))
        target = base / Path(str(payload["source_file"]))
        summary.scanned += 1
        try:
            data = store.get_bytes(key)
            digest = hashlib.sha256(data).hexdigest()
            expected = str(payload.get("source_sha256", ""))
            if expected and digest != expected:
                raise RuntimeError(
                    f"restored object sha256 {digest[:16]} does not match the recorded "
                    f"source hash {expected[:16]}"
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix(target.suffix + ".tmp")
            temporary.write_bytes(data)
            temporary.replace(target)
        except Exception as exc:
            summary.outcomes.append(
                Outcome(key=key, status="failed", detail=f"{type(exc).__name__}: {exc}")
            )
            continue

        payload.pop("source_pruned", None)
        payload.pop("source_remote_key", None)
        provenance.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        summary.verified += 1
        summary.outcomes.append(Outcome(key=key, status="restored"))
    return summary


def store_for(settings: Any) -> ObjectStore:
    """Build the configured object store, or explain precisely what is missing.

    The lake package is imported here rather than at module scope so that `archive.sync` can
    be imported — and its pure planning half tested — in an environment that has no sibling
    checkout, which is every CI run and every container.
    """
    if settings.archive_backend == "none":
        raise RuntimeError(
            "no mirror configured: set ARCHIVE_BACKEND to 's3' (Cloudflare R2) or 'local'. "
            "Until then the archive is local-only and nothing is offloaded."
        )
    try:
        from data_lake.archive.store import store_from_settings
    except ImportError as exc:
        raise RuntimeError(
            "the archive mirror needs the sibling lake package, which is not a declared "
            "dependency because CI and the container image have no sibling checkout. On a "
            "host that has one, install it with: uv pip install -e ../data-lake[archive] "
            "(re-run after any `uv sync`, which prunes what the lock does not name)"
        ) from exc
    return store_from_settings(settings)
