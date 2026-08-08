"""Single-writer scheduled collection jobs with free-tier-safe request pacing."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.blocking import BlockingScheduler

from sports_betting.archive import EventArchive, OddsArchive
from sports_betting.config import Settings, get_settings
from sports_betting.health import HealthStore
from sports_betting.historical import HistoricalImporters
from sports_betting.providers import (
    BallDontLieClient,
    FootballDataClient,
    OddsApiClient,
    TheSportsDbClient,
)
from sports_betting.throttle import ProviderThrottle, QuotaLedger

ODDS_DAILY_SAFETY_LIMIT = 20  # provider free limit is 25; reserve five for manual diagnostics


@dataclass(frozen=True)
class JobOutcome:
    status: str
    fetched: int = 0
    added: int = 0
    detail: str = ""


def _partial_outcome(fetched: int, added: int, failures: list[str]) -> JobOutcome:
    """One unavailable sport must not discard the sports that did collect.

    A single try/except around the whole sport loop reported `error` with 0/0 even when
    most sports had already been written, which hid a permanent per-sport outage.
    """
    if not failures:
        return JobOutcome("ok", fetched, added)
    status = "error" if fetched == 0 and added == 0 else "partial"
    return JobOutcome(status, fetched, added, detail="; ".join(failures))


class CollectionJobs:
    """Provider jobs. The scheduler serializes calls so catalog writes never race."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.events = EventArchive(settings.archive_root)
        self.odds = OddsArchive(settings.archive_root)
        self.health = HealthStore(settings.scheduler_health_file)
        # Carry a previous process's history across the restart; see HealthStore.seed.
        self.health.seed()
        ledger = QuotaLedger(settings.provider_quota_file)
        self.football_gate = ProviderThrottle("football-data.org", min_interval_seconds=7)
        self.thesportsdb_gate = ProviderThrottle("thesportsdb", min_interval_seconds=2)
        self.balldontlie_gate = ProviderThrottle("balldontlie", min_interval_seconds=13)
        self.odds_gate = ProviderThrottle(
            "the-odds-api",
            min_interval_seconds=2,
            ledger=ledger,
            daily_limit=ODDS_DAILY_SAFETY_LIMIT,
        )
        self.bulk_gate = ProviderThrottle(
            "historical-bulk",
            min_interval_seconds=settings.bulk_request_interval_seconds,
        )

    @staticmethod
    def collection_days(today: date | None = None) -> tuple[date, date]:
        current = today or datetime.now(UTC).date()
        return current - timedelta(days=1), current

    def football_data(self) -> JobOutcome:
        if not self.settings.football_data_api_key:
            return self._finish("football-data", JobOutcome("skipped", detail="API key not set"))

        def collect() -> JobOutcome:
            fetched = added = 0
            with FootballDataClient(
                self.settings.football_data_api_key,
                timeout_seconds=self.settings.sportsdb_timeout_seconds,
                before_request=self.football_gate,
            ) as client:
                for day in self.collection_days():
                    snapshots = client.fetch_day(day)
                    result = self.events.write(snapshots)
                    fetched += len(snapshots)
                    added += result.snapshots_added
            return JobOutcome("ok", fetched, added)

        return self._run("football-data", collect)

    def thesportsdb(self) -> JobOutcome:
        def collect() -> JobOutcome:
            fetched = added = 0
            failures: list[str] = []
            with TheSportsDbClient(
                self.settings.sportsdb_api_key,
                timeout_seconds=self.settings.sportsdb_timeout_seconds,
                before_request=self.thesportsdb_gate,
            ) as client:
                for sport in self.settings.csv(self.settings.sportsdb_sports):
                    try:
                        for day in self.collection_days():
                            snapshots = client.fetch_day(day, sport=sport)
                            result = self.events.write(snapshots)
                            fetched += len(snapshots)
                            added += result.snapshots_added
                    except Exception as exc:
                        failures.append(f"{sport}: {type(exc).__name__}: {exc}")
            return _partial_outcome(fetched, added, failures)

        return self._run("thesportsdb", collect)

    def balldontlie(self) -> JobOutcome:
        if not self.settings.balldontlie_api_key:
            return self._finish("balldontlie", JobOutcome("skipped", detail="API key not set"))

        def collect() -> JobOutcome:
            fetched = added = 0
            failures: list[str] = []
            with BallDontLieClient(
                self.settings.balldontlie_api_key,
                timeout_seconds=self.settings.sportsdb_timeout_seconds,
                before_request=self.balldontlie_gate,
            ) as client:
                for sport in self.settings.csv(self.settings.balldontlie_sports):
                    try:
                        for day in self.collection_days():
                            snapshots = client.fetch_day(day, sport=sport)
                            result = self.events.write(snapshots)
                            fetched += len(snapshots)
                            added += result.snapshots_added
                    except Exception as exc:
                        failures.append(f"{sport}: {type(exc).__name__}: {exc}")
            return _partial_outcome(fetched, added, failures)

        return self._run("balldontlie", collect)

    def the_odds_api(self) -> JobOutcome:
        if not self.settings.the_odds_api_key:
            return self._finish("the-odds-api", JobOutcome("skipped", detail="API key not set"))

        def collect() -> JobOutcome:
            fetched = added = 0
            failures: list[str] = []
            with OddsApiClient(
                self.settings.the_odds_api_key,
                regions=self.settings.the_odds_api_regions,
                timeout_seconds=self.settings.sportsdb_timeout_seconds,
                before_request=self.odds_gate,
            ) as client:
                for sport in self.settings.csv(self.settings.the_odds_api_sports):
                    try:
                        snapshots = client.fetch(sport)
                    except Exception as exc:
                        failures.append(f"{sport}: {type(exc).__name__}: {exc}")
                        continue
                    result = self.odds.write(snapshots)
                    fetched += len(snapshots)
                    added += result.snapshots_added
            return _partial_outcome(fetched, added, failures)

        return self._run("the-odds-api", collect)

    def historical_bulk(self, today: date | None = None) -> JobOutcome:
        if not self.settings.bulk_refresh_enabled:
            return self._finish(
                "historical-bulk", JobOutcome("skipped", detail="bulk refresh not enabled")
            )

        def collect() -> JobOutcome:
            current = today or datetime.now(UTC).date()
            soccer_year = current.year if current.month >= 7 else current.year - 1
            football_year = current.year if current.month >= 7 else current.year - 1
            hockey_year = current.year if current.month >= 9 else current.year - 1
            summaries = []
            selected = set(self.settings.csv(self.settings.bulk_refresh_sources))
            with HistoricalImporters(
                self.settings.archive_root,
                before_request=self.bulk_gate,
                max_download_bytes=self.settings.bulk_max_download_bytes,
            ) as importers:
                if "football-data" in selected:
                    summaries.append(
                        importers.football_data(
                            start_year=soccer_year,
                            end_year=soccer_year,
                            leagues=self.settings.csv(self.settings.bulk_football_leagues),
                        )
                    )
                if "nflverse" in selected:
                    summaries.append(
                        importers.nflverse_pbp(start_year=football_year, end_year=football_year)
                    )
                if "moneypuck" in selected:
                    summaries.append(
                        importers.moneypuck_shots(start_year=hockey_year, end_year=hockey_year)
                    )
                if "statsbomb" in selected:
                    for target in self.settings.csv(self.settings.statsbomb_refresh_targets):
                        competition_id, season_id = (int(value) for value in target.split(":"))
                        summaries.append(
                            importers.statsbomb(competition_id=competition_id, season_id=season_id)
                        )
            attempted = sum(summary.attempted for summary in summaries)
            added = sum(summary.added for summary in summaries)
            return JobOutcome("ok", attempted, added, detail=f"{len(summaries)} datasets")

        return self._run("historical-bulk", collect)

    def run_all(self) -> dict[str, JobOutcome]:
        outcomes = {
            "football-data": self.football_data(),
            "thesportsdb": self.thesportsdb(),
            "balldontlie": self.balldontlie(),
            "the-odds-api": self.the_odds_api(),
        }
        if self.settings.bulk_refresh_enabled:
            outcomes["historical-bulk"] = self.historical_bulk()
        return outcomes

    def _run(self, name: str, function) -> JobOutcome:
        # Only the job body is guarded. Recording used to sit inside this try, so a failure
        # while writing the artifact re-entered _finish from the except branch and raised a
        # second time, killing the APScheduler job outright.
        try:
            outcome = function()
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"
            return self._finish(name, JobOutcome("error", detail=detail), exc)
        return self._finish(name, outcome)

    def _finish(
        self, name: str, outcome: JobOutcome, exc: BaseException | None = None
    ) -> JobOutcome:
        self.health.record(name, outcome, exc)
        return outcome


