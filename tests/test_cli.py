import json
from argparse import Namespace
from datetime import date

import pytest

from sports_betting import cli


def args(**overrides):
    values = {"date": None, "start": None, "end": None}
    values.update(overrides)
    return Namespace(**values)


def test_date_range_accepts_one_day_or_complete_range():
    assert cli._date_range(args(date=date(2026, 8, 3))) == (
        date(2026, 8, 3),
        date(2026, 8, 3),
    )
    assert cli._date_range(args(start=date(2026, 8, 1), end=date(2026, 8, 3))) == (
        date(2026, 8, 1),
        date(2026, 8, 3),
    )


@pytest.mark.parametrize(
    "bad_args",
    [
        args(date=date(2026, 8, 3), start=date(2026, 8, 1), end=date(2026, 8, 3)),
        args(start=date(2026, 8, 1)),
        args(end=date(2026, 8, 3)),
    ],
)
def test_date_range_rejects_ambiguous_or_partial_arguments(bad_args):
    with pytest.raises(ValueError):
        cli._date_range(bad_args)


def test_main_writes_parseable_failure_artifact(monkeypatch, tmp_path, capsys):
    report = tmp_path / "report.json"
    monkeypatch.setattr(cli, "REPORT_PATH", report)
    monkeypatch.setattr(cli, "get_settings", lambda: (_ for _ in ()).throw(RuntimeError("boom")))

    assert cli.main(["ingest-events", "--date", "2026-08-03"]) == 1

    assert json.loads(report.read_text()) == {
        "error": "boom",
        "error_type": "RuntimeError",
        "ok": False,
    }
    assert str(report) in capsys.readouterr().out


def _health_settings(tmp_path):
    from sports_betting.config import Settings

    return Settings(
        archive_root=tmp_path / "archive",
        scheduler_health_file=tmp_path / "health.json",
        provider_quota_file=tmp_path / "quota.json",
    )


def test_health_exits_non_zero_and_names_the_failing_job(monkeypatch, tmp_path, capsys):
    from sports_betting.health import HealthStore
    from sports_betting.scheduler import JobOutcome

    settings = _health_settings(tmp_path)
    store = HealthStore(settings.scheduler_health_file)
    store.record("the-odds-api", JobOutcome("error", detail="401 INVALID_KEY"))
    store.record("thesportsdb", JobOutcome("ok", fetched=9, added=4))
    monkeypatch.setattr(cli, "get_settings", lambda: settings)

    assert cli.main(["health"]) == 1

    out = capsys.readouterr().out
    assert "the-odds-api" in out
    assert "failing" in out
    assert "401 INVALID_KEY" in out  # the reason is on screen, not just in the artifact
    assert str(settings.scheduler_health_file) in out


def test_health_exits_zero_when_everything_is_healthy(monkeypatch, tmp_path, capsys):
    from sports_betting.health import HealthStore
    from sports_betting.scheduler import JobOutcome

    settings = _health_settings(tmp_path)
    HealthStore(settings.scheduler_health_file).record(
        "thesportsdb", JobOutcome("ok", fetched=9, added=4)
    )
    monkeypatch.setattr(cli, "get_settings", lambda: settings)

    assert cli.main(["health"]) == 0
    assert "thesportsdb" in capsys.readouterr().out


def test_health_reports_a_missing_artifact_rather_than_claiming_health(
    monkeypatch, tmp_path, capsys
):
    settings = _health_settings(tmp_path)
    monkeypatch.setattr(cli, "get_settings", lambda: settings)

    assert cli.main(["health"]) == 1
    assert "has the scheduler run?" in capsys.readouterr().out


def test_health_quiet_hides_healthy_jobs(monkeypatch, tmp_path, capsys):
    from sports_betting.health import HealthStore
    from sports_betting.scheduler import JobOutcome

    settings = _health_settings(tmp_path)
    store = HealthStore(settings.scheduler_health_file)
    store.record("thesportsdb", JobOutcome("ok", fetched=9, added=4))
    store.record("the-odds-api", JobOutcome("error", detail="boom"))
    monkeypatch.setattr(cli, "get_settings", lambda: settings)

    assert cli.main(["health", "--quiet"]) == 1

    out = capsys.readouterr().out
    assert "the-odds-api" in out
    assert "thesportsdb" not in out


def test_health_lines_sorts_worst_first():
    report = {
        "jobs": {
            "fine": {"status": "ok", "runs": 1},
            "broken": {"status": "failing", "runs": 1, "last_error": "boom"},
            "old": {"status": "stale", "runs": 1},
        }
    }

    lines = cli.health_lines(report)

    order = [line.split()[0] for line in lines if not line.startswith(" ")]
    assert order == ["broken", "old", "fine"]


def test_age_renders_relative_time():
    from datetime import UTC, datetime, timedelta

    now = datetime(2026, 8, 8, 12, tzinfo=UTC)

    assert cli._age(None) == "never"
    assert cli._age("not-a-timestamp") == "unknown"
    assert cli._age((now - timedelta(seconds=30)).isoformat(), now=now) == "30s ago"
    assert cli._age((now - timedelta(minutes=5)).isoformat(), now=now) == "5m ago"
    assert cli._age((now - timedelta(hours=3)).isoformat(), now=now) == "3h ago"
    assert cli._age((now - timedelta(days=2)).isoformat(), now=now) == "2d ago"


def test_bulk_parser_accepts_source_specific_ranges():
    parsed = cli.build_parser().parse_args(
        [
            "bulk-import",
            "football-data",
            "--from-season",
            "2020",
            "--to-season",
            "2021",
            "--leagues",
            "E0,SP1",
        ]
    )

    assert parsed.bulk_source == "football-data"
    assert parsed.from_season == 2020
    assert parsed.to_season == 2021
    assert parsed.leagues == "E0,SP1"
