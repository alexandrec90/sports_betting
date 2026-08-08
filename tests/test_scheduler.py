import json

import pytest

from sports_betting.config import Settings
from sports_betting.providers.thesportsdb import SportsDataProviderError
from sports_betting.scheduler import CollectionJobs, _partial_outcome, build_scheduler


def test_keyed_jobs_skip_without_credentials_and_write_health(tmp_path):
    settings = Settings(
        archive_root=tmp_path / "archive",
        scheduler_health_file=tmp_path / "health.json",
        provider_quota_file=tmp_path / "quota.json",
        football_data_api_key="",
        balldontlie_api_key="",
        the_odds_api_key="",
    )
    jobs = CollectionJobs(settings)

    assert jobs.football_data().status == "skipped"
    assert jobs.balldontlie().status == "skipped"
    assert jobs.the_odds_api().status == "skipped"

    health = json.loads(settings.scheduler_health_file.read_text())
    assert {name: row["status"] for name, row in health.items()} == {
        "football-data": "skipped",
        "balldontlie": "skipped",
        "the-odds-api": "skipped",
    }


def test_scheduler_registers_all_jobs_at_safe_interval(tmp_path):
    settings = Settings(
        archive_root=tmp_path / "archive",
        scheduler_health_file=tmp_path / "health.json",
        provider_quota_file=tmp_path / "quota.json",
        collection_interval_hours=6,
    )

    scheduler = build_scheduler(settings)

    assert {job.id for job in scheduler.get_jobs()} == {
        "football-data",
        "thesportsdb",
        "balldontlie",
        "the-odds-api",
    }
    assert {job.trigger.interval.total_seconds() for job in scheduler.get_jobs()} == {21600}


@pytest.mark.parametrize(
    ("fetched", "added", "failures", "expected"),
    [
        (5, 5, [], "ok"),
        (5, 5, ["epl: boom"], "partial"),
        (0, 0, ["epl: boom"], "error"),
    ],
)
def test_partial_outcome_distinguishes_total_failure_from_one_bad_sport(
    fetched, added, failures, expected
):
    outcome = _partial_outcome(fetched, added, failures)

    assert outcome.status == expected
    assert outcome.fetched == fetched
    assert all(failure in outcome.detail for failure in failures)


def test_one_unavailable_sport_no_longer_discards_the_sports_that_worked(tmp_path, monkeypatch):
    """Regression: a paid-only sport (EPL) made the whole balldontlie job report error 0/0."""
    settings = Settings(
        archive_root=tmp_path / "archive",
        scheduler_health_file=tmp_path / "health.json",
        provider_quota_file=tmp_path / "quota.json",
        balldontlie_api_key="test-key",  # pragma: allowlist secret - dummy, client is faked
        balldontlie_sports="nba,epl",
    )

    class FakeClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def fetch_day(self, day, *, sport=None, **_kwargs):
            if sport == "epl":
                raise SportsDataProviderError("BALLDONTLIE request failed; paid plan required")
            return [_snapshot(f"{sport}-{day.isoformat()}")]

    monkeypatch.setattr("sports_betting.scheduler.BallDontLieClient", FakeClient)

    outcome = CollectionJobs(settings).balldontlie()

    assert outcome.status == "partial"
    assert outcome.fetched == 2  # both NBA days survived the EPL failure
    assert "epl" in outcome.detail
    assert "nba" not in outcome.detail

    health = json.loads(settings.scheduler_health_file.read_text())
    assert health["balldontlie"]["status"] == "partial"


def _snapshot(external_id: str):
    from datetime import UTC, datetime

    from sports_betting.providers.thesportsdb import EventSnapshot

    moment = datetime(2026, 8, 5, 12, tzinfo=UTC)
    return EventSnapshot.from_values(
        source="balldontlie-test",
        external_id=external_id,
        observed_at=moment,
        event_ts=moment,
        time_precision="timestamp",
        sport="Basketball",
        event_name="Home vs Away",
        payload={"id": external_id},
    )


def test_scheduler_adds_opt_in_weekly_historical_refresh(tmp_path):
    settings = Settings(
        archive_root=tmp_path / "archive",
        scheduler_health_file=tmp_path / "health.json",
        provider_quota_file=tmp_path / "quota.json",
        bulk_refresh_enabled=True,
    )

    scheduler = build_scheduler(settings)

    job = next(job for job in scheduler.get_jobs() if job.id == "historical-bulk")
    assert job.trigger.interval.total_seconds() == 7 * 24 * 60 * 60
