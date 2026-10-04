"""Command-line entry point for data ingestion."""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from sports_betting.archive import ArchiveSync, EventArchive, restore_sources, store_for
from sports_betting.archive.recatalog import rebuild_catalogs
from sports_betting.archive.sync import DEFAULT_MAX_OBJECT_BYTES
from sports_betting.config import Settings, get_settings
from sports_betting.health import HealthStore
from sports_betting.historical import (
    FOOTBALL_DATA_EXTRA_COUNTRIES,
    TENNIS_DATA_FIRST_XLSX_YEAR,
    BulkImportSummary,
    HistoricalImporters,
)
from sports_betting.overlay.coverage import DEFAULT_PATH as COVERAGE_PATH
from sports_betting.overlay.coverage import CoverageTracker, coverage_lines, summarize
from sports_betting.overlay.coverage import load as load_coverage
from sports_betting.overlay.lines import load_fair_lines
from sports_betting.overlay.server import DEFAULT_MIN_EDGE as OVERLAY_MIN_EDGE
from sports_betting.overlay.server import DEFAULT_PORT as OVERLAY_PORT
from sports_betting.overlay.server import HOST as OVERLAY_HOST
from sports_betting.overlay.server import CachedLines, OverlayServer
from sports_betting.pipeline import ingest_events
from sports_betting.providers import TheSportsDbClient
from sports_betting.providers.api_sports import MIN_INTERVAL_SECONDS as API_SPORTS_INTERVAL
from sports_betting.providers.api_sports import probe_football
from sports_betting.scheduler import CollectionJobs, serve

REPORT_PATH = Path("logs/ingest-events.json")
ARCHIVE_REPORT_PATH = Path("logs/archive-ops.json")
API_SPORTS_PROBE_PATH = Path("logs/api-sports-probe.json")


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
            "api-sports",
            "historical-bulk",
        ),
        default="all",
    )
    subparsers.add_parser("serve", help="run the persistent free-tier collection scheduler")
    _add_overlay_parsers(subparsers)
    health = subparsers.add_parser("health", help="is collection actually pulling data?")
    health.add_argument(
        "--quiet", action="store_true", help="print only the problem jobs, not every job"
    )
    _add_bulk_parsers(subparsers)
    sync = subparsers.add_parser(
        "archive-sync", help="mirror the bronze archive to the shared object store"
    )
    sync.add_argument(
        "--prune",
        action="store_true",
        help="delete each verified local source artifact (never the Parquet)",
    )
    sync.add_argument(
        "--dry-run", action="store_true", help="report what would be uploaded and freed"
    )
    sync.add_argument(
        "--max-object-mb",
        type=int,
        default=DEFAULT_MAX_OBJECT_BYTES // (1024 * 1024),
        help="skip objects above this size (mirroring peaks at ~2x an object's size)",
    )
    subparsers.add_parser("archive-restore", help="pull pruned source artifacts back locally")
    subparsers.add_parser(
        "archive-recatalog", help="rewrite _catalog manifests into the shared lake shape"
    )
    return parser


def _add_overlay_parsers(subparsers: argparse._SubParsersAction) -> None:
    """The Mise-o-jeu+ overlay's subcommands, and the API-Sports free-plan probe."""
    overlay = subparsers.add_parser(
        "overlay-serve", help="serve fair-line verdicts to the Mise-o-jeu+ browser overlay"
    )
    overlay.add_argument("--port", type=int, default=OVERLAY_PORT)
    overlay.add_argument(
        "--min-edge",
        type=float,
        default=OVERLAY_MIN_EDGE,
        help="smallest edge flagged as value, e.g. 0.03 for 3%%",
    )
    subparsers.add_parser(
        "overlay-coverage", help="which browsed Mise-o-jeu+ leagues the overlay could price"
    )
    probe = subparsers.add_parser(
        "probe-api-sports", help="check what an API-Sports free key returns (3 requests)"
    )
    probe.add_argument(
        "--date", type=date.fromisoformat, help="day to probe (default: tomorrow, UTC)"
    )


