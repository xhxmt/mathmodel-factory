"""Input-dependency rework rule of Stage catalog v2 (optimization plan, phase 4).

The v1 rule rewinds by *ownership*: when a Stage changes a file an earlier
Stage owns, the earliest owner Stage and everything after it run again.  The
replay of the 16 recorded runs shows what that costs: Step 5 fixing the code
Step 4 wrote rewound to model construction, and Step 12 editing
``symbol_table.md`` rewound the solve, sensitivity and the whole paper.

v2 asks a different question: **did a Step that already ran consume the
input that changed?**

* Only *computation inputs* create an obligation: model and solver code, data,
  and the canonical/sensitivity results that later computation reads.  Their
  consumers are the computation Steps 5 (solve), 6 (sensitivity), 7
  (evaluation) and 8 (visualization).  Step 4 is authoring, not computation,
  and is never re-run by this rule (decision taken 2026-11-07).
* Documents (``model.md``, ``symbol_table.md``, reports, the paper, tables,
  claim and quality contracts, the problem statement) are forward repairs:
  no completed Step is re-run.  The numerical gate, the paper profile and the
  final Judge check the result.
* Machine evidence (solver receipts, job state, logs, input snapshots) is not
  an input at all.
* A file that did not exist before the initiating subtask cannot have been
  consumed by a completed Step, so creating one never re-runs anything.

When a completed computation Step consumed a changed input, the earliest such
Step and the computation Steps after it re-run, and the workflow then returns
to the initiating Step without re-running the paper Steps in between: those
are forward repairs too.

Consumers are resolved from the v1 ownership registry's owner Stage, which
already separates solve code from sensitivity and figure scripts, and main
results from sensitivity results.  The engine may narrow this further with
solver-job receipts (the scripts and inputs a completed job actually used).

Only Stage catalog v2 projects use this rule; v1 projects keep the ownership
rule unchanged.  This module is part of the v2 classifier identity
(:func:`input_dependency_contract_sha256`).
"""

from __future__ import annotations

import fnmatch
import hashlib
from dataclasses import dataclass
from pathlib import Path

INPUT_DEPENDENCY_POLICY_SCHEMA = "factory-stage-catalog-v2-input-dependency-v1"

#: The computation Steps, in execution order.  Re-running starts at the
#: earliest completed consumer and continues through the last one.
COMPUTATION_STEPS = (5, 6, 7, 8)
LAST_COMPUTATION_STEP = COMPUTATION_STEPS[-1]

# Machine evidence: never an input.
_EVIDENCE_PATTERNS = (
    ".factory/**",
    "run_state/**",
    "logs/**",
    "*.log",
    "**/*.log",
    "STEP5_RECEIPT.json",
)
_CODE_ROOTS = ("models/", "scripts/")
_DATA_ROOTS = ("data/raw/", "data/final/", "data/intermediate/")
_RESULT_ROOT = "results/"

# v1 owner Stage of a code file -> the computation Step that executes it.
# Generic ``models/**`` and ``scripts/**`` (Stage 3, written by Step 4) and
# ``scripts/step5/**`` (Stage 4) are run by the solve; the registry's
# sensitivity scripts (Stage 5) by Step 6; figure scripts (Stage 6) by Step 8.
# Code owned by the paper Stages (bootstrap diagnostics, derived-table
# generators) feeds no computation Step.
_CODE_CONSUMER_BY_OWNER = {3: 5, 4: 5, 5: 6, 6: 8}
# v1 owner Stage of a result file -> the computation Step that reads it.
# Main results (Stage 4, written by Step 5) feed sensitivity; sensitivity
# results (Stage 5) feed evaluation.  Results owned by the paper Stages
# (derived artifacts, revision diagnostics) feed no computation Step.
_RESULT_CONSUMER_BY_OWNER = {4: 6, 5: 7}


@dataclass(frozen=True)
class InputDependency:
    """Why one changed artifact does or does not oblige a re-run."""

    artifact: str
    kind: str  # code | data | result | evidence | document | new_file
    consumer_step: int | None

    @property
    def reason(self) -> str:
        if self.consumer_step is None:
            return {
                "evidence": "machine evidence, not an input",
                "new_file": "created by this subtask; no completed Step consumed it",
                "document": "document input; forward repair",
            }.get(self.kind, f"{self.kind} input with no computation consumer; forward repair")
        return f"{self.kind} input consumed by Step {self.consumer_step}"


