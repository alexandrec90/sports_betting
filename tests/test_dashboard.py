"""The operator dashboard: the status payload built from artifacts, and its two routes on
the overlay service."""

from __future__ import annotations

import http.client
import json
import re
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sports_betting import cli
from sports_betting.config import Settings
from sports_betting.dashboard import PAGE, StatusSources, build_status
from sports_betting.dashboard.status import (
    API_SPORTS_LEDGER_KEY,
    MAX_REASON_CHARS,
    ODDS_API_LEDGER_KEY,
    coverage_section,
    dataset_row,
    dataset_section,
    fair_line_section,
    health_section,
    job_rows,
    quota_rows,
    quota_section,
)
from sports_betting.health import HealthStore
from sports_betting.overlay.coverage import CoverageTracker
from sports_betting.overlay.lines import FairLine
from sports_betting.overlay.offers import CatalogEntry
from sports_betting.overlay.server import OverlayServer, loopback_host
from sports_betting.scheduler import JobOutcome

NOW = datetime(2026, 11, 27, 12, tzinfo=UTC)


def sources(tmp_path: Path) -> StatusSources:
    return StatusSources(
        health_file=tmp_path / "health.json",
        quota_file=tmp_path / "quotas.json",
        coverage_file=tmp_path / "coverage.json",
        archive_root=tmp_path / "archive",
        odds_monthly_budget=450,
        api_sports_daily_budget=90,
    )


def fair(sport: str, *, start: datetime, observed: datetime, sharp: bool = False) -> FairLine:
    return FairLine(
        external_id=f"{sport}-{start.isoformat()}",
        sport=sport,
        league=None,
        start=start,
        home="Home",
        away="Away",
        home_prob=0.5,
        away_prob=0.5,
        books=2,
        observed_at=observed,
        sharp=sharp,
    )


def write_manifest(root: Path, name: str, payload: object) -> None:
    catalog = root / "_catalog"
    catalog.mkdir(parents=True, exist_ok=True)
    text = payload if isinstance(payload, str) else json.dumps(payload)
    (catalog / f"{name}.json").write_text(text, encoding="utf-8")


# --- health -----------------------------------------------------------------------


def test_job_rows_put_the_worst_first_trim_the_reason_and_drop_the_traceback():
    report = {
        "jobs": {
            "b-ok": {"status": "ok", "runs": 3, "last_traceback": "Traceback ..."},
            "a-failing": {"status": "failing", "last_error": "x" * 1000},
            "c-skipped": {"status": "skipped", "skip_reason": "API key not set"},
            "d-weird": {"status": "something-new"},
        }
    }
    rows = job_rows(report)
    assert [row["job"] for row in rows] == ["a-failing", "c-skipped", "b-ok", "d-weird"]
    assert len(rows[0]["reason"]) == MAX_REASON_CHARS
    assert rows[1]["reason"] == "API key not set"
    assert all("last_traceback" not in row for row in rows)
    assert rows[2]["runs"] == 3 and rows[3]["runs"] == 0


def test_health_section_says_why_when_the_scheduler_has_not_run(tmp_path):
    section = health_section(tmp_path / "missing.json", now=NOW)
    assert section["readable"] is False and section["ok"] is False and section["jobs"] == []
    assert "serve" in section["error"]


def test_health_section_reports_each_job_status(tmp_path):
    path = tmp_path / "health.json"
    store = HealthStore(path)
    store.record("football-data", JobOutcome("ok", fetched=4, added=2))
    store.record("the-odds-api", JobOutcome("error", detail="HTTP 401"))

    section = health_section(path, now=datetime.now(UTC))

    assert section["readable"] is True and section["ok"] is False
    assert section["problems"] == ["the-odds-api"]
    by_job = {row["job"]: row for row in section["jobs"]}
    assert by_job["the-odds-api"]["status"] == "failing"
    assert by_job["the-odds-api"]["reason"] == "HTTP 401"
    assert by_job["football-data"]["last_added"] == 2


# --- quotas -----------------------------------------------------------------------


def test_quota_rows_count_the_current_day_and_month(tmp_path):
    payload = {
        "date": "2026-11-27",
        "providers": {API_SPORTS_LEDGER_KEY: 41},
        "month": "2026-11",
        "monthly": {ODDS_API_LEDGER_KEY: 120},
    }
    odds, api_sports = quota_rows(payload, sources(tmp_path), now=NOW)
    assert (odds["used"], odds["limit"], odds["period"]) == (120, 450, "month 2026-11")
    assert (api_sports["used"], api_sports["limit"]) == (41, 90)


def test_quota_rows_read_zero_once_the_period_has_rolled_over(tmp_path):
    payload = {
        "date": "2026-11-26",
        "providers": {API_SPORTS_LEDGER_KEY: 90},
        "month": "2026-10",
        "monthly": {ODDS_API_LEDGER_KEY: 450},
    }
    assert [row["used"] for row in quota_rows(payload, sources(tmp_path), now=NOW)] == [0, 0]


