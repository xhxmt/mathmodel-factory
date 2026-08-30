"""One wall-clock budget shared by every Phase 7+8 shadow adapter.

The object is deliberately small and dependency-free.  Callers create it once
at the CLI, service, worker, or Web boundary and pass the same object through
SQLite, file materialization, and replay.  A nested adapter may consume the
remaining budget, but it must never create a fresh deadline.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
import threading
import time
from typing import Callable


DEFAULT_PHASE78_DEADLINE_MS = 30_000
MAXIMUM_PHASE78_DEADLINE_MS = 300_000


class Phase78CancellationReason(str, Enum):
    USER_CANCEL = "user_cancel"
    SHUTDOWN = "shutdown"
    SUPERSEDED = "superseded"


class Phase78DeadlineError(TimeoutError):
    """Raised once the single Phase 7+8 request budget is exhausted."""

    code = "PHASE78_DEADLINE_EXCEEDED"
    reason = "timeout"


class Phase78CancellationError(RuntimeError):
    """Raised when an explicit caller-owned cancellation wins the race."""

    code = "PHASE78_REQUEST_CANCELLED"

    def __init__(self, reason: Phase78CancellationReason) -> None:
        self.reason = reason.value
        super().__init__(f"Phase 7+8 request cancelled: {self.reason}")


class Phase78OutcomeUncertain(RuntimeError):
    """A commit may have completed after the caller's response deadline.

    The request idempotency key is intentionally exposed so a normal client can
    query or replay the exact request instead of generating a second result.
    """

    code = "PHASE78_OUTCOME_UNCERTAIN"

    def __init__(self, idempotency_key: str) -> None:
        self.idempotency_key = idempotency_key
        super().__init__(
            "Phase 7+8 outcome is uncertain; replay the same idempotency key"
        )


def validate_deadline_ms(value: object) -> int:
    """Return the strict bounded request budget used at public boundaries."""

    if value is None:
        return DEFAULT_PHASE78_DEADLINE_MS
    if type(value) is not int or not 1 <= value <= MAXIMUM_PHASE78_DEADLINE_MS:
        raise ValueError(
            "Phase 7+8 deadline_ms must be an integer between 1 and 300000"
        )
    return value


@dataclass(frozen=True, slots=True)
class DeadlineSnapshot:
    configured_ms: int
    elapsed_ms: int
    remaining_ms: int
    cancellation_reason: str | None


class TotalDeadline:
    """Thread-safe monotonic total budget with explicit cancellation reasons."""

    def __init__(
        self,
        deadline_ms: object = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        configured = validate_deadline_ms(deadline_ms)
        started = float(clock())
        if not math.isfinite(started):
            raise ValueError("Phase 7+8 monotonic clock must be finite")
        self._configured_ms = configured
        self._clock = clock
        self._started = started
        self._deadline = started + configured / 1000.0
        self._lock = threading.Lock()
        self._cancelled: Phase78CancellationReason | None = None

    @property
    def configured_ms(self) -> int:
        return self._configured_ms

    def cancel(self, reason: Phase78CancellationReason | str) -> bool:
        try:
            normalized = (
                reason
                if isinstance(reason, Phase78CancellationReason)
                else Phase78CancellationReason(reason)
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid Phase 7+8 cancellation reason") from exc
        with self._lock:
            if self._cancelled is not None:
                return False
            self._cancelled = normalized
            return True

    def _now(self) -> float:
        value = float(self._clock())
        if not math.isfinite(value):
            raise Phase78DeadlineError("Phase 7+8 monotonic clock became invalid")
        return value

    def check(self, _stage: str | None = None) -> None:
        with self._lock:
            cancelled = self._cancelled
        if cancelled is not None:
            raise Phase78CancellationError(cancelled)
        if self._now() >= self._deadline:
            raise Phase78DeadlineError("Phase 7+8 total request deadline exceeded")

    def remaining_seconds(self) -> float:
        """Return the live remaining budget; never return an artificial floor."""

        self.check()
        return max(0.0, self._deadline - self._now())

    def remaining_milliseconds(self) -> int:
        """Return a conservative positive timeout suitable for SQLite."""

        remaining = self.remaining_seconds()
        milliseconds = int(remaining * 1000)
        if milliseconds < 1:
            # There can be a sub-millisecond interval between check and a
            # blocking API.  Refuse to silently turn it into SQLite's 0 = no
            # wait interpretation.
            raise Phase78DeadlineError("Phase 7+8 total request deadline exceeded")
        return milliseconds

    def snapshot(self) -> DeadlineSnapshot:
        now = self._now()
        with self._lock:
            cancelled = self._cancelled
        elapsed = max(0, int((now - self._started) * 1000))
        remaining = max(0, int((self._deadline - now) * 1000))
        return DeadlineSnapshot(
            configured_ms=self._configured_ms,
            elapsed_ms=elapsed,
            remaining_ms=remaining,
            cancellation_reason=None if cancelled is None else cancelled.value,
        )
