from datetime import date

import httpx
import pytest

from sports_betting.providers import outage
from sports_betting.providers.api_sports import ApiSportsFootballClient
from sports_betting.providers.balldontlie import BallDontLieClient
from sports_betting.providers.football_data import FootballDataClient
from sports_betting.providers.odds_api import OddsApiClient
from sports_betting.providers.outage import OUTAGE_WAITS, send_through_outage
from sports_betting.providers.thesportsdb import SportsDataProviderError, TheSportsDbClient

DAY = date(2026, 11, 25)


@pytest.fixture
def slept(monkeypatch):
    waits: list[float] = []
    monkeypatch.setattr(outage, "_sleep", waits.append)
    return waits


def _answering(*answers):
    """A `send` giving each answer in turn, raising the ones that are exceptions."""
    queue = list(answers)

    def send():
        answer = queue.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    return send


@pytest.mark.parametrize(
    "fault",
    [
        httpx.Response(502),
        httpx.Response(503),
        httpx.Response(504),
        httpx.ConnectError("refused"),
        httpx.ReadTimeout("slow"),
    ],
)
def test_a_momentary_outage_is_waited_out_and_every_attempt_passes_the_gate(slept, fault):
    gate: list[None] = []
    good = httpx.Response(200)

    sent = send_through_outage(_answering(fault, good), before_request=lambda: gate.append(None))

    assert sent is good
    assert slept == [OUTAGE_WAITS[0]]
    assert len(gate) == 2


@pytest.mark.parametrize("status", [200, 401, 404, 429, 500])
def test_the_providers_own_answer_is_never_repeated(slept, status):
    answer = httpx.Response(status)

    assert send_through_outage(_answering(answer), before_request=lambda: None) is answer
    assert slept == []


def test_an_outage_that_lasts_returns_the_last_response(slept):
    answers = [httpx.Response(502), httpx.ConnectError("refused"), httpx.Response(503)]

    sent = send_through_outage(_answering(*answers), before_request=lambda: None)

    assert sent.status_code == 503
    assert slept == list(OUTAGE_WAITS)


def test_a_transport_error_that_lasts_is_raised_as_it_came(slept):
    send = _answering(*[httpx.ConnectError("refused")] * (len(OUTAGE_WAITS) + 1))

    with pytest.raises(httpx.ConnectError):
        send_through_outage(send, before_request=lambda: None)
    assert slept == list(OUTAGE_WAITS)


def test_a_spent_budget_stops_the_repeats(slept):
    claims = iter([None, RuntimeError("budget exhausted")])

    def gate():
        if (refusal := next(claims)) is not None:
            raise refusal

    with pytest.raises(RuntimeError, match="budget exhausted"):
        send_through_outage(_answering(httpx.Response(502)), before_request=gate)


# Regression for the 2026-11-25 run: one 502 on `/sports/basketball_nba/events` failed the
# sport and read the job `degraded` for six hours.
@pytest.mark.parametrize(
    ("body", "collect"),
    [
        ([], lambda http, gate: _odds(http, gate).event_starts("basketball_nba")),
        ([], lambda http, gate: _odds(http, gate).fetch("basketball_nba")),
        (
            {"errors": [], "response": []},
            lambda http, gate: ApiSportsFootballClient(
                "k", client=http, before_request=gate
            ).fixtures(DAY),
        ),
        (
            {"data": []},
            lambda http, gate: BallDontLieClient("k", client=http, before_request=gate).fetch_day(
                DAY
            ),
        ),
        (
            {"matches": []},
            lambda http, gate: FootballDataClient("k", client=http, before_request=gate).fetch_day(
                DAY
            ),
        ),
        (
            {"events": None},
            lambda http, gate: TheSportsDbClient("k", client=http, before_request=gate).fetch_day(
                DAY
            ),
        ),
    ],
    ids=["odds-events", "odds-paid", "api-sports", "balldontlie", "football-data", "thesportsdb"],
)
def test_every_provider_survives_one_bad_gateway(slept, body, collect):
    queue = [httpx.Response(502), httpx.Response(200, json=body)]
    http = httpx.Client(transport=httpx.MockTransport(lambda _request: queue.pop(0)))
    gate: list[None] = []

    assert not collect(http, lambda: gate.append(None))
    assert queue == []
    assert slept == [OUTAGE_WAITS[0]]
    assert len(gate) == 2


def test_an_outage_that_lasts_still_fails_the_sport_without_leaking_the_key(slept):
    http = httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(502)))

    with pytest.raises(SportsDataProviderError, match="502") as caught:
        OddsApiClient("private-key", client=http).event_starts("basketball_nba")

    assert "private-key" not in str(caught.value)
    assert caught.value.__cause__ is None
    assert slept == list(OUTAGE_WAITS)


def _odds(http, gate):
    return OddsApiClient("k", client=http, before_request=gate, before_free_request=gate)
