"""Immutable, provenance-preserving storage for public historical datasets."""

from __future__ import annotations

import json
import re
import shutil
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

import pyarrow.parquet as pq

_SAFE_PART = re.compile(r"[^A-Za-z0-9._=-]+")


@dataclass(frozen=True)
class BulkWriteResult:
    dataset: str
    partition: str
    source_url: str
    source_sha256: str
    rows: int
    added: bool


@dataclass(frozen=True)
class ArtifactProvenance:
    dataset: str
    partition: str
    source_url: str
    source_sha256: str
    source_media_type: str
    source_file: str
    data_file: str
    rows: int
    fetched_at: str
    license_name: str
    license_url: str


ParquetConverter = Callable[[Path, Path, str, datetime], int]


def _safe_component(value: str) -> str:
    cleaned = _SAFE_PART.sub("-", value).strip("-.")
    if not cleaned or cleaned in {".", ".."}:
        raise ValueError(f"unsafe archive component: {value!r}")
    return cleaned


class BulkArchive:
    """Write original artifacts and query-ready Parquet as immutable versions."""

    def __init__(self, root: Path | str):
        self.root = Path(root)

    def has_artifact(self, dataset: str, partition: str, source_sha256: str) -> bool:
        return self.find_artifact(dataset, partition, source_sha256) is not None

    def find_artifact(self, dataset: str, partition: str, source_sha256: str) -> dict | None:
        catalog = self._read_catalog(dataset)
        for row in catalog.get("artifacts", []):
            if (
                row.get("partition") == partition
                and row.get("source_sha256") == source_sha256
                and self._safe_path(Path(str(row.get("data_file", "")))).is_file()
            ):
                return row
        return None

    def source_path(self, artifact: dict) -> Path:
        path = self._safe_path(Path(str(artifact["source_file"])))
        if not path.is_file():
            raise FileNotFoundError(f"archived source is missing: {path}")
        return path

    def write(
        self,
        source_path: Path,
        *,
        dataset: str,
        partition_parts: tuple[str, ...],
        source_url: str,
        source_sha256: str,
        source_media_type: str,
        source_filename: str,
        fetched_at: datetime,
        license_name: str,
        license_url: str,
        converter: ParquetConverter,
    ) -> BulkWriteResult:
        safe_dataset = _safe_component(dataset)
        safe_parts = tuple(_safe_component(part) for part in partition_parts)
        logical_partition = "/".join(safe_parts)
        if self.has_artifact(safe_dataset, logical_partition, source_sha256):
            return BulkWriteResult(
                safe_dataset,
                logical_partition,
                source_url,
                source_sha256,
                0,
                False,
            )

        relative_dir = Path(
            safe_dataset,
            *safe_parts,
            f"version={source_sha256[:16]}",
        )
        target_dir = self._safe_path(relative_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        source_target = target_dir / f"source-{_safe_component(source_filename)}"
        parquet_target = target_dir / "data.parquet"
        source_temporary = source_target.with_suffix(source_target.suffix + ".tmp")
        parquet_temporary = parquet_target.with_suffix(".parquet.tmp")

        shutil.copyfile(source_path, source_temporary)
        try:
            rows = converter(source_path, parquet_temporary, source_sha256, fetched_at)
            verified = pq.ParquetFile(parquet_temporary).metadata.num_rows
            if rows != verified:
                raise RuntimeError(
                    f"bulk archive verification failed: wrote {rows} rows, read {verified}"
                )
            source_temporary.replace(source_target)
            parquet_temporary.replace(parquet_target)
        except Exception:
            source_temporary.unlink(missing_ok=True)
            parquet_temporary.unlink(missing_ok=True)
            raise

        provenance = ArtifactProvenance(
            dataset=safe_dataset,
            partition=logical_partition,
            source_url=source_url,
            source_sha256=source_sha256,
            source_media_type=source_media_type,
            source_file=source_target.relative_to(self.root.resolve()).as_posix(),
            data_file=parquet_target.relative_to(self.root.resolve()).as_posix(),
            rows=rows,
            fetched_at=fetched_at.astimezone(UTC).isoformat(),
            license_name=license_name,
            license_url=license_url,
        )
        self._write_json(target_dir / "provenance.json", asdict(provenance))
        self._write_catalog(safe_dataset)
        return BulkWriteResult(
            safe_dataset,
            logical_partition,
            source_url,
            source_sha256,
            rows,
            True,
        )

    def _safe_path(self, relative: Path) -> Path:
        root = self.root.resolve()
        path = (root / relative).resolve()
        if not path.is_relative_to(root):
            raise ValueError("archive path escapes ARCHIVE_ROOT")
        return path

    def _catalog_path(self, dataset: str) -> Path:
        return self._safe_path(Path("_catalog", f"{_safe_component(dataset)}.json"))

    def _read_catalog(self, dataset: str) -> dict:
        path = self._catalog_path(dataset)
        if not path.is_file():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _write_catalog(self, dataset: str) -> None:
        dataset_root = self._safe_path(Path(dataset))
        artifacts = []
        schemas: dict[str, str] = {}
        for provenance_path in sorted(dataset_root.rglob("provenance.json")):
            provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
            parquet_path = self._safe_path(Path(provenance["data_file"]))
            parquet = pq.ParquetFile(parquet_path)
            provenance["rows"] = parquet.metadata.num_rows
            artifacts.append(provenance)
            for field in parquet.schema_arrow:
                schemas.setdefault(field.name, str(field.type))

        fetched = [datetime.fromisoformat(row["fetched_at"]) for row in artifacts]
        manifest = {
            "dataset": dataset,
            "schema_version": 1,
            "key_columns": ["source_sha256", "source_row_number"],
            "timestamp_column": "source_fetched_at",
            "schema": schemas,
            "partitions": {
                row["data_file"]: {
                    "rows": row["rows"],
                    "min_ts": row["fetched_at"],
                    "max_ts": row["fetched_at"],
                }
                for row in artifacts
            },
            "artifacts": artifacts,
            "min_ts": min(fetched).isoformat() if fetched else None,
            "max_ts": max(fetched).isoformat() if fetched else None,
            "updated_at": datetime.now(UTC).isoformat(),
        }
        self._write_json(self._catalog_path(dataset), manifest)

    @staticmethod
    def _write_json(path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        json.loads(temporary.read_text(encoding="utf-8"))
        temporary.replace(path)
