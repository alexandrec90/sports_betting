"""The migration for manifests written before the shared-shape adoption.

Fixing the writers is only half the collision: the manifests already on disk are the ones a
foreign consumer actually meets. These tests build a genuinely old-shape manifest and assert
the rebuild converts it, because that is the failure the change exists to remove.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

import pyarrow as pa
import pyarrow.parquet as pq

from sports_betting.archive.bulk import BulkArchive
from sports_betting.archive.events import EventArchive
from sports_betting.archive.manifest import catalog_path, validate_manifest
from sports_betting.archive.recatalog import bulk_datasets, rebuild_catalogs
from sports_betting.providers.thesportsdb import EventSnapshot


def snapshot():
    return EventSnapshot.from_thesportsdb(
        {
            "idEvent": "12345",
            "strEvent": "Home vs Away",
            "strSport": "Ice Hockey",
            "dateEvent": "2026-08-03",
            "strTimestamp": "2026-08-03T23:30:00Z",
            "strHomeTeam": "Home",
            "strAwayTeam": "Away",
            "intHomeScore": "4",
            "intAwayScore": "2",
            "strStatus": "Final",
        },
        observed_at=datetime(2026, 8, 4, 12, tzinfo=UTC),
    )


def converter(source, target, sha256, fetched_at):
    table = pa.table({"home": ["a"], "away": ["b"]})
    pq.write_table(table, target)
    return table.num_rows


def write_bulk(root, dataset="football_data_uk_matches"):
    source = root / "input.csv"
    source.write_text("Div,HomeTeam\nE0,Arsenal\n", encoding="utf-8")
    BulkArchive(root).write(
        source,
        dataset=dataset,
        partition_parts=("league=E0",),
        source_url="https://example.test/E0.csv",
        source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        source_media_type="text/csv",
        source_filename="E0.csv",
        fetched_at=datetime(2026, 8, 1, tzinfo=UTC),
        license_name="terms",
        license_url="https://example.test/terms",
        converter=converter,
    )
    source.unlink()


def make_manifest_old_shape(path):
    """Undo the adoption on disk, reproducing exactly what the previous writer emitted."""
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["timestamp_column"] = manifest.pop("ts_column")
    for entry in manifest["partitions"].values():
        entry.pop("updated_at", None)
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")


def test_bulk_datasets_finds_datasets_and_ignores_infrastructure_dirs(tmp_path):
    write_bulk(tmp_path)
    (tmp_path / "_staging").mkdir(exist_ok=True)
    (tmp_path / "_staging" / "provenance.json").write_text("{}", encoding="utf-8")

    assert bulk_datasets(tmp_path) == ["football_data_uk_matches"]


def test_rebuild_converts_an_old_shape_event_manifest(tmp_path):
    EventArchive(tmp_path).write([snapshot()])
    path = catalog_path(tmp_path, "sports_events")
    make_manifest_old_shape(path)

    rebuilt = rebuild_catalogs(tmp_path)

    assert "sports_events" in rebuilt
    manifest = json.loads(path.read_text(encoding="utf-8"))
    validate_manifest(manifest)
    assert "timestamp_column" not in manifest


def test_rebuild_converts_an_old_shape_bulk_manifest(tmp_path):
    write_bulk(tmp_path)
    path = catalog_path(tmp_path, "football_data_uk_matches")
    make_manifest_old_shape(path)

    rebuild_catalogs(tmp_path)

    manifest = json.loads(path.read_text(encoding="utf-8"))
    validate_manifest(manifest)
    assert manifest["ts_column"] == "source_fetched_at"


def test_rebuild_preserves_the_bulk_provenance_extension(tmp_path):
    write_bulk(tmp_path)
    path = catalog_path(tmp_path, "football_data_uk_matches")
    make_manifest_old_shape(path)

    rebuild_catalogs(tmp_path)

    manifest = json.loads(path.read_text(encoding="utf-8"))
    assert manifest["artifacts"][0]["license_url"] == "https://example.test/terms"


def test_rebuild_is_idempotent(tmp_path):
    EventArchive(tmp_path).write([snapshot()])
    write_bulk(tmp_path)

    first = rebuild_catalogs(tmp_path)
    second = rebuild_catalogs(tmp_path)

    assert first == second == ["sports_events", "football_data_uk_matches"]


def test_rebuild_on_an_empty_root_does_nothing(tmp_path):
    assert rebuild_catalogs(tmp_path) == []
