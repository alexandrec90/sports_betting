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
        TheSportsDbClient("private-key", client=failing, sleep=lambda _: None).fetch_day(
            date(2026, 8, 3)
        )
    assert "private-key" not in str(exc_info.value)


def _sequence_client(*responses: httpx.Response) -> tuple[httpx.Client, list[httpx.Request]]:
    seen: list[httpx.Request] = []
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return queue.pop(0)

    return httpx.Client(transport=httpx.MockTransport(handler)), seen


def test_client_waits_out_rate_limit_and_retries():
    # Regression: the shared free key's bucket is drained by other users, so a 429 on the
    # first call failed every sport for the whole run instead of waiting it out.
    client, seen = _sequence_client(
        httpx.Response(429, headers={"Retry-After": "42"}, text="<html>"),
        httpx.Response(200, json={"events": [event_payload()]}),
    )
    gate_calls: list[None] = []
    slept: list[float] = []
    provider = TheSportsDbClient(
        client=client, before_request=lambda: gate_calls.append(None), sleep=slept.append
    )

    events = provider.fetch_day(date(2026, 8, 3), observed_at=OBSERVED)

    assert [event.external_id for event in events] == ["12345"]
    assert slept == [42.0]
    assert len(seen) == 2
    # The retry passes the provider gate again, so pacing still holds.
    assert len(gate_calls) == 2


def test_client_uses_default_wait_when_retry_after_is_missing_or_malformed():
    client, _ = _sequence_client(
        httpx.Response(429),
        httpx.Response(429, headers={"Retry-After": "soon"}),
        httpx.Response(200, json={"events": None}),
    )
    slept: list[float] = []

    assert TheSportsDbClient(client=client, sleep=slept.append).fetch_day(date(2026, 8, 3)) == []
    assert slept == [60.0, 60.0]


def test_client_gives_up_after_bounded_retries_and_names_the_status():
    client, seen = _sequence_client(*[httpx.Response(429, headers={"Retry-After": "1"})] * 5)
    slept: list[float] = []

    with pytest.raises(SportsDataProviderError) as exc_info:
        TheSportsDbClient("private-key", client=client, sleep=slept.append).fetch_day(
            date(2026, 8, 3)
        )

    assert len(seen) == 3
    assert slept == [1.0, 1.0]
    assert "HTTP 429" in str(exc_info.value)
    assert "private-key" not in str(exc_info.value)


def test_client_does_not_wait_for_a_retry_after_beyond_its_cap():
    client, seen = _sequence_client(httpx.Response(429, headers={"Retry-After": "3600"}))
    slept: list[float] = []

    with pytest.raises(SportsDataProviderError, match="HTTP 429"):
        TheSportsDbClient(client=client, sleep=slept.append).fetch_day(date(2026, 8, 3))

    assert slept == []
    assert len(seen) == 1


def test_client_does_not_retry_other_http_errors():
    client, seen = _sequence_client(httpx.Response(500))
    slept: list[float] = []

    with pytest.raises(SportsDataProviderError, match="HTTP 500"):
        TheSportsDbClient(client=client, sleep=slept.append).fetch_day(date(2026, 8, 3))

    assert slept == []
    assert len(seen) == 1


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
