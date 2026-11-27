"""Assemble the dashboard's JSON from the artifacts collection already writes.

Read-only and best-effort: a missing or unreadable artifact becomes an empty section with
a reason, never an exception, because a dashboard that 500s on a half-written ledger hides
exactly the state it exists to show. Nothing secret is copied out: of the settings only the
budgets and file paths are read, and a job's traceback stays in its artifact.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sports_betting.archive.manifest import CATALOG_DIRNAME
from sports_betting.config import Settings
from sports_betting.health import HealthStore
from sports_betting.overlay.coverage import load as load_coverage
from sports_betting.overlay.coverage import summarize
from sports_betting.overlay.lines import FairLine

#: Worst first, matching `sports-betting health`'s ordering.
STATUS_ORDER = ("failing", "stale", "degraded", "idle", "never-run", "skipped", "ok")

#: Longest job reason the page shows; the full text is in the health artifact.
MAX_REASON_CHARS = 240

#: The ledger keys the scheduler claims against (`sports_betting/scheduler.py`).
ODDS_API_LEDGER_KEY = "the-odds-api"
API_SPORTS_LEDGER_KEY = "api-sports-football"


@dataclass(frozen=True)
class StatusSources:
    """Where each artifact lives, and the budgets the ledger is measured against."""

    health_file: Path
    quota_file: Path
    coverage_file: Path
    archive_root: Path
    odds_monthly_budget: int
    api_sports_daily_budget: int

    @classmethod
    def from_settings(cls, settings: Settings, *, coverage_file: Path) -> StatusSources:
        return cls(
            health_file=settings.scheduler_health_file,
            quota_file=settings.provider_quota_file,
            coverage_file=coverage_file,
            archive_root=settings.archive_root,
            odds_monthly_budget=settings.the_odds_api_monthly_budget,
            api_sports_daily_budget=settings.api_sports_daily_budget,
        )


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value else None


def _rank(status: str) -> int:
    return STATUS_ORDER.index(status) if status in STATUS_ORDER else len(STATUS_ORDER)


def job_rows(report: dict[str, Any]) -> list[dict[str, Any]]:
    """One row per job, worst status first, with the reason trimmed and no traceback."""
    rows = []
    for name, entry in report.get("jobs", {}).items():
        reason = entry.get("last_error") or entry.get("skip_reason") or entry.get("last_detail")
        rows.append(
            {
                "job": name,
                "status": entry.get("status", "unknown"),
                "last_run": entry.get("last_run"),
                "last_success": entry.get("last_success"),
                "last_wrote": entry.get("last_wrote"),
                "runs": entry.get("runs", 0),
                "failures": entry.get("failures", 0),
                "consecutive_failures": entry.get("consecutive_failures", 0),
                "last_fetched": entry.get("last_fetched", 0),
                "last_added": entry.get("last_added", 0),
                "interval_seconds": entry.get("interval_seconds"),
                "reason": str(reason)[:MAX_REASON_CHARS] if reason else None,
            }
        )
    return sorted(rows, key=lambda row: (_rank(row["status"]), row["job"]))


def health_section(path: Path, *, now: datetime) -> dict[str, Any]:
    """The scheduler's per-job health, or why it could not be read."""
    store = HealthStore(path)
    if not store.seed():
        return {
            "readable": False,
            "artifact": str(path),
            "ok": False,
            "problems": [],
            "jobs": [],
            "error": "no readable scheduler health artifact; has `serve` run?",
        }
    report = store.report(now=now)
    return {
        "readable": True,
        "artifact": str(path),
        "ok": report["ok"],
        "problems": report["problems"],
        "jobs": job_rows(report),
    }


def _read_json(path: Path) -> tuple[dict[str, Any], str | None]:
    """A JSON object from `path`, empty when the file is absent; an error when unreadable."""
    if not path.is_file():
        return {}, None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {}, f"unreadable: {type(exc).__name__}"
    if not isinstance(payload, dict):
        return {}, "not a JSON object"
    return payload, None


