from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

from ..domain import ExecutionResult


AUDIT_SNAPSHOT_SCHEMA = "factory-content-snapshot-v2"
AUDIT_RESULT_SCHEMA = "factory-audit-result-v2"


class AuditProfile(str, Enum):
    MODEL = "model"
    RESULTS = "results"
    PAPER = "paper"
    FINAL = "final"


class AuditStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    INDETERMINATE = "INDETERMINATE"
    OVERRIDDEN = "OVERRIDDEN"


@dataclass(frozen=True)
class AuditSnapshot:
    snapshot_id: str
    base: str
    profile: str
    created_at: str
    identity: dict[str, Any]
    schema_version: str = AUDIT_SNAPSHOT_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AuditRecord:
    snapshot_id: str
    base: str
    profile: str
    status: AuditStatus
    decision: str
    judge_completed: bool
    delivery_allowed: bool
    created_at: str
    error_class: str = ""
    returncode: int = 0
    resume_after_step: int | None = None
    override: bool = False
    reused: bool = False
    evidence: dict[str, Any] = field(default_factory=dict)
    schema_version: str = AUDIT_RESULT_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["status"] = self.status.value
        return value


@dataclass(frozen=True)
class AuditOutcome:
    execution: ExecutionResult
    record: AuditRecord
    snapshot: AuditSnapshot