def build_scheduler(settings: Settings | None = None) -> BlockingScheduler:
    resolved = settings or get_settings()
    jobs = CollectionJobs(resolved)
    scheduler = BlockingScheduler(
        timezone="UTC",
        executors={"default": ThreadPoolExecutor(max_workers=1)},
        job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 3600},
    )
    interval = resolved.collection_interval_hours
    for name, minute, function in (
        ("football-data", 5, jobs.football_data),
        ("thesportsdb", 10, jobs.thesportsdb),
        ("balldontlie", 20, jobs.balldontlie),
        ("the-odds-api", 30, jobs.the_odds_api),
    ):
        scheduler.add_job(
            function,
            "interval",
            hours=interval,
            id=name,
            name=name,
            next_run_time=datetime.now(UTC) + timedelta(seconds=minute),
        )
        jobs.health.record_schedule(name, interval * 3600)
    if resolved.bulk_refresh_enabled:
        scheduler.add_job(
            jobs.historical_bulk,
            "interval",
            weeks=1,
            id="historical-bulk",
            name="historical-bulk",
            next_run_time=datetime.now(UTC) + timedelta(minutes=45),
        )
        jobs.health.record_schedule("historical-bulk", 7 * 24 * 3600)
    return scheduler


def serve(settings: Settings | None = None) -> None:
    build_scheduler(settings).start()
