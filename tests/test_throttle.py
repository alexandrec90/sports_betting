import json
from datetime import UTC, datetime

import pytest

from sports_betting.throttle import DailyQuotaExceededError, ProviderThrottle, QuotaLedger
from sports_betting.throttle import QuotaBudget


def test_quota_ledger_survives_reinstantiation_and_fails_closed_at_budget(tmp_path):
    path = tmp_path / "quota.json"

    def now():
        return datetime(2026, 8, 5, tzinfo=UTC)

    assert QuotaLedger(path, now=now).claim("odds", daily_limit=2) == 1
    assert QuotaLedger(path, now=now).claim("odds", daily_limit=2) == 2
    with pytest.raises(DailyQuotaExceededError, match="2/2"):
        QuotaLedger(path, now=now).claim("odds", daily_limit=2)


def test_monthly_budget_counts_credits_and_survives_a_new_day(tmp_path):
    path = tmp_path / "quota.json"
    clock = [datetime(2026, 10, 30, 23, tzinfo=UTC)]
    ledger = QuotaLedger(path, now=lambda: clock[0])

    ledger.claim("odds", monthly_limit=5, cost=2)
    clock[0] = datetime(2026, 10, 31, 1, tzinfo=UTC)
    ledger.claim("odds", monthly_limit=5, cost=2)

    assert ledger.used("odds") == (1, 4)
    with pytest.raises(DailyQuotaExceededError, match="4/5 credits in 2026-10"):
        ledger.claim("odds", monthly_limit=5, cost=2)

    clock[0] = datetime(2026, 11, 1, tzinfo=UTC)
    assert ledger.used("odds") == (0, 0)
    ledger.claim("odds", monthly_limit=5, cost=2)
    assert json.loads(path.read_text())["monthly"] == {"odds": 2}


def test_ledger_reads_a_file_written_before_monthly_counts_existed(tmp_path):
    path = tmp_path / "quota.json"
    path.write_text(json.dumps({"date": "2026-10-02", "providers": {"odds": 2}}))
    ledger = QuotaLedger(path, now=lambda: datetime(2026, 10, 2, 12, tzinfo=UTC))

    assert ledger.used("odds") == (2, 0)
    assert ledger.claim("odds", daily_limit=3, monthly_limit=10) == 3


@pytest.mark.parametrize(
    "kwargs",
    [{}, {"daily_limit": 0}, {"monthly_limit": -1}, {"monthly_limit": 5, "cost": 0}],
)
def test_ledger_rejects_unusable_limits(tmp_path, kwargs):
    with pytest.raises(ValueError):
        QuotaLedger(tmp_path / "quota.json").claim("odds", **kwargs)


def test_quota_budget_needs_a_limit_with_its_ledger(tmp_path):
    with pytest.raises(ValueError, match="configured together"):
        QuotaBudget(QuotaLedger(tmp_path / "quota.json"))


def test_quota_budget_claims_against_its_ledger(tmp_path):
    ledger = QuotaLedger(tmp_path / "quota.json")
    budget = QuotaBudget(ledger, daily_limit=1)
    assert budget.claim("odds") == 1
    with pytest.raises(DailyQuotaExceededError):
        budget.claim("odds")
    assert ledger.used("odds") == (1, 1)


def test_throttle_claims_its_cost_per_request_against_the_month(tmp_path):
    ledger = QuotaLedger(tmp_path / "quota.json")
    gate = ProviderThrottle(
        "odds",
        min_interval_seconds=0,
        budget=QuotaBudget(ledger, monthly_limit=4, cost_per_request=2),
    )
    gate()
    gate()
    with pytest.raises(DailyQuotaExceededError):
        gate()
    assert ledger.used("odds") == (2, 4)


def test_throttle_counts_only_the_requests_it_let_through(tmp_path):
    ledger = QuotaLedger(tmp_path / "quota.json")
    gate = ProviderThrottle(
        "odds", min_interval_seconds=0, budget=QuotaBudget(ledger, daily_limit=2)
    )
    assert gate.let_through == 0
    gate()
    gate()
    with pytest.raises(DailyQuotaExceededError):
        gate()
    assert gate.let_through == 2


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
