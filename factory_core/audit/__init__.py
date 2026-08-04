"""Independent, snapshot-bound audit subsystem for Modeling Factory."""

from .domain import AuditOutcome, AuditProfile, AuditRecord, AuditSnapshot, AuditStatus
from .incremental import IncrementalAuditService

__all__ = [
    "AuditOutcome",
    "AuditProfile",
    "AuditRecord",
    "AuditSnapshot",
    "AuditStatus",
    "FinalAuditService",
    "IncrementalAuditService",
    "build_final_audit_service",
]


def __getattr__(name: str):
    if name in {"FinalAuditService", "build_final_audit_service"}:
        from .service import FinalAuditService, build_final_audit_service

        return {
            "FinalAuditService": FinalAuditService,
            "build_final_audit_service": build_final_audit_service,
        }[name]
    raise AttributeError(name)