def _matches(path: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


def classify_input(artifact: str, *, baseline: str, v1_owner_stage: int) -> InputDependency:
    """Classify one changed artifact of a subtask's manifest delta.

    ``baseline`` is the artifact's fingerprint before the subtask (``"MISSING"``
    when it did not exist); ``v1_owner_stage`` is the owner the v1 ownership
    registry assigned to the change.
    """

    path = str(artifact)
    if path.startswith("@"):
        # Synthetic paper/protected keys describe paper semantics: documents.
        return InputDependency(path, "document", None)
    if _matches(path, _EVIDENCE_PATTERNS):
        return InputDependency(path, "evidence", None)
    if str(baseline) == "MISSING":
        return InputDependency(path, "new_file", None)
    owner = int(v1_owner_stage)
    if path.startswith(_CODE_ROOTS):
        return InputDependency(path, "code", _CODE_CONSUMER_BY_OWNER.get(owner))
    if path.startswith(_DATA_ROOTS):
        return InputDependency(path, "data", 5)
    if path.startswith(_RESULT_ROOT):
        return InputDependency(path, "result", _RESULT_CONSUMER_BY_OWNER.get(owner))
    return InputDependency(path, "document", None)


def recompute_start(
    dependencies: list[InputDependency], *, completed_steps: set[int]
) -> int | None:
    """The earliest completed computation Step that consumed a changed input."""

    consumers = sorted(
        {
            item.consumer_step
            for item in dependencies
            if item.consumer_step is not None and item.consumer_step in completed_steps
        }
    )
    return consumers[0] if consumers else None


def recompute_steps(start: int) -> tuple[int, ...]:
    """The computation Steps re-run when ``start`` consumed a changed input."""

    return tuple(step for step in COMPUTATION_STEPS if step >= start)


#: Change records the rule never touches: they were constructed to fail
#: closed (a missing Stage baseline), describe protected items, or come from
#: a bespoke recovery path.  They keep the ownership rewind.
FAIL_CLOSED_SOURCES = frozenset({"explicit_fail_closed", "protected", "bespoke_recovery"})


def apply_to_record(
    record: dict,
    *,
    initiator_stage: int,
    completed_steps: set[int],
    stage_of_step,
) -> InputDependency | None:
    """Re-own one engine change record under this rule, in place.

    An upstream-owned change consumed by a completed computation Step is owned
    by that Step's Stage (its dirty cause clears when the Step re-runs); any
    other upstream-owned change is owned by the initiating Stage, a forward
    repair.  Changes owned at or after the initiating Stage keep their owner.
    Returns the classification, or ``None`` for fail-closed records.
    """

    if record.get("classification_source") in FAIL_CLOSED_SOURCES:
        return None
    owner = int(record["owner_stage"])
    dependency = classify_input(
        str(record.get("cause_artifact", "")),
        baseline=str(record.get("baseline_fingerprint", "")),
        v1_owner_stage=int(record.get("v1_owner_stage", owner)),
    )
    consumed = dependency.consumer_step is not None and dependency.consumer_step in completed_steps
    if consumed:
        record["input_consumer_step"] = dependency.consumer_step
    if owner < int(initiator_stage):
        record.setdefault("v1_owner_stage", owner)
        if consumed:
            record["owner_stage"] = min(int(stage_of_step(dependency.consumer_step)), int(initiator_stage))
            record["v2_owner_rule"] = f"v2_input_consumer_{dependency.kind}"
        else:
            record["owner_stage"] = int(initiator_stage)
            record["v2_owner_rule"] = f"v2_input_forward_{dependency.kind}"
        record["classification_source"] = "stage_catalog_v2"
    return dependency


def input_dependency_contract_sha256() -> str:
    """Identity of this rule, folded into the v2 classifier identity."""

    return hashlib.sha256(
        b"\0".join([INPUT_DEPENDENCY_POLICY_SCHEMA.encode(), Path(__file__).read_bytes()])
    ).hexdigest()
