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
