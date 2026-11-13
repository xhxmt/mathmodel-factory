"""Human-decision receipts.

A decision is authoritative in SQLite (append-only request and instance rows).
When it is recorded, the store also writes ``.factory/decisions/<gate>/
<request_id>/<decision_id>.json`` and keeps that file's path, size and SHA-256
in the decision's ``artifact_refs``.  The file is a projection of the SQLite
row for people and tools to read; nothing re-reads it to decide whether the
decision exists.  (It used to be verified byte for byte on every read, and a
missing or edited copy made a valid decision disappear until it was repaired.)

What still makes a decision unusable is in the decision itself: an approval
recorded before decisions were bound to their subject (``LEGACY_UNBOUND``)
cannot authorize anything.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


@dataclass(frozen=True)
class DecisionReceiptVerification:
    valid: bool
    errors: tuple[str, ...]
    path: str | None = None
    sha256: str | None = None
    legacy_unbound: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "errors": list(self.errors),
            "path": self.path,
            "sha256": self.sha256,
            "legacy_unbound": self.legacy_unbound,
        }


def verify_decision_receipt(
    project_dir: str | Path,
    decision: Mapping[str, Any],
) -> DecisionReceiptVerification:
    """Whether the SQLite decision can be used; never reads the receipt file."""

    del project_dir
    refs = decision.get("artifact_refs")
    reference = refs[0] if isinstance(refs, list) and refs and isinstance(refs[0], Mapping) else {}
    path = reference.get("path") if isinstance(reference.get("path"), str) else None
    sha256 = reference.get("sha256") if isinstance(reference.get("sha256"), str) else None
    if decision.get("subject_fingerprint") == "LEGACY_UNBOUND":
        if decision.get("kind") == "approval":
            return DecisionReceiptVerification(
                False,
                ("legacy approval has no immutable content-bound receipt",),
                path=path,
                sha256=sha256,
                legacy_unbound=True,
            )
        return DecisionReceiptVerification(
            True, (), path=path, sha256=sha256, legacy_unbound=True
        )
    return DecisionReceiptVerification(True, (), path=path, sha256=sha256)


def verified_approval_receipts(
    project_dir: str | Path,
    *,
    require_content_freeze: bool | None = None,
) -> list[dict[str, Any]]:
    """Return exact current approval receipts consumed by finalization."""

    project = Path(project_dir).resolve()
    from .storage import SQLiteStateStore

    store = SQLiteStateStore(project)
    freeze_required, approvals = store.read_finalization_approvals()
    if require_content_freeze is None:
        require_content_freeze = freeze_required
    records: list[dict[str, Any]] = []
    for gate in ("content_freeze", "delivery_freeze_override"):
        decision = approvals[gate]
        if decision is None or decision.get("approved") is not True:
            if gate == "content_freeze" and require_content_freeze:
                raise ValueError("verified content-freeze approval receipt is required")
            continue
        verification = verify_decision_receipt(project, decision)
        if not verification.valid:
            raise ValueError(
                f"{gate} approval receipt is invalid: "
                + "; ".join(verification.errors)
            )
        reference = decision["artifact_refs"][0]
        records.append(
            {
                "gate": gate,
                "request_id": decision.get("request_id"),
                "decision_id": decision.get("decision_id"),
                "generation": decision.get("generation"),
                "path": reference.get("path"),
                "size": reference.get("size"),
                "sha256": reference.get("sha256"),
                "subject_fingerprint": decision.get("subject_fingerprint"),
                "options_fingerprint": decision.get("options_fingerprint"),
            }
        )
    return records
