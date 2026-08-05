import json
from datetime import UTC, datetime

import pyarrow.parquet as pq

from sports_betting.archive.odds import OddsArchive
from sports_betting.providers.odds_api import OddsSnapshot

OBSERVED = datetime(2026, 8, 5, 12, tzinfo=UTC)


def snapshot(price: float) -> OddsSnapshot:
    return OddsSnapshot.from_api(
        {
            "id": "event-1",
            "sport_title": "NBA",
            "commence_time": "2026-08-06T00:00:00Z",
            "home_team": "Home",
            "away_team": "Away",
            "bookmakers": [{"key": "example", "price": price}],
        },
        sport_key="basketball_nba",
        observed_at=OBSERVED,
    )


def test_odds_archive_is_idempotent_and_preserves_line_movements(tmp_path):
    archive = OddsArchive(tmp_path)
    first = archive.write([snapshot(1.9)])
    repeat = archive.write([snapshot(1.9)])
    changed = archive.write([snapshot(2.0)])

    assert (first.snapshots_added, repeat.snapshots_added, changed.snapshots_added) == (1, 0, 1)
    rows = pq.ParquetFile(tmp_path / first.partitions[0]).read().to_pylist()
    assert len(rows) == 2
    manifest = json.loads((tmp_path / "_catalog" / "sports_odds.json").read_text())
    assert manifest["partitions"][first.partitions[0]]["rows"] == 2
