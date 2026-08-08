"""Per-job outcome tracking for `serve`, persisted to a parseable artifact.

The scheduler deliberately swallows a job's exception so one dead provider cannot kill the
long-running process. The cost is that nothing outside the log stream knows a job stopped
working. The previous store kept only the *last* outcome per job, which lost the two facts
that matter most when something breaks:

- **when the job last succeeded.** `the-odds-api` sat at `status: error` against a contract
  that never existed. Because each write clobbered the row, there was no way to tell whether
  it broke yesterday or had never worked once — the artifact looked the same either way.
- **whether the job is still running at all.** A wedged scheduler and a healthy job that has
  simply not fired yet produce byte-identical rows. Recording each job's promised cadence is
  what lets a reader call the difference.

This module is modelled on ibkr_trader's `job_health.py`, which was itself written after a
dead ingestion pipeline hid for six days. Two of its distinctions are carried over deliberately:
`last_wrote` is tracked separately from `last_success`, because a provider that answers 200 OK
while storing nothing is healthy by every other measure; and recording must never be able to
break collection, so a filesystem error while writing is swallowed and the in-memory registry
stays correct regardless of whether the artifact lands.

Unlike ibkr's module-level registry, this store is an instance: the scheduler is single-writer
by construction, one `CollectionJobs` owns one artifact, and tests get isolation for free.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import traceback
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

#: A job is "stale" once it has gone this many times its own interval without a success. Two
#: and a half intervals means a single missed run is not yet an alarm, but two are.
STALE_INTERVAL_FACTOR = 2.5

#: How many consecutive writeless successes count as "idle". A collector may legitimately add
#: nothing when a day has no new fixtures, so one empty run proves nothing; a provider that has
#: answered successfully three times running and stored nothing is not actually collecting.
IDLE_RUN_THRESHOLD = 3


def _blank(job: str) -> dict[str, Any]:
    return {
        "job": job,
        "interval_seconds": None,
        "runs": 0,
        "failures": 0,
        "consecutive_failures": 0,
        "degraded_runs": 0,
        "writeless_successes": 0,
        "last_run": None,
        "last_success": None,
        "last_failure": None,
        # Distinct from last_success on purpose: a run that adds 0 rows has *succeeded* but
        # has not collected anything.
        "last_wrote": None,
        "last_status": None,
        "last_fetched": 0,
        "last_added": 0,
        "last_detail": "",
        "last_error": None,
        "last_traceback": None,
        "skip_reason": None,
    }


def _now() -> str:
    return datetime.now(UTC).isoformat()


class HealthStore:
    """Registry of per-job outcomes, flushed to a JSON artifact after every run."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._registry: dict[str, dict[str, Any]] = {}

    # -- recording ---------------------------------------------------------------------

    def record_schedule(self, job: str, interval_seconds: float) -> None:
        """Declare a job's cadence, so staleness can be judged against what it promised."""
        with self._lock:
            self._entry(job)["interval_seconds"] = interval_seconds
        self.flush()

    def record(self, job: str, outcome: Any, exc: BaseException | None = None) -> None:
        """Fold one `JobOutcome` into the job's history and flush the artifact.

        Never raises: a scheduler that cannot write its health file must still collect.
        """
        with self._lock:
            entry = self._entry(job)
            stamp = _now()
            entry["last_status"] = outcome.status
            entry["last_detail"] = outcome.detail

            if outcome.status == "skipped":
                # Not evidence of health or failure — the job never ran. Leave the counters
                # and every timestamp alone so a missing key cannot mask a real outage.
                entry["skip_reason"] = outcome.detail
                self._write_locked()
                return

            entry["skip_reason"] = None
            entry["runs"] += 1
            entry["last_run"] = stamp
            entry["last_fetched"] = outcome.fetched
            entry["last_added"] = outcome.added

            if outcome.status == "error":
                entry["failures"] += 1
                entry["consecutive_failures"] += 1
                entry["last_failure"] = stamp
                entry["last_error"] = outcome.detail or "job reported error"
                entry["last_traceback"] = (
                    "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
                    if exc is not None
                    else None
                )
                self._write_locked()
                return

            # ok or partial: something collected, so the failure streak is broken.
            entry["consecutive_failures"] = 0
            entry["last_success"] = stamp
            entry["last_traceback"] = None
            if outcome.status == "partial":
                entry["degraded_runs"] += 1
                # A partial run keeps the per-sport failures visible instead of discarding
                # them the moment one sport succeeds.
                entry["last_error"] = outcome.detail
            else:
                entry["last_error"] = None
            if outcome.added > 0:
                entry["last_wrote"] = stamp
                entry["writeless_successes"] = 0
            else:
                entry["writeless_successes"] += 1
            self._write_locked()

    # -- reading -----------------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """The full registry, shaped as it is written to disk."""
        with self._lock:
            return self._snapshot_locked()

    def seed(self) -> bool:
        """Restore a previous process's registry so a restart does not erase job history.

        Without this, every `serve` restart resets the counters and every job reports
        "never-run" until it has fired once — which for a six-hourly job means most of a day
        of a red signal that means nothing. A restart is not evidence that anything collected,
        so the recorded facts (including an open failure streak) carry over unchanged.

        Unknown keys are dropped rather than merged: an artifact written by an older build
        must not inject a shape this code does not expect.
        """
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        jobs = loaded.get("jobs") if isinstance(loaded, dict) else None
        if not isinstance(jobs, dict):
            return False
        with self._lock:
            for name, stored in jobs.items():
                if not isinstance(stored, dict):
                    continue
                entry = _blank(str(name))
                entry.update({key: value for key, value in stored.items() if key in entry})
                entry["job"] = str(name)
                self._registry[str(name)] = entry
        return True

    def report(self, *, now: datetime | None = None) -> dict[str, Any]:
        """Health summary for `sports-betting health`: a status per job plus an overall verdict."""
        snapshot = self.snapshot()
        jobs = {
            name: {**entry, "status": status_for(entry, now=now)}
            for name, entry in snapshot["jobs"].items()
        }
        problems = sorted(
            name for name, entry in jobs.items() if entry["status"] in _UNHEALTHY_STATUSES
        )
        return {
            "ok": not problems,
            "written_at": snapshot["written_at"],
            "artifact": str(self.path),
            "problems": problems,
            "jobs": jobs,
        }

    # -- internals ---------------------------------------------------------------------

    def _entry(self, job: str) -> dict[str, Any]:
        return self._registry.setdefault(job, _blank(job))

    def _snapshot_locked(self) -> dict[str, Any]:
        return {
            "written_at": _now(),
            "jobs": {job: dict(entry) for job, entry in self._registry.items()},
        }

    def flush(self) -> bool:
        """Write the registry atomically. Returns whether it landed; never raises."""
        with self._lock:
            return self._write_locked()

    def _write_locked(self) -> bool:
        payload = json.dumps(self._snapshot_locked(), indent=2, sort_keys=True)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # temp-then-replace so a reader never sees a half-written artifact
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=self.path.name,
                suffix=".tmp",
                delete=False,
            ) as handle:
                handle.write(payload)
                temporary = handle.name
            os.replace(temporary, self.path)
        except OSError:
            # Deliberately swallowed: collection must survive an unwritable logs directory.
            return False
        return True


