import json
from datetime import UTC, date, datetime

import httpx
import pytest

from sports_betting.providers.thesportsdb import (
    EventSnapshot,
    SportsDataProviderError,
    TheSportsDbClient,
)

OBSERVED = datetime(2026, 8, 4, 12, tzinfo=UTC)


def event_payload(**overrides):
    event = {
        "idEvent": "12345",
        "strEvent": "Montreal Example vs Quebec Example",
        "strSport": "Ice Hockey",
        "idLeague": "4380",
        "strLeague": "Example League",
        "strSeason": "2026-2027",
        "dateEvent": "2026-08-03",
        "strTimestamp": "2026-08-03T23:30:00Z",
        "strHomeTeam": "Montreal Example",
        "strAwayTeam": "Quebec Example",
        "intHomeScore": "4",
        "intAwayScore": "2",
        "strStatus": "Match Finished",
        "strVenue": "Example Arena",
        "strCountry": "Canada",
    }
    event.update(overrides)
    return event


def test_snapshot_normalizes_result_and_drops_secret_shaped_raw_fields():
    raw = event_payload(apiToken="must-not-be-archived", nested={"password": "also-secret"})

    snapshot = EventSnapshot.from_thesportsdb(raw, observed_at=OBSERVED)

    assert snapshot.status == "final"
    assert snapshot.home_score == "4"
    assert snapshot.event_ts == datetime(2026, 8, 3, 23, 30, tzinfo=UTC)
    assert snapshot.time_precision == "timestamp"
    assert "must-not-be-archived" not in snapshot.payload_json
    assert "also-secret" not in snapshot.payload_json


def test_snapshot_uses_explicit_date_precision_when_time_is_missing():
    snapshot = EventSnapshot.from_thesportsdb(
        event_payload(
            strTimestamp=None,
            strTime=None,
            strStatus=None,
            intHomeScore=None,
            intAwayScore=None,
        ),
        observed_at=OBSERVED,
    )

    assert snapshot.event_ts == datetime(2026, 8, 3, tzinfo=UTC)
    assert snapshot.time_precision == "date_only"
    assert snapshot.status == "scheduled"


def test_client_calls_documented_day_endpoint_and_skips_malformed_events():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/123/eventsday.php")
        assert dict(request.url.params) == {
            "d": "2026-08-03",
            "s": "Ice Hockey",
            "l": "4380",
        }
        return httpx.Response(
            200,
            json={"events": [event_payload(), {"idEvent": "missing-fields"}, "not-an-object"]},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = TheSportsDbClient(client=client)

    events = provider.fetch_day(
        date(2026, 8, 3), sport="Ice Hockey", league="4380", observed_at=OBSERVED
    )

    assert [event.external_id for event in events] == ["12345"]


def test_client_returns_empty_for_provider_null_and_hides_key_on_failure():
    empty = httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"events": None}))
    )
    assert TheSportsDbClient(client=empty).fetch_day(date(2026, 8, 3)) == []

    failing = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(429)))
    with pytest.raises(SportsDataProviderError) as exc_info:
        TheSportsDbClient("private-key", client=failing).fetch_day(date(2026, 8, 3))
    assert "private-key" not in str(exc_info.value)


def test_payload_hash_is_stable_across_observation_times_but_changes_with_result():
    first = EventSnapshot.from_thesportsdb(event_payload(), observed_at=OBSERVED)
    later = EventSnapshot.from_thesportsdb(
        event_payload(), observed_at=datetime(2026, 8, 4, 13, tzinfo=UTC)
    )
    correction = EventSnapshot.from_thesportsdb(
        event_payload(intHomeScore="5"), observed_at=OBSERVED
    )

    assert first.payload_hash == later.payload_hash
    assert first.payload_hash != correction.payload_hash
    assert json.loads(first.payload_json)["idEvent"] == "12345"