def _add_bulk_parsers(subparsers: argparse._SubParsersAction) -> None:
    """The `bulk-import` subcommand's sources, and the StatsBomb catalogue listing."""
    bulk = subparsers.add_parser("bulk-import", help="import free historical data dumps")
    bulk_sources = bulk.add_subparsers(dest="bulk_source", required=True)
    football = bulk_sources.add_parser("football-data", help="soccer results and bookmaker odds")
    _add_year_range(football, default_start=2000)
    football.add_argument("--leagues", default="E0,D1,I1,SP1,F1")
    extra = bulk_sources.add_parser(
        "football-data-extra", help="soccer results and closing odds, 16 extra countries"
    )
    extra.add_argument("--countries", default=",".join(FOOTBALL_DATA_EXTRA_COUNTRIES))
    tennis = bulk_sources.add_parser("tennis-data", help="ATP/WTA results and bookmaker odds")
    _add_year_range(tennis, default_start=TENNIS_DATA_FIRST_XLSX_YEAR)
    tennis.add_argument("--tours", default="atp,wta")
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


def _write_report(payload: dict, path: Path = REPORT_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def sync_lines(report: dict) -> list[str]:
    """Terminal summary for an archive-sync run: a status line, never the whole outcome list.

    The per-object detail belongs in the artifact — a prune over a full StatsBomb import is
    thousands of rows, and streaming those past an operator buries the one line that matters.
    """
    freed_mb = report["freed_bytes"] / (1024 * 1024)
    verb = "would mirror" if report["dry_run"] else "mirrored"
    lines = [
        f"{verb} {report['uploaded']} object(s) to {report['backend']}; "
        f"{report['already_present']} already present, {report['scanned']} scanned"
    ]
    if report["dry_run"]:
        lines.append(f"planned {report['planned']} object(s)")
    if report["pruned"] or report["freed_bytes"]:
        lines.append(f"pruned {report['pruned']} verified source artifact(s), {freed_mb:.1f} MB")
    for failure in report["failures"][:10]:
        lines.append(f"  {failure['status']:<9} {failure['key']}: {failure['detail'][:120]}")
    if len(report["failures"]) > 10:
        lines.append(f"  … {len(report['failures']) - 10} more, see the artifact")
    return lines


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


def _run_bulk_import(importers: HistoricalImporters, args: argparse.Namespace) -> BulkImportSummary:
    """Run the importer the `bulk-import` subcommand named, with its parsed options."""
    csv = Settings.csv
    bulk_function: dict[str, Callable[[], BulkImportSummary]] = {
        "football-data": lambda: importers.football_data(
            start_year=args.from_season, end_year=args.to_season, leagues=csv(args.leagues)
        ),
        "football-data-extra": lambda: importers.football_data_extra(countries=csv(args.countries)),
        "tennis-data": lambda: importers.tennis_data(
            start_year=args.from_season, end_year=args.to_season, tours=csv(args.tours)
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
    return bulk_function[args.bulk_source]()


def _overlay_serve(args: argparse.Namespace) -> int:
    if not 0 <= args.min_edge < 1:
        raise ValueError("--min-edge is a fraction between 0 and 1, e.g. 0.03")
    root = get_settings().archive_root
    lines = CachedLines(lambda: load_fair_lines(root))
    server = OverlayServer(
        (OVERLAY_HOST, args.port),
        lines=lines,
        min_edge=args.min_edge,
        coverage=CoverageTracker(COVERAGE_PATH),
    )
    count = len(lines())
    sys.stdout.write(
        f"overlay: {count} upcoming fair line(s) from {root}; "
        f"listening on http://{OVERLAY_HOST}:{server.server_port} (Ctrl+C to stop)\n"
    )
    if not count:
        sys.stdout.write(
            "overlay: no lines yet; run `sports-betting collect --provider the-odds-api`\n"
        )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def _serve(_args: argparse.Namespace, _report_path: Path) -> int:
    serve()
    return 0


def _archive_recatalog(_args: argparse.Namespace, report_path: Path) -> int:
    settings = get_settings()
    # Needs no store: manifests are derived from local Parquet, and a checkout with
    # no mirror configured is exactly the one most likely to still hold old-shape
    # manifests. Re-run archive-sync afterwards to push the rewritten copies.
    rebuilt = rebuild_catalogs(settings.archive_root)
    payload = {"ok": True, "rebuilt": rebuilt, "root": str(settings.archive_root)}
    _write_report(payload, report_path)
    sys.stdout.write(f"rebuilt {len(rebuilt)} manifest(s): {', '.join(rebuilt) or '-'}\n")
    sys.stdout.write(f"artifact: {report_path}\n")
    return 0


def _archive_mirror(args: argparse.Namespace, report_path: Path) -> int:
    """`archive-sync` and `archive-restore`: the two directions of the object-store mirror."""
    settings = get_settings()
    object_store = store_for(settings)
    if args.command == "archive-restore":
        sync_summary = restore_sources(settings.archive_root, object_store)
    else:
        sync_summary = ArchiveSync(
            settings.archive_root,
            object_store,
            backend=settings.archive_backend,
            max_object_bytes=args.max_object_mb * 1024 * 1024,
        ).run(prune=args.prune, dry_run=args.dry_run)
    payload = sync_summary.as_dict()
    _write_report(payload, report_path)
    sys.stdout.write("\n".join(sync_lines(payload)) + "\n")
    sys.stdout.write(f"artifact: {report_path}\n")
    return 0 if payload["ok"] else 1


def _health(args: argparse.Namespace, _report_path: Path) -> int:
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


def _collect(args: argparse.Namespace, _report_path: Path) -> int:
    jobs = CollectionJobs(get_settings())
    function = {
        "football-data": jobs.football_data,
        "thesportsdb": jobs.thesportsdb,
        "balldontlie": jobs.balldontlie,
        "the-odds-api": jobs.the_odds_api,
        "api-sports": jobs.api_sports,
        "historical-bulk": jobs.historical_bulk,
    }
    outcomes = (
        jobs.run_all() if args.provider == "all" else {args.provider: function[args.provider]()}
    )
    payload = {
        "ok": all(outcome.status != "error" for outcome in outcomes.values()),
        "jobs": {name: asdict(outcome) for name, outcome in outcomes.items()},
    }
    _write_report(payload)
    sys.stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    return 0 if payload["ok"] else 1


def _bulk(args: argparse.Namespace, _report_path: Path) -> int:
    """`bulk-import` and `statsbomb-list`, which share the throttled importers."""
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
            summary = _run_bulk_import(importers, args)
            payload = {"ok": True, **asdict(summary)}
    _write_report(payload)
    sys.stdout.write(json.dumps(payload, default=str, sort_keys=True) + "\n")
    return 0


def _ingest_events(args: argparse.Namespace, _report_path: Path) -> int:
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


# One handler per subcommand, each taking the parsed args and the artifact path a failure
# is reported to; `main` owns that failure path, so a handler only raises.
_COMMANDS: dict[str, Callable[[argparse.Namespace, Path], int]] = {
    "serve": _serve,
    "overlay-serve": lambda args, _report_path: _overlay_serve(args),
    "archive-recatalog": _archive_recatalog,
    "archive-sync": _archive_mirror,
    "archive-restore": _archive_mirror,
    "health": _health,
    "collect": _collect,
    "bulk-import": _bulk,
    "statsbomb-list": _bulk,
    "ingest-events": _ingest_events,
    "overlay-coverage": lambda _args, _report_path: _overlay_coverage(),
    "probe-api-sports": lambda args, _report_path: _probe_api_sports(args),
}


def _overlay_coverage() -> int:
    sys.stdout.write("\n".join(coverage_lines(summarize(load_coverage(COVERAGE_PATH)))))
    sys.stdout.write(f"\nartifact: {COVERAGE_PATH}\n")
    return 0


def _probe_api_sports(args: argparse.Namespace) -> int:
    day = args.date or datetime.now(UTC).date() + timedelta(days=1)
    result = probe_football(
        get_settings().api_sports_key,
        day,
        pause=lambda: time.sleep(API_SPORTS_INTERVAL),
    )
    _write_report({"ok": result["verdict"] == "usable", **result}, API_SPORTS_PROBE_PATH)
    odds = result["odds"]
    sys.stdout.write(
        f"api-sports {result['plan'] or 'unknown plan'}: verdict {result['verdict']}; "
        f"{result['fixtures']['results']} fixtures and {odds['results']} with odds on "
        f"{result['day']} ({odds['pages'] or 0} page(s))\n"
    )
    for error in result["status_errors"] + result["fixtures"]["errors"] + odds["errors"]:
        sys.stdout.write(f"  {error}\n")
    sys.stdout.write(f"artifact: {API_SPORTS_PROBE_PATH}\n")
    return 0 if result["verdict"] == "usable" else 1


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report_path = ARCHIVE_REPORT_PATH if args.command.startswith("archive-") else REPORT_PATH
    try:
        return _COMMANDS[args.command](args, report_path)
    except Exception as exc:
        payload = {"ok": False, "error_type": type(exc).__name__, "error": str(exc)}
        _write_report(payload, report_path)
        sys.stdout.write(f"{args.command}: FAILED — details in {report_path}\n")
        return 1
