"""Idempotent Hive-partitioned Parquet storage for sports event snapshots."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from sports_betting.archive.manifest import CATALOG_DIRNAME, build_manifest, partition_entry
from sports_betting.providers.thesportsdb import EventSnapshot

DATASET = "sports_events"
NATURAL_KEY = ("source", "external_id", "payload_hash")
_SAFE_PART = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class WriteResult:
    snapshots_received: int
    snapshots_added: int
    partitions: tuple[str, ...]


def _slug(value: str) -> str:
    slug = _SAFE_PART.sub("-", value.lower()).strip("-")
    return slug or "unknown"


def _key(record: dict[str, Any]) -> tuple[str, str, str]:
    return tuple(str(record[column]) for column in NATURAL_KEY)  # type: ignore[return-value]


class EventArchive:
    """Local archive rooted in the sibling data-lake checkout by default.

    Rows are content-addressed observations. Re-fetching an unchanged event is a no-op;
    a scheduled event becoming final creates a second immutable snapshot.
    """

    def __init__(self, root: Path | str):
        self.root = Path(root)

    def write(self, snapshots: list[EventSnapshot]) -> WriteResult:
        grouped: dict[Path, list[dict[str, Any]]] = {}
        for snapshot in snapshots:
            day = snapshot.event_ts.astimezone(UTC).date().isoformat()
            relative = Path(
                DATASET,
                f"source={_slug(snapshot.source)}",
                f"sport={_slug(snapshot.sport)}",
                f"event_date={day}",
                "events.parquet",
            )
            grouped.setdefault(relative, []).append(snapshot.as_record())

        added = 0
        written: list[str] = []
        for relative, incoming in sorted(grouped.items(), key=lambda pair: str(pair[0])):
            path = self._safe_path(relative)
            existing = pq.read_table(path).to_pylist() if path.is_file() else []
            merged = {_key(row): row for row in existing}
            before = len(merged)
            for row in incoming:
                merged.setdefault(_key(row), row)
            rows = sorted(merged.values(), key=_key)
            self._write_verified(path, rows)
            added += len(merged) - before
            written.append(relative.as_posix())

        if written:
            self._write_catalog()
        return WriteResult(len(snapshots), added, tuple(written))

    def rebuild_catalog(self) -> None:
        """Recompute the manifest from the stored partitions. Used by the shape migration."""
        self._write_catalog()

    def _safe_path(self, relative: Path) -> Path:
        root = self.root.resolve()
        path = (root / relative).resolve()
        if not path.is_relative_to(root):
            raise ValueError("archive path escapes ARCHIVE_ROOT")
        return path

    def _write_verified(self, path: Path, rows: list[dict[str, Any]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".parquet.tmp")
        table = pa.Table.from_pylist(rows)
        pq.write_table(table, temporary, compression="zstd")
        stored = pq.read_table(temporary).to_pylist()
        if {_key(row) for row in stored} != {_key(row) for row in rows}:
            temporary.unlink(missing_ok=True)
            raise RuntimeError(f"archive verification failed for {path}")
        temporary.replace(path)

    def _write_catalog(self) -> None:
        dataset_root = self._safe_path(Path(DATASET))
        now = datetime.now(UTC)
        stamp = now.isoformat()
        partitions: dict[str, dict[str, Any]] = {}
        schema: dict[str, str] = {}
        for path in sorted(dataset_root.rglob("*.parquet")):
            table = pq.read_table(path)
            if not schema:
                schema = {field.name: str(field.type) for field in table.schema}
            timestamps = [value for value in table.column("event_ts").to_pylist() if value]
            key = path.relative_to(self.root.resolve()).as_posix()
            partitions[key] = partition_entry(
                rows=table.num_rows,
                min_ts=min(timestamps).isoformat(),
                max_ts=max(timestamps).isoformat(),
                updated_at=stamp,
            )

        manifest = build_manifest(
            dataset=DATASET,
            ts_column="event_ts",
            key_columns=NATURAL_KEY,
            schema=schema,
            partitions=partitions,
            updated_at=now,
        )
        path = self._safe_path(Path(CATALOG_DIRNAME, f"{DATASET}.json"))
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
        json.loads(temporary.read_text(encoding="utf-8"))
        temporary.replace(path)
