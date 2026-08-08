"""Command-line entry point for data ingestion."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from sports_betting.archive import EventArchive
from sports_betting.config import get_settings
from sports_betting.health import HealthStore
from sports_betting.historical import HistoricalImporters
from sports_betting.pipeline import ingest_events
from sports_betting.providers import TheSportsDbClient
from sports_betting.scheduler import CollectionJobs, serve

REPORT_PATH = Path("logs/ingest-events.json")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sports-betting")
    subparsers = parser.add_subparsers(dest="command", required=True)
    ingest = subparsers.add_parser("ingest-events", help="archive schedules and results")
    ingest.add_argument("--date", type=date.fromisoformat, help="one ISO date (default: yesterday)")
    ingest.add_argument("--from", dest="start", type=date.fromisoformat)
    ingest.add_argument("--to", dest="end", type=date.fromisoformat)
    ingest.add_argument("--sport", help="provider sport filter, e.g. Ice Hockey")
    ingest.add_argument("--league", help="provider league ID or name filter")
    collect = subparsers.add_parser("collect", help="run scheduled collectors once")
    collect.add_argument(
        "--provider",
        choices=(
            "all",
            "football-data",
            "thesportsdb",
            "balldontlie",
            "the-odds-api",
            "historical-bulk",
        ),
        default="all",
    )
    subparsers.add_parser("serve", help="run the persistent free-tier collection scheduler")
    health = subparsers.add_parser("health", help="is collection actually pulling data?")
    health.add_argument(
        "--quiet", action="store_true", help="print only the problem jobs, not every job"
    )
    bulk = subparsers.add_parser("bulk-import", help="import free historical data dumps")
    bulk_sources = bulk.add_subparsers(dest="bulk_source", required=True)
    football = bulk_sources.add_parser("football-data", help="soccer results and bookmaker odds")
    _add_year_range(football, default_start=2000)
    football.add_argument("--leagues", default="E0,D1,I1,SP1,F1")
    nfl = bulk_sources.add_parser("nflverse", help="NFL play-by-play Parquet")
    _add_year_range(nfl, default_start=1999)
    money = bulk_sources.add_parser("moneypuck", help="NHL shot-level ZIP files")
    _add_year_range(money, default_start=2007)
    bulk_sources.add_parser("moneypuck-games", help="all NHL team game-level data")
    statsbomb = bulk_sources.add_parser("statsbomb", help="soccer event data")
    statsbomb.add_argument("--competition-id", type=int, required=True)
    statsbomb.add_argument("--season-id", type=int, required=True)
    statsbomb.add_argument("--max-matches", type=int)
    subparsers.add_parser("statsbomb-list", help="list importable competition and season IDs")
    return parser


def _add_year_range(parser: argparse.ArgumentParser, *, default_start: int) -> None:
    # A source's current-season artifact may not exist yet (especially before the
    # first game), so the no-argument backfill ends at the last completed season.
    current = datetime.now(UTC).year - 1
    parser.add_argument("--from-season", type=int, default=default_start)
    parser.add_argument("--to-season", type=int, default=current)


def _date_range(args: argparse.Namespace) -> tuple[date, date]:
    if args.date and (args.start or args.end):
        raise ValueError("use --date or --from/--to, not both")
    if bool(args.start) != bool(args.end):
        raise ValueError("--from and --to must be supplied together")
    if args.date:
        return args.date, args.date
    if args.start:
        return args.start, args.end
    yesterday = datetime.now(UTC).date() - timedelta(days=1)
    return yesterday, yesterday


def _write_report(payload: dict) -> None:
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _age(stamp: str | None, *, now: datetime | None = None) -> str:
    """Human "how long ago", because a raw ISO timestamp does not answer 'is this stale'."""
    if not stamp:
        return "never"
    try:
        moment = datetime.fromisoformat(str(stamp))
    except ValueError:
        return "unknown"
    seconds = int(((now or datetime.now(UTC)) - moment).total_seconds())
    if seconds < 0:
        return "just now"
    for size, unit in ((86400, "d"), (3600, "h"), (60, "m")):
        if seconds >= size:
            return f"{seconds // size}{unit} ago"
    return f"{seconds}s ago"


def health_lines(report: dict, *, quiet: bool = False, now: datetime | None = None) -> list[str]:
    """Render a health report as terminal lines, worst first."""
    order = {"failing": 0, "stale": 1, "degraded": 2, "idle": 3, "never-run": 4, "skipped": 5}
    rows = sorted(report["jobs"].items(), key=lambda row: (order.get(row[1]["status"], 9), row[0]))
    lines = []
    for name, entry in rows:
        status = entry["status"]
        if quiet and status == "ok":
            continue
        lines.append(
            f"{name:<16} {status:<10} "
            f"last ok {_age(entry.get('last_success'), now=now):<10} "
            f"wrote {_age(entry.get('last_wrote'), now=now):<10} "
            f"runs {entry.get('runs', 0)} fail {entry.get('failures', 0)}"
        )
        reason = entry.get("last_error") or entry.get("skip_reason")
        if reason and status != "ok":
            lines.append(f"{'':<16} └─ {str(reason)[:160]}")
    if not lines:
        lines.append("all jobs ok")
    return lines


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "serve":
            serve()
            return 0
        if args.command == "health":
            settings = get_settings()
            store = HealthStore(settings.scheduler_health_file)
            if not store.seed():
                sys.stdout.write(
                    f"health: no readable artifact at {settings.scheduler_health_file}; "
                    "has the scheduler run?\n"
                )
                return 1
            report = store.report()
            sys.stdout.write("\n".join(health_lines(report, quiet=args.quiet)) + "\n")
            sys.stdout.write(f"artifact: {settings.scheduler_health_file}\n")
            return 0 if report["ok"] else 1
        if args.command == "collect":
            jobs = CollectionJobs(get_settings())
            function = {
                "football-data": jobs.football_data,
                "thesportsdb": jobs.thesportsdb,
                "balldontlie": jobs.balldontlie,
                "the-odds-api": jobs.the_odds_api,
                "historical-bulk": jobs.historical_bulk,
            }
            outcomes = (
                jobs.run_all()
                if args.provider == "all"
                else {args.provider: function[args.provider]()}
            )
            payload = {
                "ok": all(outcome.status != "error" for outcome in outcomes.values()),
                "jobs": {name: asdict(outcome) for name, outcome in outcomes.items()},
            }
            _write_report(payload)
            sys.stdout.write(json.dumps(payload, sort_keys=True) + "\n")
            return 0 if payload["ok"] else 1

        if args.command in {"bulk-import", "statsbomb-list"}:
            settings = get_settings()
            from sports_betting.throttle import ProviderThrottle

            gate = ProviderThrottle(
                "historical-bulk", min_interval_seconds=settings.bulk_request_interval_seconds
            )
            with HistoricalImporters(
                settings.archive_root,
                before_request=gate,
                max_download_bytes=settings.bulk_max_download_bytes,
            ) as importers:
                if args.command == "statsbomb-list":
                    competitions = importers.statsbomb_competitions()
                    payload = {"ok": True, "competitions": competitions}
                else:
                    bulk_function = {
                        "football-data": lambda: importers.football_data(
                            start_year=args.from_season,
                            end_year=args.to_season,
                            leagues=settings.csv(args.leagues),
                        ),
                        "nflverse": lambda: importers.nflverse_pbp(
                            start_year=args.from_season, end_year=args.to_season
                        ),
                        "moneypuck": lambda: importers.moneypuck_shots(
                            start_year=args.from_season, end_year=args.to_season
                        ),
                        "moneypuck-games": importers.moneypuck_games,
                        "statsbomb": lambda: importers.statsbomb(
                            competition_id=args.competition_id,
                            season_id=args.season_id,
                            max_matches=args.max_matches,
                        ),
                    }
                    summary = bulk_function[args.bulk_source]()
                    payload = {"ok": True, **asdict(summary)}
            _write_report(payload)
            sys.stdout.write(json.dumps(payload, default=str, sort_keys=True) + "\n")
            return 0

        start, end = _date_range(args)
        settings = get_settings()
        with TheSportsDbClient(
            settings.sportsdb_api_key,
            timeout_seconds=settings.sportsdb_timeout_seconds,
        ) as provider:
            result = ingest_events(
                provider,
                EventArchive(settings.archive_root),
                start=start,
                end=end,
                sport=args.sport,
                league=args.league,
            )
        payload = {"ok": True, **asdict(result), "archive_root": str(settings.archive_root)}
        _write_report(payload)
        sys.stdout.write(json.dumps(payload, default=list, sort_keys=True) + "\n")
        return 0
    except Exception as exc:
        payload = {"ok": False, "error_type": type(exc).__name__, "error": str(exc)}
        _write_report(payload)
        sys.stdout.write(f"{args.command}: FAILED — details in {REPORT_PATH}\n")
        return 1
