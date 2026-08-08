import json
from datetime import UTC, datetime, timedelta

from sports_betting.health import (
    IDLE_RUN_THRESHOLD,
    STALE_INTERVAL_FACTOR,
    HealthStore,
    status_for,
)
from sports_betting.scheduler import JobOutcome


def _store(tmp_path, name="health.json"):
    return HealthStore(tmp_path / name)


def _entry(store, job="collector"):
    return store.snapshot()["jobs"][job]


def test_failure_does_not_erase_when_the_job_last_worked(tmp_path):
    """The old store kept only the last outcome, so an outage erased its own start point."""
    store = _store(tmp_path)

    store.record("collector", JobOutcome("ok", fetched=10, added=10))
    store.record("collector", JobOutcome("error", detail="401 Unauthorized"))

    entry = _entry(store)
    assert entry["last_success"] is not None  # survived the failure
    assert entry["last_wrote"] is not None
    assert entry["last_failure"] is not None
    assert entry["last_error"] == "401 Unauthorized"
    assert (entry["runs"], entry["failures"], entry["consecutive_failures"]) == (2, 1, 1)


def test_consecutive_failures_accumulate_and_reset_on_success(tmp_path):
    store = _store(tmp_path)

    for _ in range(3):
        store.record("collector", JobOutcome("error", detail="boom"))
    assert _entry(store)["consecutive_failures"] == 3
    assert status_for(_entry(store)) == "failing"

    store.record("collector", JobOutcome("ok", fetched=1, added=1))
    entry = _entry(store)
    assert entry["consecutive_failures"] == 0
    assert entry["failures"] == 3  # the lifetime count is not rewritten by one good run
    assert status_for(entry) == "ok"


def test_skipped_job_is_not_a_run_and_cannot_mask_a_failure(tmp_path):
    store = _store(tmp_path)

    store.record("collector", JobOutcome("error", detail="boom"))
    store.record("collector", JobOutcome("skipped", detail="API key not set"))

    entry = _entry(store)
    assert entry["runs"] == 1  # the skip did not count as a run
    assert entry["consecutive_failures"] == 1  # nor did it clear the open streak
    assert status_for(entry) == "failing"


def test_skip_before_any_run_reads_as_skipped_not_failing(tmp_path):
    store = _store(tmp_path)

    store.record("collector", JobOutcome("skipped", detail="API key not set"))

    entry = _entry(store)
    assert status_for(entry) == "skipped"
    assert entry["skip_reason"] == "API key not set"


def test_partial_is_degraded_and_keeps_the_failing_sport_visible(tmp_path):
    store = _store(tmp_path)

    store.record("collector", JobOutcome("partial", fetched=4, added=2, detail="epl: 401"))

    entry = _entry(store)
    assert status_for(entry) == "degraded"
    assert entry["last_error"] == "epl: 401"
    assert entry["consecutive_failures"] == 0
    assert entry["last_wrote"] is not None


def test_stale_when_a_job_stops_firing(tmp_path):
    store = _store(tmp_path)
    store.record_schedule("collector", 3600)
    store.record("collector", JobOutcome("ok", fetched=1, added=1))
    entry = _entry(store)

    fresh = datetime.now(UTC) + timedelta(seconds=3600 * STALE_INTERVAL_FACTOR - 60)
    overdue = datetime.now(UTC) + timedelta(seconds=3600 * STALE_INTERVAL_FACTOR + 60)

    assert status_for(entry, now=fresh) == "ok"
    assert status_for(entry, now=overdue) == "stale"


def test_a_job_with_no_declared_cadence_is_never_stale(tmp_path):
    store = _store(tmp_path)
    store.record("collector", JobOutcome("ok", fetched=1, added=1))

    far_future = datetime.now(UTC) + timedelta(days=365)
    assert status_for(_entry(store), now=far_future) == "ok"


def test_repeated_successes_that_write_nothing_read_as_idle(tmp_path):
    """A provider answering 200 OK while storing nothing is healthy by every other measure."""
    store = _store(tmp_path)

    for _ in range(IDLE_RUN_THRESHOLD - 1):
        store.record("collector", JobOutcome("ok", fetched=5, added=0))
    assert status_for(_entry(store)) == "ok"  # one empty day proves nothing

    store.record("collector", JobOutcome("ok", fetched=5, added=0))
    assert status_for(_entry(store)) == "idle"

    store.record("collector", JobOutcome("ok", fetched=5, added=2))
    assert status_for(_entry(store)) == "ok"


