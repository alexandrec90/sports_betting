"""Idempotent Hive-partitioned Parquet storage for odds snapshots."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from sports_betting.providers.odds_api import OddsSnapshot

DATASET = "sports_odds"
NATURAL_KEY = ("source", "external_id", "payload_hash")
_SAFE_PART = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class OddsWriteResult:
    snapshots_received: int
    snapshots_added: int
    partitions: tuple[str, ...]


def _slug(value: str) -> str:
    return _SAFE_PART.sub("-", value.lower()).strip("-") or "unknown"


def _key(record: dict[str, Any]) -> tuple[str, str, str]:
    return tuple(str(record[column]) for column in NATURAL_KEY)  # type: ignore[return-value]


class OddsArchive:
    def __init__(self, root: Path | str):
        self.root = Path(root)

    def write(self, snapshots: list[OddsSnapshot]) -> OddsWriteResult:
        grouped: dict[Path, list[dict[str, Any]]] = {}
        for snapshot in snapshots:
            event_day = snapshot.event_ts.astimezone(UTC).date().isoformat()
            relative = Path(
                DATASET,
                f"source={_slug(snapshot.source)}",
                f"sport={_slug(snapshot.sport)}",
                f"event_date={event_day}",
                "odds.parquet",
            )
            grouped.setdefault(relative, []).append(snapshot.as_record())

        added = 0
        paths: list[str] = []
        for relative, incoming in sorted(grouped.items(), key=lambda pair: str(pair[0])):
            path = self._safe_path(relative)
            existing = pq.read_table(path).to_pylist() if path.is_file() else []
            merged = {_key(row): row for row in existing}
            before = len(merged)
            for row in incoming:
                merged.setdefault(_key(row), row)
            rows = sorted(merged.values(), key=lambda row: (row["event_ts"], *_key(row)))
            self._write_verified(path, rows)
            added += len(merged) - before
            paths.append(relative.as_posix())
        if paths:
            self._write_catalog()
        return OddsWriteResult(len(snapshots), added, tuple(paths))

    def _safe_path(self, relative: Path) -> Path:
        root = self.root.resolve()
        path = (root / relative).resolve()
        if not path.is_relative_to(root):
            raise ValueError("archive path escapes ARCHIVE_ROOT")
        return path

    def _write_verified(self, path: Path, rows: list[dict[str, Any]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".parquet.tmp")
        pq.write_table(pa.Table.from_pylist(rows), temporary, compression="zstd")
        stored = pq.read_table(temporary).to_pylist()
        if {_key(row) for row in stored} != {_key(row) for row in rows}:
            temporary.unlink(missing_ok=True)
            raise RuntimeError(f"archive verification failed for {path}")
        temporary.replace(path)

    def _write_catalog(self) -> None:
        partitions: dict[str, dict[str, Any]] = {}
        schema: dict[str, str] = {}
        for path in sorted(self._safe_path(Path(DATASET)).rglob("*.parquet")):
            table = pq.read_table(path)
            if not schema:
                schema = {field.name: str(field.type) for field in table.schema}
            timestamps = table.column("event_ts").to_pylist()
            key = path.relative_to(self.root.resolve()).as_posix()
            partitions[key] = {
                "rows": table.num_rows,
                "min_ts": min(timestamps).isoformat(),
                "max_ts": max(timestamps).isoformat(),
            }
        manifest = {
            "dataset": DATASET,
            "schema_version": 1,
            "key_columns": list(NATURAL_KEY),
            "timestamp_column": "event_ts",
            "schema": schema,
            "partitions": partitions,
            "updated_at": datetime.now(UTC).isoformat(),
        }
        path = self._safe_path(Path("_catalog", f"{DATASET}.json"))
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
        json.loads(temporary.read_text(encoding="utf-8"))
        temporary.replace(path)
