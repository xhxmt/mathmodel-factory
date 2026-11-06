from __future__ import annotations

from dataclasses import asdict, dataclass


CONTEST_DURATION_SECONDS = 74 * 3_600
DELIVERY_RESERVE_SECONDS = 6 * 3_600
DELIVERY_FREEZE_SECONDS = 2 * 3_600


class ContestDeadlineExceeded(RuntimeError):
    """Raised when the current contest phase has no executable time left."""


@dataclass(frozen=True)
class ContestPolicy:
    profile: str
    contest_started_at: int
    contest_deadline_at: int
    content_freeze_at: int
    delivery_freeze_at: int
    delivery_reserve_seconds: int

    @classmethod
    def default(cls, *, started_at: int) -> "ContestPolicy":
        deadline = int(started_at) + CONTEST_DURATION_SECONDS
        return cls.for_deadline(started_at=started_at, deadline_at=deadline)

    @classmethod
    def for_deadline(
        cls, *, started_at: int, deadline_at: int
    ) -> "ContestPolicy":
        started = int(started_at)
        deadline = int(deadline_at)
        if deadline <= started:
            raise ValueError("contest deadline must be after project creation")
        return cls(
            profile="contest_core_v1",
            contest_started_at=started,
            contest_deadline_at=deadline,
            content_freeze_at=deadline - DELIVERY_RESERVE_SECONDS,
            delivery_freeze_at=deadline - DELIVERY_FREEZE_SECONDS,
            delivery_reserve_seconds=DELIVERY_RESERVE_SECONDS,
        )

    @classmethod
    def from_dict(cls, payload: dict) -> "ContestPolicy":
        return cls(
            profile=str(payload["profile"]),
            contest_started_at=int(payload["contest_started_at"]),
            contest_deadline_at=int(payload["contest_deadline_at"]),
            content_freeze_at=int(payload["content_freeze_at"]),
            delivery_freeze_at=int(payload["delivery_freeze_at"]),
            delivery_reserve_seconds=int(payload["delivery_reserve_seconds"]),
        )

    def to_dict(self) -> dict[str, int | str]:
        return asdict(self)


@dataclass(frozen=True)
class ContestPhase:
    id: int
    name: str
    steps: tuple[int, ...]
    human_gate: str | None = None


CONTEST_PHASES: tuple[ContestPhase, ...] = (
    ContestPhase(1, "problem_understanding", (0, 1)),
    ContestPhase(2, "model_tournament", (2, 3), "step3"),
    ContestPhase(3, "model_and_solve", (4, 5)),
    ContestPhase(4, "validation", (6, 7)),
    ContestPhase(5, "paper_construction", (8, 9)),
    ContestPhase(6, "deterministic_paper_audit", (10,)),
    ContestPhase(7, "review_and_revision", (11, 12, 13, 14, 15), "content_freeze"),
    ContestPhase(8, "final_audit_and_delivery", (16,)),
)

# Stage catalog v2 runs Step 13 as the final Judge after the content freeze,
# so it belongs to the final-audit phase.  (The literal is
# ``stages.STAGE_CATALOG_VERSION_V2``; stages imports this module.)
CONTEST_CATALOG_V2 = "factory-stage-catalog-v2"
CONTEST_PHASES_V2: tuple[ContestPhase, ...] = (
    *CONTEST_PHASES[:6],
    ContestPhase(7, "review_and_revision", (11, 12, 14, 15), "content_freeze"),
    ContestPhase(8, "final_audit_and_delivery", (13, 16)),
)

_PHASE_BY_STEP = {
    step_id: phase for phase in CONTEST_PHASES for step_id in phase.steps
}
_PHASE_BY_STEP_V2 = {
    step_id: phase for phase in CONTEST_PHASES_V2 for step_id in phase.steps
}


def contest_phases_for(catalog_version: str | None = None) -> tuple[ContestPhase, ...]:
    return CONTEST_PHASES_V2 if catalog_version == CONTEST_CATALOG_V2 else CONTEST_PHASES


def phase_for_step(step_id: int, *, catalog_version: str | None = None) -> ContestPhase:
    phases = _PHASE_BY_STEP_V2 if catalog_version == CONTEST_CATALOG_V2 else _PHASE_BY_STEP
    try:
        return phases[int(step_id)]
    except KeyError as exc:
        raise KeyError(f"contest phase is not defined for Step {step_id}") from exc


def effective_timeout(
    policy: ContestPolicy,
    *,
    step_id: int,
    step_timeout: int,
    now: int,
) -> int:
    boundary = (
        policy.contest_deadline_at if int(step_id) == 16 else policy.content_freeze_at
    )
    remaining = boundary - int(now)
    if remaining <= 0:
        label = "contest deadline" if int(step_id) == 16 else "content freeze"
        raise ContestDeadlineExceeded(f"{label} reached before Step {step_id}")
    return min(int(step_timeout), remaining)