def test_failing_outranks_stale_because_it_is_the_actionable_fact(tmp_path):
    store = _store(tmp_path)
    store.record_schedule("collector", 60)
    store.record("collector", JobOutcome("ok", fetched=1, added=1))
    store.record("collector", JobOutcome("error", detail="boom"))

    long_after = datetime.now(UTC) + timedelta(days=7)
    assert status_for(_entry(store), now=long_after) == "failing"


def test_history_survives_a_restart(tmp_path):
    path = tmp_path / "health.json"
    first = HealthStore(path)
    first.record_schedule("collector", 3600)
    first.record("collector", JobOutcome("error", detail="boom"))
    first.record("collector", JobOutcome("error", detail="boom"))

    restarted = HealthStore(path)
    assert restarted.seed() is True

    entry = restarted.snapshot()["jobs"]["collector"]
    assert entry["consecutive_failures"] == 2  # an open streak is not erased by a restart
    assert entry["interval_seconds"] == 3600
    assert status_for(entry) == "failing"


def test_seed_ignores_unknown_keys_from_an_older_build(tmp_path):
    path = tmp_path / "health.json"
    path.write_text(
        json.dumps({"jobs": {"collector": {"runs": 4, "surprise": "value"}}}), encoding="utf-8"
    )
    store = HealthStore(path)

    assert store.seed() is True

    entry = store.snapshot()["jobs"]["collector"]
    assert entry["runs"] == 4
    assert "surprise" not in entry
    assert entry["consecutive_failures"] == 0  # blank default, not missing


def test_seed_reports_false_for_a_missing_or_corrupt_artifact(tmp_path):
    assert HealthStore(tmp_path / "absent.json").seed() is False

    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not json", encoding="utf-8")
    assert HealthStore(corrupt).seed() is False


def test_recording_survives_an_unwritable_artifact_path(tmp_path):
    """Collection must not die because logs/ is unwritable."""
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file, not a directory", encoding="utf-8")
    store = HealthStore(blocker / "health.json")

    store.record("collector", JobOutcome("ok", fetched=1, added=1))  # must not raise

    assert store.flush() is False
    assert _entry(store)["runs"] == 1  # the in-memory registry is still correct


def test_report_flags_problem_jobs_and_sets_the_overall_verdict(tmp_path):
    store = _store(tmp_path)
    store.record("good", JobOutcome("ok", fetched=1, added=1))
    store.record("bad", JobOutcome("error", detail="boom"))
    store.record("unset", JobOutcome("skipped", detail="API key not set"))

    report = store.report()

    assert report["ok"] is False
    assert report["problems"] == ["bad"]  # a skipped job is not a problem to fix
    assert report["jobs"]["good"]["status"] == "ok"
    assert report["jobs"]["unset"]["status"] == "skipped"


def test_report_is_ok_when_every_job_is_healthy_or_skipped(tmp_path):
    store = _store(tmp_path)
    store.record("good", JobOutcome("ok", fetched=1, added=1))
    store.record("unset", JobOutcome("skipped", detail="API key not set"))

    assert store.report()["ok"] is True


def test_artifact_is_valid_json_after_every_record(tmp_path):
    path = tmp_path / "health.json"
    store = HealthStore(path)

    store.record("collector", JobOutcome("ok", fetched=3, added=3))
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["jobs"]["collector"]["last_added"] == 3
    assert "written_at" in payload


def test_traceback_is_captured_when_an_exception_is_supplied(tmp_path):
    store = _store(tmp_path)
    try:
        raise ValueError("the real cause")
    except ValueError as exc:
        store.record("collector", JobOutcome("error", detail="ValueError: the real cause"), exc)

    entry = _entry(store)
    assert "ValueError: the real cause" in entry["last_traceback"]
    assert "test_health.py" in entry["last_traceback"]


def test_success_clears_a_stale_traceback(tmp_path):
    store = _store(tmp_path)
    try:
        raise ValueError("old")
    except ValueError as exc:
        store.record("collector", JobOutcome("error", detail="old"), exc)

    store.record("collector", JobOutcome("ok", fetched=1, added=1))

    assert _entry(store)["last_traceback"] is None
