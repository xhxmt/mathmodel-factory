"""Dirty-owner rules of Stage catalog v2 (optimization plan, phase 2).

The ownership registry (``artifact_ownership.py``) names v1 Stage numbers and
stays the single table every other consumer reads.  A v2 project reuses it and
re-owns three kinds of change, applied by the engine to each classified change
of a v2 Stage subtask before the upstream-owner decision:

1. **The Judge's own records.**  ``judge_evaluation.md`` belongs to Step 13,
   which is in Stage 10 in v2.  Under v1 ownership (Stage 8) the final audit
   rewriting it reopened Stage 8 from Stage 10 -- 11 of the recorded reopens.
   Likewise the issue ledger rows the Judge Stage itself records: its verdict
   is what decides a reopen, and unresolved BLOCKING rows still block
   delivery in the final audit, so the bookkeeping write is not an obligation.
2. **The paper's mathematical content.**  A paper source change classified
   MATH belongs to Stage 8 in v1, so abstract and polish (Stage 9) touching an
   inline formula reopened Stage 8 -- 6 recorded reopens.  In v2 the one
   Judge (Stage 10) is what validates the paper's mathematics, so this domain
   is owned there; ``verify_numbers`` and the Judge check it before delivery.
3. **Documentation edited once the paper is being written.**  From Stage 7 on
   (Step 9 drafting onwards), a change to one of the documents below that an
   earlier Stage owns is a forward repair by the initiating Stage, not an
   obligation to re-run the earlier Stage.  Executable model code, data,
   canonical results, solver receipts and the claim registry keep their
   owners: changing what is computed still reopens the computation.

This module is part of the v2 classifier identity
(:func:`v2_classifier_contract_sha256`), so editing it invalidates v2 Stage 9
checkpoints exactly as a v1 classifier change invalidates v1 ones.  It is not
part of the v1 identity: a v1 project's classification is unchanged.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

V2_DIRTY_POLICY_SCHEMA = "factory-stage-catalog-v2-dirty-policy-v1"

# Step 13 projections owned by the v2 Judge Stage.
JUDGE_STAGE = 10
JUDGE_PROJECTIONS = frozenset({"judge_evaluation.md"})
JUDGE_STAGE_BOOKKEEPING = frozenset({"audit_issue_ledger.md"})

# The first Stage of the paper segment (Step 9 drafting).
PAPER_SEGMENT_FIRST_STAGE = 7

# Narrative documents whose later edits are forward repairs in v2.
FORWARD_REPAIR_DOCUMENTS = frozenset(
    {
        "model.md",
        "symbol_table.md",
        "evaluation.md",
        "sensitivity_report.md",
        "visualization_log.md",
        "reviewer_entry_map.md",
        "anchor_figure_plan.md",
    }
)


def _is_paper_math(record: dict[str, Any]) -> bool:
    return (
        str(record.get("flag")) == "MATH_DIRTY"
        and int(record["owner_stage"]) == 8
        and str(record.get("cause_artifact", "")).endswith(".tex")
    )


def v2_owner_stage(record: dict[str, Any], *, initiator_stage: int) -> tuple[int, str | None]:
    """Return the v2 owner of one classified change and the rule that set it.

    ``record`` is a dirty-change record as the engine builds it (``flag``,
    ``owner_stage``, ``cause_artifact``).  The rule name is ``None`` when the
    v1 owner stands.
    """

    owner = int(record["owner_stage"])
    artifact = str(record.get("cause_artifact", ""))
    if artifact in JUDGE_PROJECTIONS and owner != JUDGE_STAGE:
        return JUDGE_STAGE, "v2_judge_projection"
    if (
        artifact in JUDGE_STAGE_BOOKKEEPING
        and int(initiator_stage) == JUDGE_STAGE
        and owner < JUDGE_STAGE
    ):
        return JUDGE_STAGE, "v2_judge_issue_bookkeeping"
    if _is_paper_math(record):
        return JUDGE_STAGE, "v2_paper_math_owned_by_judge"
    if (
        int(initiator_stage) >= PAPER_SEGMENT_FIRST_STAGE
        and owner < int(initiator_stage)
        and artifact in FORWARD_REPAIR_DOCUMENTS
    ):
        return int(initiator_stage), "v2_forward_repair_document"
    return owner, None


def v2_classifier_contract_sha256(base_classifier_sha256: str) -> str:
    """The v2 classifier identity: the shared classifier plus these rules.

    Phase 4 folds in the input-dependency rework rule
    (``input_dependency_policy``), which v2 applies after these owner rules.
    """

    from .input_dependency_policy import input_dependency_contract_sha256

    return hashlib.sha256(
        b"\0".join(
            [
                V2_DIRTY_POLICY_SCHEMA.encode(),
                base_classifier_sha256.encode(),
                Path(__file__).read_bytes(),
                input_dependency_contract_sha256().encode(),
            ]
        )
    ).hexdigest()
