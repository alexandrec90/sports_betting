from datetime import UTC, date, datetime

import pytest

from sports_betting.archive import EventArchive
from sports_betting.pipeline import ingest_events
from sports_betting.providers.thesportsdb import EventSnapshot


class FakeProvider:
    def __init__(self):
        self.calls = []

    def fetch_day(self, day, *, sport=None, league=None):
        self.calls.append((day, sport, league))
        return [
            EventSnapshot.from_thesportsdb(
                {
                    "idEvent": day.isoformat(),
                    "strEvent": "Home vs Away",
                    "strSport": sport or "Soccer",
                    "dateEvent": day.isoformat(),
                    "strHomeTeam": "Home",
                    "strAwayTeam": "Away",
                    "intHomeScore": "1",
                    "intAwayScore": "0",
                },
                observed_at=datetime(2026, 8, 4, tzinfo=UTC),
            )
        ]


def test_pipeline_fetches_each_day_and_forwards_filters(tmp_path):
    provider = FakeProvider()

    result = ingest_events(
        provider,
        EventArchive(tmp_path),
        start=date(2026, 8, 2),
        end=date(2026, 8, 3),
        sport="Soccer",
        league="4328",
    )

    assert result.days == 2
    assert result.snapshots_fetched == 2
    assert result.snapshots_added == 2
    assert provider.calls == [
        (date(2026, 8, 2), "Soccer", "4328"),
        (date(2026, 8, 3), "Soccer", "4328"),
    ]


@pytest.mark.parametrize(
    ("start", "end", "message"),
    [
        (date(2026, 8, 4), date(2026, 8, 3), "start must"),
        (date(2026, 1, 1), date(2026, 2, 1), "31 days"),
    ],
)
def test_pipeline_rejects_invalid_or_excessive_ranges(tmp_path, start, end, message):
    with pytest.raises(ValueError, match=message):
        ingest_events(FakeProvider(), EventArchive(tmp_path), start=start, end=end)
