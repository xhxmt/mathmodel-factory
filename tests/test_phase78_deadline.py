from __future__ import annotations

import pytest

from factory_core.phase78_deadline import (
    DEFAULT_PHASE78_DEADLINE_MS,
    Phase78CancellationError,
    Phase78CancellationReason,
    Phase78DeadlineError,
    Phase78OutcomeUncertain,
    TotalDeadline,
    validate_deadline_ms,
)


class FakeClock:
    def __init__(self) -> None:
        self.value = 100.0

    def __call__(self) -> float:
        return self.value

    def advance_ms(self, value: float) -> None:
        self.value += value / 1000.0


def test_deadline_validation_is_strict_and_bounded() -> None:
    assert validate_deadline_ms(None) == DEFAULT_PHASE78_DEADLINE_MS
    assert validate_deadline_ms(1) == 1
    assert validate_deadline_ms(300_000) == 300_000
    for value in (True, False, 0, -1, 1.0, "100", 300_001):
        with pytest.raises(ValueError):
            validate_deadline_ms(value)


def test_one_total_deadline_never_resets_between_adapters_or_retry() -> None:
    clock = FakeClock()
    deadline = TotalDeadline(100, clock=clock)
    first_adapter = deadline
    second_adapter = first_adapter
    clock.advance_ms(60)
    assert 0 < second_adapter.remaining_milliseconds() <= 40
    clock.advance_ms(40)
    with pytest.raises(Phase78DeadlineError) as captured:
        first_adapter.check()
    assert captured.value.code == "PHASE78_DEADLINE_EXCEEDED"


@pytest.mark.parametrize("reason", list(Phase78CancellationReason))
def test_cancellation_reasons_are_stable_and_first_reason_wins(
    reason: Phase78CancellationReason,
) -> None:
    clock = FakeClock()
    deadline = TotalDeadline(1000, clock=clock)
    assert deadline.cancel(reason) is True
    assert deadline.cancel(Phase78CancellationReason.SUPERSEDED) is False
    with pytest.raises(Phase78CancellationError) as captured:
        deadline.check()
    assert captured.value.reason == reason.value
    assert deadline.snapshot().cancellation_reason == reason.value


def test_deadline_snapshot_and_submillisecond_budget_fail_closed() -> None:
    clock = FakeClock()
    deadline = TotalDeadline(10, clock=clock)
    clock.advance_ms(9.9)
    snapshot = deadline.snapshot()
    assert snapshot.configured_ms == 10
    assert snapshot.elapsed_ms in {8, 9}
    assert 0 <= snapshot.remaining_ms <= 1
    with pytest.raises(Phase78DeadlineError):
        deadline.remaining_milliseconds()


def test_post_commit_uncertainty_exposes_only_replay_key() -> None:
    error = Phase78OutcomeUncertain("phase78-request-0001")
    assert error.code == "PHASE78_OUTCOME_UNCERTAIN"
    assert error.idempotency_key == "phase78-request-0001"
    assert "replay" in str(error).lower()
