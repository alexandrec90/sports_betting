"""The Odds API credit planner and the scheduled job that follows it."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from sports_betting import scheduler
from sports_betting.config import Settings
from sports_betting.odds_plan import (
    RefreshLog,
    expand_focus,
    has_event_within,
    order_by_staleness,
    run_allowance,
    runs_left_in_month,
)
from sports_betting.providers.odds_api import OddsApiClient
from sports_betting.scheduler import ODDS_PROVIDER_RESERVE, CollectionJobs

NOW = datetime(2026, 10, 3, 12, tzinfo=UTC)


# --- pure planning ------------------------------------------------------------------


def test_expand_focus_keeps_priority_order_matches_globs_and_drops_inactive():
    active = ["tennis_wta_china_open", "soccer_epl", "tennis_atp_china_open", "icehockey_nhl"]
    patterns = ["icehockey_nhl", "baseball_mlb", "tennis_*", "tennis_atp_*", "soccer_epl"]

    assert expand_focus(patterns, active) == [
        "icehockey_nhl",
        "tennis_atp_china_open",
        "tennis_wta_china_open",
        "soccer_epl",
    ]


@pytest.mark.parametrize(
    ("now", "interval", "expected"),
    [
        (datetime(2026, 10, 31, 20, tzinfo=UTC), 6, 1),
        (datetime(2026, 10, 31, 12, tzinfo=UTC), 6, 2),
        (datetime(2026, 12, 31, 0, tzinfo=UTC), 6, 4),
        (datetime(2026, 10, 1, 0, tzinfo=UTC), 24, 31),
    ],
)
def test_runs_left_in_month_counts_this_run_and_rolls_over_december(now, interval, expected):
    assert runs_left_in_month(now, interval) == expected


@pytest.mark.parametrize(
    ("credits_left", "runs_left", "cost", "expected"),
    [(375, 114, 1, 3), (5, 114, 1, 1), (0, 10, 1, 0), (1, 10, 2, 0), (40, 5, 2, 4)],
)
def test_run_allowance_is_an_even_share_and_at_least_one_when_affordable(
    credits_left, runs_left, cost, expected
):
    assert run_allowance(credits_left, runs_left, cost) == expected


def test_order_by_staleness_puts_never_fetched_first_then_oldest():
    keys = ["a", "b", "c", "d"]
    fetched = {"a": NOW - timedelta(hours=1), "c": NOW - timedelta(hours=9)}
    assert order_by_staleness(keys, fetched) == ["b", "d", "c", "a"]


def test_has_event_within_includes_the_window_edges_only():
    window = timedelta(hours=36)
    assert has_event_within([NOW + window], NOW, window)
    assert has_event_within([NOW], NOW, window)
    assert not has_event_within([NOW - timedelta(minutes=1), NOW + window * 2], NOW, window)


def test_refresh_log_round_trips_and_tolerates_a_corrupt_file(tmp_path):
    log = RefreshLog(tmp_path / "refresh.json")
    assert log.load() == {}
    log.save({"icehockey_nhl": NOW})
    assert RefreshLog(log.path).load() == {"icehockey_nhl": NOW}
    log.path.write_text("{not json", encoding="utf-8")
    assert log.load() == {}


# --- the scheduled job --------------------------------------------------------------


def odds_event(event_id: str, start: datetime) -> dict[str, Any]:
    return {
        "id": event_id,
        "sport_title": "Test",
        "commence_time": start.isoformat().replace("+00:00", "Z"),
        "home_team": "Home",
        "away_team": "Away",
        "bookmakers": [],
    }


class FakeOddsApi:
    """MockTransport handler: free sports/events listings, paid odds, a credit counter."""

    def __init__(self, remaining: int, starts: dict[str, datetime]):
        self.remaining = remaining
        self.starts = starts
        self.paid: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        parts = request.url.path.strip("/").split("/")  # v4, sports, [key, events|odds]
        headers = {"x-requests-remaining": str(self.remaining)}
        if len(parts) == 2:
            sports = [{"key": key, "active": True, "has_outrights": False} for key in self.starts]
            sports.append({"key": "baseball_mlb", "active": False, "has_outrights": False})
            sports.append({"key": "icehockey_nhl_winner", "active": True, "has_outrights": True})
            return httpx.Response(200, json=sports, headers=headers)
        key, kind = parts[2], parts[3]
        events = [odds_event(f"{key}-1", self.starts[key])]
        if kind == "odds":
            self.paid.append(key)
            self.remaining -= 1
            headers = {"x-requests-remaining": str(self.remaining)}
        return httpx.Response(200, json=events, headers=headers)


def run_job(
    monkeypatch, tmp_path, api: FakeOddsApi, **overrides: Any
) -> tuple[scheduler.JobOutcome, CollectionJobs]:
    transport = httpx.Client(transport=httpx.MockTransport(api))
    monkeypatch.setattr(
        scheduler,
        "OddsApiClient",
        lambda *args, **kwargs: OddsApiClient(*args, client=transport, **kwargs),
    )
    values: dict[str, Any] = {
        "archive_root": tmp_path / "archive",
        "scheduler_health_file": tmp_path / "health.json",
        "provider_quota_file": tmp_path / "quota.json",
        "odds_refresh_file": tmp_path / "refresh.json",
        "the_odds_api_key": "test-key",  # pragma: allowlist secret - dummy, MockTransport only
        "the_odds_api_sports": "icehockey_nhl,soccer_epl,tennis_atp_*,baseball_mlb",
    }
    values.update(overrides)
    jobs = CollectionJobs(Settings(**values))
    jobs.odds_gate._sleep = jobs.odds_free_gate._sleep = lambda _seconds: None
    return jobs.the_odds_api(now=NOW), jobs


def test_odds_job_pays_only_for_active_focus_leagues_with_a_game_soon(monkeypatch, tmp_path):
    api = FakeOddsApi(
        400,
        {
            "icehockey_nhl": NOW + timedelta(hours=6),
            "soccer_epl": NOW + timedelta(days=5),
            "tennis_atp_china_open": NOW + timedelta(hours=2),
        },
    )

    outcome, jobs = run_job(monkeypatch, tmp_path, api)

    assert api.paid == ["icehockey_nhl", "tennis_atp_china_open"]
    assert (outcome.status, outcome.fetched, outcome.added) == ("ok", 2, 2)
    assert outcome.detail.startswith("2/3 paid call(s) over 3 active focus league(s)")
    assert jobs.ledger.used("the-odds-api") == (2, 2)
    assert set(RefreshLog(tmp_path / "refresh.json").load()) == {
        "icehockey_nhl",
        "tennis_atp_china_open",
    }


def test_odds_job_spends_its_allowance_on_the_stalest_league_first(monkeypatch, tmp_path):
    RefreshLog(tmp_path / "refresh.json").save({"icehockey_nhl": NOW - timedelta(hours=1)})
    api = FakeOddsApi(
        400,
        {
            "icehockey_nhl": NOW + timedelta(hours=6),
            "tennis_atp_china_open": NOW + timedelta(hours=2),
        },
    )

    outcome, _ = run_job(monkeypatch, tmp_path, api, the_odds_api_monthly_budget=1)

    assert api.paid == ["tennis_atp_china_open"]
    assert outcome.detail.startswith("1/1 paid call(s)")


def test_odds_job_stops_at_the_provider_reserve_whatever_the_ledger_says(monkeypatch, tmp_path):
    api = FakeOddsApi(ODDS_PROVIDER_RESERVE, {"icehockey_nhl": NOW + timedelta(hours=6)})

    outcome, _ = run_job(monkeypatch, tmp_path, api)

    assert api.paid == []
    assert (outcome.status, outcome.fetched) == ("ok", 0)
    assert outcome.detail.startswith("0/0 paid call(s)")


def test_odds_job_records_the_monthly_spend_in_the_ledger_file(monkeypatch, tmp_path):
    api = FakeOddsApi(400, {"icehockey_nhl": NOW + timedelta(hours=6)})
    run_job(monkeypatch, tmp_path, api)
    ledger = json.loads((tmp_path / "quota.json").read_text(encoding="utf-8"))
    assert ledger["monthly"] == {"the-odds-api": 1}
    assert ledger["month"] == datetime.now(UTC).strftime("%Y-%m")