def quota_rows(payload: dict[str, Any], sources: StatusSources, *, now: datetime) -> list[dict]:
    """Spend against each persistent budget, zero once its UTC day or month has rolled over.

    Mirrors `QuotaLedger._current`: a ledger stamped with an earlier day or month has not
    been written since the boundary, so nothing has been spent in the current period yet.
    """
    moment = now.astimezone(UTC)
    today, month = moment.date().isoformat(), moment.strftime("%Y-%m")
    daily = payload.get("providers") if payload.get("date") == today else None
    monthly = payload.get("monthly") if payload.get("month") == month else None
    daily = daily if isinstance(daily, dict) else {}
    monthly = monthly if isinstance(monthly, dict) else {}
    return [
        {
            "key": ODDS_API_LEDGER_KEY,
            "label": "The Odds API credits",
            "period": f"month {month}",
            "used": int(monthly.get(ODDS_API_LEDGER_KEY, 0)),
            "limit": sources.odds_monthly_budget,
        },
        {
            "key": API_SPORTS_LEDGER_KEY,
            "label": "API-Sports requests",
            "period": f"day {today} (UTC)",
            "used": int(daily.get(API_SPORTS_LEDGER_KEY, 0)),
            "limit": sources.api_sports_daily_budget,
        },
    ]


def quota_section(sources: StatusSources, *, now: datetime) -> dict[str, Any]:
    payload, error = _read_json(sources.quota_file)
    return {
        "artifact": str(sources.quota_file),
        "error": error,
        "rows": quota_rows(payload, sources, now=now),
    }


def fair_line_section(lines: list[FairLine], *, now: datetime) -> dict[str, Any]:
    """How many upcoming games the overlay can price, and how fresh those prices are."""
    by_sport = Counter(line.sport for line in lines)
    upcoming = [line.start for line in lines if line.start >= now]
    return {
        "total": len(lines),
        "sharp": sum(1 for line in lines if line.sharp),
        "as_of": _iso(max((line.observed_at for line in lines), default=None)),
        "next_start": _iso(min(upcoming, default=None)),
        "by_sport": [
            {"sport": sport, "lines": count}
            for sport, count in sorted(by_sport.items(), key=lambda item: (-item[1], item[0]))
        ],
    }


def coverage_section(path: Path) -> dict[str, Any]:
    """Browsed Mise-o-jeu+ events per sport, and how many had a fair line."""
    rows = summarize(load_coverage(path))
    by_sport: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for row in rows:
        by_sport[row["sport"]][0] += row["events"]
        by_sport[row["sport"]][1] += row["priced"]
    return {
        "artifact": str(path),
        "events": sum(row["events"] for row in rows),
        "priced": sum(row["priced"] for row in rows),
        "by_sport": [
            {"sport": sport, "events": events, "priced": priced}
            for sport, (events, priced) in sorted(
                by_sport.items(), key=lambda item: (-item[1][0], item[0])
            )
        ],
    }


def dataset_row(manifest: dict[str, Any]) -> dict[str, Any] | None:
    """One catalog manifest as a row, or None when it is not in the shared shape."""
    partitions = manifest.get("partitions")
    if not isinstance(manifest.get("dataset"), str) or not isinstance(partitions, dict):
        return None
    entries = [entry for entry in partitions.values() if isinstance(entry, dict)]
    newest = [str(entry["max_ts"]) for entry in entries if entry.get("max_ts")]
    return {
        "dataset": manifest["dataset"],
        "partitions": len(entries),
        "rows": sum(int(entry.get("rows", 0) or 0) for entry in entries),
        "newest": max(newest, default=None),
        "updated_at": manifest.get("updated_at"),
    }


def dataset_section(archive_root: Path) -> dict[str, Any]:
    """Every dataset `_catalog/` declares, largest first."""
    catalog = Path(archive_root, CATALOG_DIRNAME)
    rows: list[dict[str, Any]] = []
    unreadable: list[str] = []
    for path in sorted(catalog.glob("*.json")) if catalog.is_dir() else []:
        manifest, error = _read_json(path)
        row = None if error else dataset_row(manifest)
        if row is None:
            unreadable.append(path.name)
        else:
            rows.append(row)
    rows.sort(key=lambda row: (-row["rows"], row["dataset"]))
    return {
        "catalog": str(catalog),
        "present": catalog.is_dir(),
        "rows": rows,
        "unreadable": unreadable,
    }


def build_status(
    sources: StatusSources, lines: list[FairLine], *, now: datetime | None = None
) -> dict[str, Any]:
    """The whole dashboard payload. Never raises on a missing or malformed artifact."""
    moment = now or datetime.now(UTC)
    return {
        "generated_at": moment.astimezone(UTC).isoformat(),
        "health": health_section(sources.health_file, now=moment),
        "quotas": quota_section(sources, now=moment),
        "fair_lines": fair_line_section(lines, now=moment),
        "coverage": coverage_section(sources.coverage_file),
        "datasets": dataset_section(sources.archive_root),
    }
