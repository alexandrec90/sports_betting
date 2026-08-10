"""The `_catalog/<dataset>.json` contract, shared with the sibling data-lake package.

Both projects write manifests into the same `_catalog/` namespace of the same bronze tree,
and until this module existed they wrote *different shapes under the same filenames and the
same `schema_version: 1`*: data-lake emits `ts_column` with per-partition `updated_at`
(`data_lake.archive.catalog.DatasetManifest`), while this project emitted `timestamp_column`
and omitted the partition timestamp. Nothing caught it because nothing had yet pointed the
lake's reader at this tree — `load_catalog` would have raised `KeyError: 'ts_column'` on the
first sports manifest it met.

This module resolves that by **adopting DatasetManifest's key names** rather than retreating
to a private prefix. Adopting is the better half of that choice: a distinct prefix would keep
the two catalogs from colliding but would also keep a foreign consumer from discovering this
project's datasets at all, which is the entire purpose of the pooled `_catalog/` namespace
(`.claude/rules/data-lake.md`, "The catalog is the reuse contract").

`DatasetManifest.from_dict` reads only the keys it knows and ignores the rest, so extra
top-level keys are a supported extension point — `bulk.py` uses one for its `artifacts`
provenance list. What is *not* optional is the required set below; a manifest missing any of
it is unreadable by the lake, so `validate_manifest` is called on every write.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

#: Mirrors `data_lake.archive.catalog.CATALOG_PREFIX` (which is `"_catalog/"`).
CATALOG_DIRNAME = "_catalog"

#: Mirrors `data_lake.archive.catalog.CATALOG_SCHEMA_VERSION`. Bump both together; the two
#: projects share one namespace, so a version that means different things in each is worse
#: than no version at all.
CATALOG_SCHEMA_VERSION = 1

#: Exactly what `DatasetManifest.from_dict` requires at the top level. `dataset`, `ts_column`
#: and `key_columns` are subscripted directly there and raise `KeyError` when absent; the
#: rest are `.get()` with defaults but are meaningless to omit.
REQUIRED_MANIFEST_KEYS = frozenset(
    {"dataset", "schema_version", "ts_column", "key_columns", "schema", "partitions", "updated_at"}
)

#: Exactly what `PartitionEntry.from_dict` requires of each `partitions` value. `updated_at`
#: is the one this project used to omit.
REQUIRED_PARTITION_KEYS = frozenset({"rows", "min_ts", "max_ts", "updated_at"})


def catalog_path(root: Path, dataset: str) -> Path:
    return Path(root, CATALOG_DIRNAME, f"{dataset}.json")


def partition_entry(*, rows: int, min_ts: str, max_ts: str, updated_at: str) -> dict[str, Any]:
    """One `partitions` value in the shape `PartitionEntry.from_dict` expects."""
    return {"rows": rows, "min_ts": min_ts, "max_ts": max_ts, "updated_at": updated_at}


def build_manifest(
    *,
    dataset: str,
    ts_column: str,
    key_columns: tuple[str, ...] | list[str],
    schema: dict[str, str],
    partitions: dict[str, dict[str, Any]],
    updated_at: datetime | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """A validated manifest dict. `extra` carries this project's own additions unchanged."""
    stamp = (updated_at or datetime.now(UTC)).isoformat()
    manifest: dict[str, Any] = {
        "dataset": dataset,
        "schema_version": CATALOG_SCHEMA_VERSION,
        "ts_column": ts_column,
        "key_columns": list(key_columns),
        "schema": dict(schema),
        "partitions": dict(partitions),
        "updated_at": stamp,
    }
    manifest.update(extra)
    validate_manifest(manifest)
    return manifest


def validate_manifest(manifest: dict[str, Any]) -> None:
    """Raise if `manifest` would be unreadable by `DatasetManifest.from_dict`.

    Called on the write path rather than offered as a lint, because an unreadable manifest is
    invisible until a *foreign* consumer trips over it — by which time the bad shape is
    already in the shared bucket and the writer that produced it has long since exited.
    """
    missing = REQUIRED_MANIFEST_KEYS - manifest.keys()
    if missing:
        raise ValueError(
            f"catalog manifest for {manifest.get('dataset', '<unnamed>')!r} is missing "
            f"{sorted(missing)}; the lake's DatasetManifest.from_dict cannot read it"
        )
    if not isinstance(manifest["partitions"], dict):
        raise ValueError("catalog manifest 'partitions' must be a mapping")
    for key, entry in manifest["partitions"].items():
        if not isinstance(entry, dict):
            raise ValueError(f"catalog partition {key!r} must be a mapping")
        absent = REQUIRED_PARTITION_KEYS - entry.keys()
        if absent:
            raise ValueError(
                f"catalog partition {key!r} is missing {sorted(absent)}; "
                "the lake's PartitionEntry.from_dict cannot read it"
            )