#: Statuses that make `sports-betting health` exit non-zero.
_UNHEALTHY_STATUSES = frozenset({"failing", "stale", "degraded", "idle"})


def status_for(entry: dict[str, Any], *, now: datetime | None = None) -> str:
    """Classify one job's entry.

    ``failing`` outranks ``stale`` because a job erroring right now is the more actionable
    fact — staleness is usually its consequence. ``stale`` outranks ``degraded`` because a job
    that stopped running matters more than one running with a known-bad sport.
    """
    if entry.get("consecutive_failures"):
        return "failing"
    if not entry.get("runs"):
        return "skipped" if entry.get("skip_reason") else "never-run"
    last_success = entry.get("last_success")
    if not last_success:
        return "never-run"
    if _is_stale(entry, last_success, now):
        return "stale"
    if entry.get("last_status") == "partial":
        return "degraded"
    if entry.get("writeless_successes", 0) >= IDLE_RUN_THRESHOLD:
        return "idle"
    return "ok"


def _is_stale(entry: dict[str, Any], last_success: str, now: datetime | None) -> bool:
    interval = entry.get("interval_seconds")
    if not interval:
        return False
    try:
        succeeded_at = datetime.fromisoformat(str(last_success))
    except ValueError:
        return False
    moment = now or datetime.now(UTC)
    return (moment - succeeded_at).total_seconds() > interval * STALE_INTERVAL_FACTOR


def outcome_as_dict(outcome: Any) -> dict[str, Any]:
    """`JobOutcome` -> plain dict, for CLI payloads."""
    return asdict(outcome)