def test_quota_rows_tolerate_a_malformed_ledger_shape(tmp_path):
    payload = {"date": "2026-11-27", "providers": [1, 2], "month": "2026-11", "monthly": None}
    assert [row["used"] for row in quota_rows(payload, sources(tmp_path), now=NOW)] == [0, 0]


def test_quota_section_reports_an_unreadable_ledger_without_raising(tmp_path):
    where = sources(tmp_path)
    assert quota_section(where, now=NOW)["error"] is None  # absent: nothing spent yet
    where.quota_file.write_text("{half", encoding="utf-8")
    section = quota_section(where, now=NOW)
    assert section["error"] == "unreadable: JSONDecodeError"
    assert [row["used"] for row in section["rows"]] == [0, 0]
    where.quota_file.write_text("[]", encoding="utf-8")
    assert quota_section(where, now=NOW)["error"] == "not a JSON object"


# --- fair lines, coverage, datasets -------------------------------------------------


def test_fair_line_section_counts_by_sport_and_dates_the_freshest_price():
    lines = [
        fair("Ice Hockey", start=NOW + timedelta(hours=5), observed=NOW - timedelta(hours=2)),
        fair("Ice Hockey", start=NOW + timedelta(hours=1), observed=NOW - timedelta(hours=1)),
        fair(
            "Soccer", start=NOW + timedelta(hours=9), observed=NOW - timedelta(hours=3), sharp=True
        ),
    ]
    section = fair_line_section(lines, now=NOW)
    assert section["total"] == 3 and section["sharp"] == 1
    assert section["as_of"] == (NOW - timedelta(hours=1)).isoformat()
    assert section["next_start"] == (NOW + timedelta(hours=1)).isoformat()
    assert section["by_sport"] == [
        {"sport": "Ice Hockey", "lines": 2},
        {"sport": "Soccer", "lines": 1},
    ]


def test_fair_line_section_is_empty_without_lines():
    assert fair_line_section([], now=NOW) == {
        "total": 0,
        "sharp": 0,
        "as_of": None,
        "next_start": None,
        "by_sport": [],
    }


def test_coverage_section_totals_each_sport_across_leagues(tmp_path):
    path = tmp_path / "coverage.json"
    CoverageTracker(path, now=lambda: NOW).record(
        [
            CatalogEntry("1", "FOOTBALL", "EPL"),
            CatalogEntry("2", "FOOTBALL", "Serie A"),
            CatalogEntry("3", "BASEBALL", "MLB"),
        ],
        {"1", "3"},
    )
    section = coverage_section(path)
    assert (section["events"], section["priced"]) == (3, 2)
    assert section["by_sport"] == [
        {"sport": "FOOTBALL", "events": 2, "priced": 1},
        {"sport": "BASEBALL", "events": 1, "priced": 1},
    ]
    assert coverage_section(tmp_path / "absent.json")["events"] == 0


def test_dataset_row_sums_partitions_and_finds_the_newest_record():
    row = dataset_row(
        {
            "dataset": "odds_snapshots",
            "updated_at": "2026-11-27T00:00:00+00:00",
            "partitions": {
                "a": {"rows": 10, "max_ts": "2026-11-20T00:00:00+00:00"},
                "b": {"rows": 5, "max_ts": "2026-11-26T00:00:00+00:00"},
                "junk": "not a mapping",
            },
        }
    )
    assert row == {
        "dataset": "odds_snapshots",
        "partitions": 2,
        "rows": 15,
        "newest": "2026-11-26T00:00:00+00:00",
        "updated_at": "2026-11-27T00:00:00+00:00",
    }


@pytest.mark.parametrize(
    "manifest", [{}, {"dataset": 3, "partitions": {}}, {"dataset": "x", "partitions": []}]
)
def test_dataset_row_refuses_a_manifest_outside_the_shared_shape(manifest):
    assert dataset_row(manifest) is None


def test_dataset_section_lists_good_manifests_and_names_bad_ones(tmp_path):
    root = tmp_path / "archive"
    assert dataset_section(root) == {
        "catalog": str(root / "_catalog"),
        "present": False,
        "rows": [],
        "unreadable": [],
    }
    write_manifest(root, "small", {"dataset": "small", "partitions": {"p": {"rows": 1}}})
    write_manifest(root, "big", {"dataset": "big", "partitions": {"p": {"rows": 9}}})
    write_manifest(root, "broken", "{not json")
    write_manifest(root, "shapeless", {"rows": 3})

    section = dataset_section(root)

    assert section["present"] is True
    assert [row["dataset"] for row in section["rows"]] == ["big", "small"]
    assert section["unreadable"] == ["broken.json", "shapeless.json"]


