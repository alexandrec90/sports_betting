"""The `_catalog/` manifest contract shared with the sibling data-lake package.

Two tiers on purpose. The contract tests assert the manifest shape against constants held
*in this repo*, so they run everywhere including a single-repo CI checkout. The round-trip
tests at the bottom feed a real manifest to the real `DatasetManifest.from_dict` and skip
when the sibling is absent.

Only the second tier proves the mirrored constants are still accurate, so it is not
decoration. But it cannot be the only gate either: a test that skips in CI is a gate that
reports green having checked nothing.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from sports_betting.archive.bulk import BulkArchive
from sports_betting.archive.events import EventArchive
from sports_betting.archive.manifest import (
    CATALOG_DIRNAME,
    CATALOG_SCHEMA_VERSION,
    REQUIRED_MANIFEST_KEYS,
    REQUIRED_PARTITION_KEYS,
    build_manifest,
    catalog_path,
    partition_entry,
    validate_manifest,
)
from sports_betting.providers.thesportsdb import EventSnapshot

OBSERVED = datetime(2026, 8, 4, 12, tzinfo=UTC)

SKIP_REASON = (
    "sibling data-lake package absent; install with `uv pip install -e ../data-lake[archive]`"
)


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
        observed_at=OBSERVED,
    )


def a_manifest(**overrides):
    base: dict = dict(
        dataset="sports_events",
        ts_column="event_ts",
        key_columns=("source", "external_id"),
        schema={"event_ts": "timestamp[us, tz=UTC]"},
        partitions={
            "sports_events/a.parquet": partition_entry(
                rows=3,
                min_ts="2026-01-01T00:00:00+00:00",
                max_ts="2026-01-02T00:00:00+00:00",
                updated_at="2026-01-03T00:00:00+00:00",
            )
        },
    )
    base.update(overrides)
    return build_manifest(**base)


def two_row_converter(source, target, sha256, fetched_at):
    table = pa.table({"home": ["a", "b"], "away": ["c", "d"]})
    pq.write_table(table, target)
    return table.num_rows


def write_bulk_artifact(root, csv_path):
    BulkArchive(root).write(
        csv_path,
        dataset="football_data_uk_matches",
        partition_parts=("league=E0", "season=2425"),
        source_url="https://example.test/E0.csv",
        source_sha256="a" * 64,
        source_media_type="text/csv",
        source_filename="E0.csv",
        fetched_at=datetime.now(UTC),
        license_name="terms",
        license_url="https://example.test/terms",
        converter=two_row_converter,
    )


def test_build_manifest_emits_every_required_key():
    manifest = a_manifest()
    assert REQUIRED_MANIFEST_KEYS <= manifest.keys()
    assert manifest["schema_version"] == CATALOG_SCHEMA_VERSION


def test_build_manifest_uses_ts_column_not_timestamp_column():
    """The collision itself: the lake subscripts `ts_column` and KeyErrors on the old name."""
    manifest = a_manifest()
    assert manifest["ts_column"] == "event_ts"
    assert "timestamp_column" not in manifest


def test_every_partition_entry_carries_updated_at():
    entry = next(iter(a_manifest()["partitions"].values()))
    assert REQUIRED_PARTITION_KEYS <= entry.keys()


def test_extra_keys_ride_along_unchanged():
    manifest = a_manifest(artifacts=[{"source_url": "https://example.test/a.csv"}])
    assert manifest["artifacts"] == [{"source_url": "https://example.test/a.csv"}]


def test_validate_rejects_a_manifest_missing_ts_column():
    manifest = a_manifest()
    del manifest["ts_column"]
    with pytest.raises(ValueError, match="ts_column"):
        validate_manifest(manifest)


def test_validate_rejects_a_partition_missing_updated_at():
    manifest = a_manifest()
    next(iter(manifest["partitions"].values())).pop("updated_at")
    with pytest.raises(ValueError, match="updated_at"):
        validate_manifest(manifest)


def test_validate_rejects_non_mapping_partitions():
    manifest = a_manifest()
    manifest["partitions"] = ["sports_events/a.parquet"]
    with pytest.raises(ValueError, match="must be a mapping"):
        validate_manifest(manifest)


def test_build_manifest_validates_on_the_write_path():
    """A bad partition must be rejected where it is built, not discovered by a consumer."""
    with pytest.raises(ValueError, match="min_ts"):
        a_manifest(partitions={"x.parquet": {"rows": 1, "updated_at": "2026-01-01T00:00:00+00:00"}})


def test_event_archive_writes_a_contract_shaped_manifest(tmp_path):
    """Reversion check: the writers, not just the helper, must emit the adopted shape."""
    EventArchive(tmp_path).write([snapshot()])

    manifest = json.loads(catalog_path(tmp_path, "sports_events").read_text(encoding="utf-8"))
    validate_manifest(manifest)
    assert manifest["ts_column"] == "event_ts"


def test_bulk_archive_writes_a_contract_shaped_manifest(tmp_path):
    csv_path = tmp_path / "E0.csv"
    csv_path.write_text("Div,HomeTeam\nE0,Arsenal\n", encoding="utf-8")

    write_bulk_artifact(tmp_path, csv_path)

    manifest = json.loads(
        catalog_path(tmp_path, "football_data_uk_matches").read_text(encoding="utf-8")
    )
    validate_manifest(manifest)
    assert manifest["ts_column"] == "source_fetched_at"
    # The extension key still travels in the object a foreign consumer already reads.
    assert manifest["artifacts"][0]["source_url"] == "https://example.test/E0.csv"


def test_manifest_round_trips_through_the_real_lake_reader():
    catalog = pytest.importorskip("data_lake.archive.catalog", reason=SKIP_REASON)
    manifest = a_manifest(artifacts=[{"source_url": "https://example.test/a.csv"}])

    parsed = catalog.DatasetManifest.from_dict(manifest)

    assert parsed.dataset == "sports_events"
    assert parsed.ts_column == "event_ts"
    assert parsed.total_rows == 3
    assert parsed.schema_version == catalog.CATALOG_SCHEMA_VERSION


def test_a_real_written_manifest_round_trips(tmp_path):
    """End to end: what `EventArchive` actually puts on disk, through the lake's reader."""
    catalog = pytest.importorskip("data_lake.archive.catalog", reason=SKIP_REASON)
    EventArchive(tmp_path).write([snapshot()])
    on_disk = json.loads(catalog_path(tmp_path, "sports_events").read_text(encoding="utf-8"))

    parsed = catalog.DatasetManifest.from_dict(on_disk)

    assert parsed.total_rows == 1
    assert parsed.min_ts is not None


def test_our_constants_match_the_lake_constants():
    catalog = pytest.importorskip("data_lake.archive.catalog", reason=SKIP_REASON)

    assert catalog.CATALOG_PREFIX == f"{CATALOG_DIRNAME}/"
    assert catalog.CATALOG_SCHEMA_VERSION == CATALOG_SCHEMA_VERSION
