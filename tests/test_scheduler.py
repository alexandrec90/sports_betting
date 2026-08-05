import json

from sports_betting.config import Settings
from sports_betting.scheduler import CollectionJobs, build_scheduler


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