def test_build_status_never_raises_on_an_empty_checkout(tmp_path):
    payload = build_status(sources(tmp_path), [], now=NOW)
    assert payload["generated_at"] == NOW.isoformat()
    assert payload["health"]["readable"] is False
    assert payload["datasets"]["present"] is False
    json.dumps(payload)  # the server sends it as-is


def test_status_sources_take_budgets_and_paths_from_settings(tmp_path):
    settings = Settings(
        _env_file=None,
        archive_root=tmp_path,
        the_odds_api_monthly_budget=300,
        api_sports_daily_budget=50,
        the_odds_api_key="secret-key",  # pragma: allowlist secret - dummy, asserted absent
    )
    where = StatusSources.from_settings(settings, coverage_file=tmp_path / "c.json")
    assert (where.odds_monthly_budget, where.api_sports_daily_budget) == (300, 50)
    assert where.archive_root == tmp_path and where.coverage_file == tmp_path / "c.json"
    assert "secret-key" not in repr(where)


# --- the page and its routes --------------------------------------------------------


def test_page_is_self_contained_and_builds_nodes_without_markup():
    assert "innerHTML" not in PAGE and "outerHTML" not in PAGE
    assert re.search(r"""(src|href)\s*=\s*["']?https?:""", PAGE) is None
    assert 'fetch("/api/status"' in PAGE
    assert PAGE.isascii()


@pytest.mark.parametrize(
    ("header", "allowed"),
    [
        ("127.0.0.1:8765", True),
        ("localhost:51234", True),
        ("LOCALHOST", True),
        ("[::1]:8765", True),
        ("evil.example:8765", False),
        ("127.0.0.1.evil.example", False),
        ("[::2]:8765", False),
        ("", False),
        (None, False),
    ],
)
def test_loopback_host_accepts_only_loopback_names(header, allowed):
    assert loopback_host(header) is allowed


@pytest.fixture
def dashboard_server():
    def serve(status):
        server = OverlayServer(("127.0.0.1", 0), lines=list, status=status)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        started.append(server)
        return server.server_port

    started: list[OverlayServer] = []
    yield serve
    for server in started:
        server.shutdown()
        server.server_close()


def get(port: int, path: str, host: str | None = None) -> http.client.HTTPResponse:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {"Host": host} if host else {}
    connection.request("GET", path, headers=headers)
    response = connection.getresponse()
    response.read()
    connection.close()
    return response


def get_body(port: int, path: str) -> tuple[http.client.HTTPResponse, bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        return response, response.read()
    finally:
        connection.close()


def test_service_serves_the_page_and_the_status(dashboard_server):
    port = dashboard_server(lambda: {"generated_at": NOW, "ok": True})

    page, body = get_body(port, "/")
    assert page.status == 200
    assert page.getheader("Content-Type") == "text/html; charset=utf-8"
    assert "default-src 'none'" in page.getheader("Content-Security-Policy")
    assert page.getheader("X-Content-Type-Options") == "nosniff"
    assert body.decode() == PAGE

    status, body = get_body(port, "/api/status?t=1")
    assert status.status == 200
    assert json.loads(body) == {"generated_at": NOW.isoformat(), "ok": True}
    assert get_body(port, "/health?x=1")[0].status == 200


def test_service_refuses_the_dashboard_under_a_foreign_host(dashboard_server):
    port = dashboard_server(lambda: {"ok": True})
    assert get(port, "/", host="rebound.example").status == 421
    assert get(port, "/api/status", host="rebound.example").status == 421
    assert get(port, "/", host="localhost:8765").status == 200


def test_service_without_a_status_source_serves_no_dashboard(dashboard_server):
    port = dashboard_server(None)
    assert get(port, "/").status == 404
    assert get(port, "/api/status").status == 404
    assert get(port, "/health").status == 200


def test_cli_overlay_serve_hands_the_server_a_dashboard(monkeypatch, tmp_path, capsys):
    seen: dict = {}

    class FakeServer:
        server_port = 8765

        def __init__(self, address, **kwargs):
            seen.update(kwargs, address=address)

        def serve_forever(self):
            raise KeyboardInterrupt

        def server_close(self):
            seen["closed"] = True

    settings = Settings(_env_file=None, archive_root=tmp_path / "archive")
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cli, "OverlayServer", FakeServer)
    monkeypatch.setattr(cli, "COVERAGE_PATH", tmp_path / "coverage.json")
    monkeypatch.chdir(tmp_path)

    assert cli.main(["overlay-serve"]) == 0

    assert seen["closed"] is True
    payload = seen["status"]()
    assert payload["fair_lines"]["total"] == 0
    assert payload["coverage"]["artifact"] == str(tmp_path / "coverage.json")
    assert "dashboard: http://127.0.0.1:8765/" in capsys.readouterr().out
