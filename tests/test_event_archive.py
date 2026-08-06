import json
from datetime import UTC, datetime

import pyarrow.parquet as pq

from sports_betting.archive.events import EventArchive
from sports_betting.providers.thesportsdb import EventSnapshot

OBSERVED = datetime(2026, 8, 4, 12, tzinfo=UTC)


def snapshot(*, score="4"):
    return EventSnapshot.from_thesportsdb(
        {
            "idEvent": "12345",
            "strEvent": "Home vs Away",
            "strSport": "Ice Hockey",
            "dateEvent": "2026-08-03",
            "strTimestamp": "2026-08-03T23:30:00Z",
            "strHomeTeam": "Home",
            "strAwayTeam": "Away",
            "intHomeScore": score,
            "intAwayScore": "2",
            "strStatus": "Final",
        },
        observed_at=OBSERVED,
    )


def test_write_is_idempotent_and_keeps_changed_provider_snapshots(tmp_path):
    archive = EventArchive(tmp_path)
    first = archive.write([snapshot()])
    repeat = archive.write([snapshot()])
    correction = archive.write([snapshot(score="5")])

    assert first.snapshots_added == 1
    assert repeat.snapshots_added == 0
    assert correction.snapshots_added == 1
    path = tmp_path / first.partitions[0]
    rows = pq.ParquetFile(path).read().to_pylist()
    assert len(rows) == 2
    assert {row["home_score"] for row in rows} == {"4", "5"}


def test_write_creates_self_describing_catalog(tmp_path):
    result = EventArchive(tmp_path).write([snapshot()])

    manifest = json.loads((tmp_path / "_catalog" / "sports_events.json").read_text())
    assert manifest["dataset"] == "sports_events"
    assert manifest["key_columns"] == ["source", "external_id", "payload_hash"]
    assert manifest["timestamp_column"] == "event_ts"
    assert manifest["partitions"][result.partitions[0]]["rows"] == 1
    assert "payload_json" in manifest["schema"]


def test_empty_write_does_not_create_archive(tmp_path):
    result = EventArchive(tmp_path).write([])

    assert result.snapshots_received == 0
    assert result.partitions == ()
    assert list(tmp_path.iterdir()) == []
