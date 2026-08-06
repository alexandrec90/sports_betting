from datetime import UTC, datetime

import pytest

from sports_betting.throttle import DailyQuotaExceededError, ProviderThrottle, QuotaLedger


def test_quota_ledger_survives_reinstantiation_and_fails_closed_at_budget(tmp_path):
    path = tmp_path / "quota.json"

    def now():
        return datetime(2026, 8, 5, tzinfo=UTC)

    assert QuotaLedger(path, now=now).claim("odds", daily_limit=2) == 1
    assert QuotaLedger(path, now=now).claim("odds", daily_limit=2) == 2
    with pytest.raises(DailyQuotaExceededError, match="2/2"):
        QuotaLedger(path, now=now).claim("odds", daily_limit=2)


def test_provider_throttle_spaces_requests_without_sleeping_before_first():
    clock = [100.0]
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds

    gate = ProviderThrottle(
        "provider",
        min_interval_seconds=13,
        monotonic=lambda: clock[0],
        sleep=sleep,
    )

    gate()
    clock[0] += 3
    gate()

    assert sleeps == [10]
