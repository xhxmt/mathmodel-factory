#!/usr/bin/env python3
"""Replay recorded workflow decisions against a decision rule.

This is the liveness regression gate of the optimization plan
(docs/architecture/NEXT_OPTIMIZATION_EXECUTION_PLAN.md, phase 0).  It never
re-runs a model.  It reads the content-free fixtures written by
``scripts/extract_run_history.py`` and answers three questions:

1. Time accounting - of each run's wall-clock span, how much model work was
   kept, redone after a rewind, lost to failures, or spent idle.
2. Change-set decisions - for every recorded change set (the classified
   artifact delta a Stage subtask committed), would a given rule rewind the
   workflow, and to where?  ``current`` uses the classifier and reopen rule of
   the checked-out ``factory_core``; later phases register new rules here.
3. Failure attribution - which recorded failures consumed a content attempt,
   and which of those were caused by infrastructure or a model provider
   according to the hand-checked labels in ``labels.json``.

Usage:
    python3 scripts/replay_run_history.py [--fixtures DIR] [--rule current]
        [--json OUT.json] [--markdown OUT.md]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

DEFAULT_FIXTURES = REPO_ROOT / "tests" / "fixtures" / "run_history"
REPORT_SCHEMA = "factory-run-history-replay-v1"

# An idle gap is a stretch with no workflow event that is not covered by a
# Step attempt.  Twenty minutes is longer than any heartbeat-free stretch the
# engine produces while it is working.
IDLE_GAP_SECONDS = 1200

# Events that close the open Step attempt, and the outcome they record.
_ATTEMPT_END = {
    "STEP_SUCCEEDED": "ok",
    "STEP_FAILED": "fail",
    "RETRY_SCHEDULED": "retry",
    "RUNNER_INTERRUPTED": "interrupted",
    "STAGE_SEMANTIC_REOPENED": "reopened_mid",
    "STEP_REOPENED": "reopened_mid",
    "KILLED": "killed",
    "RUN_STOPPED": "stopped",
    "CONTEST_DEADLINE_EXHAUSTED": "deadline",
    "RUNNER_EXIT_UNVERIFIED": "unverified",
}
# These end an attempt whatever Step they name: they stop the runner itself.
_RUNNER_WIDE = {"KILLED", "RUN_STOPPED"}
# A pause suspends the open attempt rather than ending it: after ``RESUMED``
# recovery may promote the interrupted Step straight to ``STEP_SUCCEEDED``
# without a new ``STEP_STARTED``.  The PAUSED..RESUMED stretch is not work.
# An attempt that never continues (the next event is a new ``STEP_STARTED``,
# or the history ends) ends at its pause with outcome ``paused``.

_FAILURE_EVENTS = {"STEP_FAILED", "RETRY_SCHEDULED"}

# The engine decides a semantic reopen only when a Stage subtask tries to
# commit (``_complete_stage_task``).  A failed attempt persists its change set
# (``_fail_stage_task``) but never rewinds, so those change sets are recorded
# here and left undecided.
_DECIDING_EVENTS = {"STEP_SUCCEEDED", "STAGE_SEMANTIC_REOPENED"}

_PAPER_DOMAIN = {
    "MATH_DIRTY": "math",
    "CITATION_DIRTY": "citation",
    "PROSE_DIRTY": "prose",
    "FORMAT_DIRTY": "format",
}


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------


def load_fixtures(directory: Path = DEFAULT_FIXTURES) -> list[dict[str, Any]]:
    fixtures = []
    for path in sorted(directory.glob("*.json")):
        if path.name == "labels.json" or path.name.startswith("_"):
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("schema") != "factory-run-history-fixture-v1":
            raise ValueError(f"{path}: not a run-history fixture")
        fixtures.append(data)
    fixtures.sort(key=lambda fixture: fixture["events"][0]["ts"] if fixture["events"] else 0)
    return fixtures


def load_labels(directory: Path = DEFAULT_FIXTURES) -> dict[str, dict[int, dict[str, str]]]:
    path = directory / "labels.json"
    if not path.is_file():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {
        run: {int(rev): label for rev, label in entries.items()}
        for run, entries in raw.get("failures", {}).items()
    }


# --------------------------------------------------------------------------
# 1. Time accounting
# --------------------------------------------------------------------------


@dataclass
class Attempt:
    step: int | None
    start: float
    end: float
    outcome: str
    error_class: str | None = None
    fate: str = ""
    end_rev: int | None = None
    pauses: list[tuple[float, float]] = field(default_factory=list)
    # First pause not yet followed by an end event of this attempt.
    paused_at: float | None = None
    # Start of the pause still waiting for its RESUMED.
    open_pause: float | None = None

    @property
    def hours(self) -> float:
        paused = sum(max(0.0, end - start) for start, end in self.pauses)
        return max(0.0, self.end - self.start - paused) / 3600

    def segments(self) -> list[tuple[float, float]]:
        """The stretches this attempt was running, with its pauses cut out."""

        result, cursor = [], self.start
        for start, end in sorted(self.pauses):
            if start > cursor:
                result.append((cursor, start))
            cursor = max(cursor, end)
        if self.end > cursor:
            result.append((cursor, self.end))
        return result

    def close_at_pause(self) -> None:
        """End a suspended attempt that never continued, at its pause."""

        assert self.paused_at is not None
        self.end = self.paused_at
        self.pauses = [(start, end) for start, end in self.pauses if end <= self.paused_at]
        self.outcome = "paused"
        self.paused_at = self.open_pause = None


def attempt_ledger(events: list[dict[str, Any]]) -> list[Attempt]:
    """Rebuild every Step attempt and what became of it.

    ``kept`` is the last successful attempt of a Step; an earlier success of
    the same Step was ``redone`` (a later rewind or retry repeated it).
    """

    attempts: list[Attempt] = []
    current: Attempt | None = None
    for event in events:
        kind = event["type"]
        if kind == "STEP_STARTED":
            if current is not None:
                if current.paused_at is not None:
                    current.close_at_pause()
                else:
                    current.end = event["ts"]
                    current.outcome = "superseded"
                attempts.append(current)
            current = Attempt(step=event.get("step"), start=event["ts"], end=event["ts"], outcome="open")
            continue
        if current is None:
            continue
        if kind == "PAUSED":
            if current.paused_at is None:
                current.paused_at = event["ts"]
            if current.open_pause is None:
                current.open_pause = event["ts"]
            continue
        if kind == "RESUMED":
            if current.open_pause is not None:
                current.pauses.append((current.open_pause, event["ts"]))
                current.open_pause = None
            continue
        if kind not in _ATTEMPT_END:
            continue
        if kind in _RUNNER_WIDE or event.get("step") in (None, current.step):
            current.end = event["ts"]
            current.end_rev = event["rev"]
            current.outcome = _ATTEMPT_END[kind]
            current.error_class = event.get("error_class")
            # The attempt continued through its pause(s).  A pause with no
            # RESUMED was not idle: the worker kept running until this event.
            current.paused_at = current.open_pause = None
            attempts.append(current)
            current = None
    if current is not None:
        if current.paused_at is not None:
            current.close_at_pause()
        else:
            current.end = events[-1]["ts"]
        attempts.append(current)

    last_success = {attempt.step: index for index, attempt in enumerate(attempts) if attempt.outcome == "ok"}
    for index, attempt in enumerate(attempts):
        if attempt.outcome == "ok":
            attempt.fate = "kept" if last_success[attempt.step] == index else "redone"
        else:
            attempt.fate = attempt.outcome
    return attempts


def time_accounting(fixture: dict[str, Any]) -> dict[str, Any]:
    events = fixture["events"]
    if not events:
        return {"span_h": 0.0, "idle_h": 0.0, "fate_h": {}, "fate_n": {}, "attempts": 0}
    attempts = attempt_ledger(events)
    covered = [segment for attempt in attempts for segment in attempt.segments()]
    idle = 0.0
    for before, after in zip(events, events[1:]):
        gap = after["ts"] - before["ts"]
        if gap <= IDLE_GAP_SECONDS:
            continue
        if any(start <= before["ts"] and after["ts"] <= end for start, end in covered):
            continue
        idle += gap
    fate_hours: dict[str, float] = defaultdict(float)
    fate_count: Counter[str] = Counter()
    for attempt in attempts:
        fate_hours[attempt.fate] += attempt.hours
        fate_count[attempt.fate] += 1
    return {
        "span_h": (events[-1]["ts"] - events[0]["ts"]) / 3600,
        "idle_h": idle / 3600,
        "fate_h": dict(fate_hours),
        "fate_n": dict(fate_count),
        "attempts": len(attempts),
    }


# --------------------------------------------------------------------------
# 2. Change-set decisions
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ChangeSet:
    run: str
    rev: int
    event_type: str
    stage: int | None
    step: int | None
    subtask: str | None
    classifier: tuple[str, ...]
    changes: tuple[dict[str, Any], ...]
    solver_job_owners: dict[str, int]
    recorded_reopen: bool
    recorded_owner: int | None
    recorded_resume_after: int | None


@dataclass(frozen=True)
class Decision:
    reopen: bool
    owner_stage: int | None = None
    resume_after_step: int | None = None
    owners: tuple[int, ...] = ()
    # The Stage the rule evaluated the change set in.  None means the recorded
    # Stage; a catalog that moves the initiating Step (v2 runs Step 13 in
    # Stage 10, recorded as the v1 Stage-8 precheck) states its own.
    stage: int | None = None
    # Phase 4: why the rule decided as it did, one line per changed artifact
    # that a completed computation Step consumed (empty for other rules).
    reasons: tuple[str, ...] = ()
    # Phase 4: the computation Steps re-run before returning to the
    # initiating Step (empty for rules that rewind by ownership).
    recompute_steps: tuple[int, ...] = ()


def change_sets(fixture: dict[str, Any]) -> list[ChangeSet]:
    by_rev = {event["rev"]: event for event in fixture["events"]}
    result = []
    for entry in fixture["change_sets"]:
        event = by_rev.get(entry["rev"], {})
        stage = event.get("stage")
        step = event.get("step")
        if stage is None and step is not None:
            from factory_core.stages import stage_for_step

            stage = stage_for_step(step).id
        reopen = event.get("type") == "STAGE_SEMANTIC_REOPENED"
        result.append(
            ChangeSet(
                run=fixture["run"],
                rev=entry["rev"],
                event_type=event.get("type", "?"),
                stage=stage,
                step=step,
                subtask=event.get("subtask"),
                classifier=tuple(entry.get("classifier", ())),
                changes=tuple(entry["changes"]),
                solver_job_owners=fixture.get("solver_job_owners", {}),
                recorded_reopen=reopen,
                recorded_owner=event.get("semantic_owner_stage") if reopen else None,
                recorded_resume_after=event.get("resume_after_step") if reopen else None,
            )
        )
    return result


def rebuild_manifests(changes: Iterable[dict[str, Any]]) -> tuple[dict[str, str], dict[str, str], list[dict[str, Any]]]:
    """Rebuild the before/after manifest keys a recorded change set came from.

    A recorded cause names a path; a paper source's semantic domains were
    manifest keys of the form ``@paper:<path>:<domain>``, so those are rebuilt
    from the flag.  ``MISSING_STAGE_INPUT_BASELINE`` rows were never a manifest
    delta (the engine writes them to fail closed) and are passed through.
    """

    before: dict[str, str] = {}
    after: dict[str, str] = {}
    passthrough: list[dict[str, Any]] = []
    for row in changes:
        artifact = row["artifact"]
        if artifact == "MISSING_STAGE_INPUT_BASELINE":
            passthrough.append(row)
            continue
        key = artifact
        if (
            not artifact.startswith("@")
            and artifact.endswith(".tex")
            and row["flag"] in _PAPER_DOMAIN
            and row["owner_stage"] in (8, 9)
        ):
            key = f"@paper:{artifact}:{_PAPER_DOMAIN[row['flag']]}"
        baseline, current = row["baseline"], row["current"]
        if baseline == current:
            current = f"{current}~changed"
        if baseline != "MISSING":
            before[key] = baseline
        if current != "MISSING":
            after[key] = current
    return before, after, passthrough


def current_rule(change_set: ChangeSet) -> Decision:
    """The checked-out engine's semantic-reopen decision for a change set."""

    from factory_core.current_dirty import classify_manifest_changes
    from factory_core.dirty import solver_receipt_job_id
    from factory_core.stages import resume_after_step_for_stage

    before, after, passthrough = rebuild_manifests(change_set.changes)
    owners: set[int] = {int(row["owner_stage"]) for row in passthrough}
    for change in classify_manifest_changes(before, after):
        owner = change.owner_stage
        job_id = solver_receipt_job_id(change.cause_artifact)
        if job_id is not None and job_id in change_set.solver_job_owners:
            owner = change_set.solver_job_owners[job_id]
        owners.add(int(owner))
    if change_set.stage is None:
        return Decision(reopen=False)
    upstream = tuple(sorted(owner for owner in owners if owner < change_set.stage))
    if not upstream:
        return Decision(reopen=False)
    return Decision(
        reopen=True,
        owner_stage=upstream[0],
        resume_after_step=resume_after_step_for_stage(upstream[0]),
        owners=upstream,
    )


def recorded_rule(change_set: ChangeSet) -> Decision:
    return Decision(
        reopen=change_set.recorded_reopen,
        owner_stage=change_set.recorded_owner,
        resume_after_step=change_set.recorded_resume_after,
    )


def stage_catalog_v2_rule(change_set: ChangeSet) -> Decision:
    """The same change set under Stage catalog v2 (optimization plan, phase 2).

    The initiating Step is mapped to its v2 Stage (Step 13, recorded as the v1
    Stage-8 precheck, becomes the v2 Stage-10 final Judge), each classified
    change goes through the v2 owner rules, and the reopen target is the v2
    catalog position of the earliest upstream owner.  The engine applies the
    same rules in ``FactoryEngine._stage_manifest_delta``.
    """

    from factory_core.current_dirty import classify_manifest_changes
    from factory_core.dirty import solver_receipt_job_id
    from factory_core.stage_catalog_v2_policy import v2_owner_stage
    from factory_core.stages import CATALOG_V2

    if change_set.step is None and change_set.stage is None:
        return Decision(reopen=False)
    stage = v2_stage(change_set)
    before, after, passthrough = rebuild_manifests(change_set.changes)
    owners: set[int] = {int(row["owner_stage"]) for row in passthrough}
    for change in classify_manifest_changes(before, after):
        record = change.to_dict()
        job_id = solver_receipt_job_id(change.cause_artifact)
        if job_id is not None and job_id in change_set.solver_job_owners:
            record["owner_stage"] = change_set.solver_job_owners[job_id]
        owner, _rule = v2_owner_stage(record, initiator_stage=stage)
        owners.add(int(owner))
    upstream = tuple(sorted(owner for owner in owners if owner < stage))
    if not upstream:
        return Decision(reopen=False, stage=stage)
    return Decision(
        reopen=True,
        owner_stage=upstream[0],
        resume_after_step=CATALOG_V2.resume_after_step_for_stage(upstream[0]),
        owners=upstream,
        stage=stage,
    )


def v2_stage(change_set: ChangeSet) -> int | None:
    """The v2 Stage of a change set's initiating Step (recorded Stage if none)."""

    from factory_core.stages import CATALOG_V2

    if change_set.step is not None:
        return CATALOG_V2.stage_for_step(change_set.step).id
    return change_set.stage


def input_dependency_rule(change_set: ChangeSet) -> Decision:
    """Stage catalog v2 with the phase-4 input-dependency rework rule.

    A change obliges a re-run only when a computation Step (5-8) that already
    ran consumed it (``factory_core.input_dependency_policy``).  The earliest
    such Step and the computation Steps after it re-run, then the workflow
    returns to the initiating Step; documents are forward repairs.  The
    initiating Step is placed by its v2 catalog position, as in the ``v2`` rule.
    """

    from factory_core.input_dependency_policy import (
        COMPUTATION_STEPS,
        classify_input,
        recompute_start,
        recompute_steps,
    )
    from factory_core.stages import CATALOG_V2

    if change_set.step is None:
        return Decision(reopen=False, stage=v2_stage(change_set))
    stage = v2_stage(change_set)
    position = CATALOG_V2.step_position(change_set.step)
    completed = {step for step in COMPUTATION_STEPS if CATALOG_V2.step_position(step) < position}
    dependencies = [
        classify_input(
            str(row["artifact"]),
            baseline=str(row.get("baseline", "")),
            v1_owner_stage=int(row["owner_stage"]),
        )
        for row in change_set.changes
    ]
    start = recompute_start(dependencies, completed_steps=completed)
    if start is None:
        return Decision(reopen=False, stage=stage)
    steps = tuple(step for step in recompute_steps(start) if step in completed)
    reasons = tuple(
        sorted(
            f"{item.artifact}: {item.reason}"
            for item in dependencies
            if item.consumer_step is not None and item.consumer_step in completed
        )
    )
    owner = CATALOG_V2.stage_for_step(start).id
    return Decision(
        reopen=True,
        owner_stage=owner,
        resume_after_step=CATALOG_V2.resume_after_step_for_stage(owner),
        owners=(owner,),
        stage=stage,
        reasons=reasons,
        recompute_steps=steps,
    )


RULES: dict[str, Callable[[ChangeSet], Decision]] = {
    "recorded": recorded_rule,
    "current": current_rule,
    "v2": stage_catalog_v2_rule,
    "v2-inputs": input_dependency_rule,
}


def current_classifier_id() -> str:
    from factory_core.current_dirty import classifier_contract_sha256

    return classifier_contract_sha256()[:12]


def replay_decisions(fixtures: list[dict[str, Any]], rule: Callable[[ChangeSet], Decision]) -> list[dict[str, Any]]:
    rows = []
    for fixture in fixtures:
        for change_set in change_sets(fixture):
            evaluated = change_set.event_type in _DECIDING_EVENTS
            decision = rule(change_set) if evaluated else Decision(reopen=False)
            # The Stage the rule judged in.  Stage-pair statistics must use
            # this, not the recorded Stage: under v2 a Step-13 change set is
            # recorded in v1 Stage 8 but evaluated in Stage 10.
            if decision.stage is not None:
                evaluated_stage = decision.stage
            elif rule in (stage_catalog_v2_rule, input_dependency_rule):
                evaluated_stage = v2_stage(change_set)
            else:
                evaluated_stage = change_set.stage
            rows.append(
                {
                    "run": change_set.run,
                    "rev": change_set.rev,
                    "event_type": change_set.event_type,
                    # ``stage`` keeps the v1 report shape (it is the
                    # recorded Stage); pair statistics use evaluated_stage.
                    "stage": change_set.stage,
                    "recorded_stage": change_set.stage,
                    "evaluated_stage": evaluated_stage,
                    "step": change_set.step,
                    "subtask": change_set.subtask,
                    "classifier": list(change_set.classifier),
                    "evaluated": evaluated,
                    "recorded": {
                        "reopen": change_set.recorded_reopen,
                        "owner_stage": change_set.recorded_owner,
                        "resume_after_step": change_set.recorded_resume_after,
                    },
                    "decided": {
                        "reopen": decision.reopen,
                        "owner_stage": decision.owner_stage,
                        "resume_after_step": decision.resume_after_step,
                        "owners": list(decision.owners),
                        "reasons": list(decision.reasons),
                        "recompute_steps": list(decision.recompute_steps),
                    },
                }
            )
    return rows


# --------------------------------------------------------------------------
# 3. Failure attribution
# --------------------------------------------------------------------------


def failure_records(fixture: dict[str, Any], labels: dict[int, dict[str, str]]) -> list[dict[str, Any]]:
    """Every failure that ended a Step attempt, with its hand-checked cause.

    A failed attempt counts against the Step's content budget unless its
    recorded error class is one the engine exempts
    (:data:`factory_core.engine.NON_CONTENT_ERROR_CLASSES`).  The replay
    re-applies that budget rule to recorded classes; it cannot re-run a check
    to reclassify a failure recorded under an older checker.
    """

    from factory_core.engine import NON_CONTENT_ERROR_CLASSES

    attempts = {attempt.end_rev: attempt for attempt in attempt_ledger(fixture["events"]) if attempt.end_rev is not None}
    records = []
    for event in fixture["events"]:
        if event["type"] not in _FAILURE_EVENTS:
            continue
        attempt = attempts.get(event["rev"])
        label = labels.get(event["rev"], {})
        records.append(
            {
                "run": fixture["run"],
                "rev": event["rev"],
                "type": event["type"],
                "step": event.get("step"),
                "error_class": event.get("error_class"),
                "reason": event.get("reason"),
                "attempt_hours": attempt.hours if attempt is not None else 0.0,
                # Neither a non-content retry nor the terminal class its
                # exhausted budget ends with is a content attempt.
                "consumed_attempt": attempt is not None
                and str(event.get("error_class") or "") not in NON_CONTENT_ERROR_CLASSES
                and str(event.get("error_class") or "") not in NON_CONTENT_ERROR_CLASSES.values(),
                "cause": label.get("cause", "unlabeled"),
                "evidence": label.get("evidence"),
            }
        )
    return records


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------


def build_report(fixtures: list[dict[str, Any]], rule_name: str = "current", labels=None) -> dict[str, Any]:
    labels = labels if labels is not None else {}
    rule = RULES[rule_name]
    runs = []
    totals: dict[str, float] = defaultdict(float)
    for fixture in fixtures:
        accounting = time_accounting(fixture)
        totals["span_h"] += accounting["span_h"]
        totals["idle_h"] += accounting["idle_h"]
        for fate, hours in accounting["fate_h"].items():
            totals[f"{fate}_h"] += hours
        runs.append(
            {
                "run": fixture["run"],
                "final_status": fixture["final_state"]["status"],
                "last_completed_step": fixture["final_state"]["last_completed_step"],
                **accounting,
            }
        )

    decisions = replay_decisions(fixtures, rule)
    current_id = current_classifier_id()
    evaluated = [row for row in decisions if row["evaluated"]]
    same_classifier = [row for row in evaluated if row["classifier"] == [current_id]]
    fidelity_mismatches = [
        row
        for row in same_classifier
        if (row["recorded"]["reopen"], row["recorded"]["resume_after_step"])
        != (row["decided"]["reopen"], row["decided"]["resume_after_step"])
    ]
    reopen_pairs: Counter[str] = Counter()
    for row in decisions:
        if row["decided"]["reopen"]:
            reopen_pairs[f"stage{row['evaluated_stage']}->owner{row['decided']['owner_stage']}"] += 1

    failures = []
    for fixture in fixtures:
        failures.extend(failure_records(fixture, labels.get(fixture["run"], {})))
    non_content = [
        record
        for record in failures
        if record["consumed_attempt"] and record["cause"] in {"infrastructure", "provider", "workflow"}
    ]

    return {
        "schema": REPORT_SCHEMA,
        "rule": rule_name,
        "current_classifier": current_id,
        "runs": runs,
        "totals": {
            "runs": len(fixtures),
            "completed": sum(1 for run in runs if run["final_status"] == "completed"),
            **{key: round(value, 2) for key, value in sorted(totals.items())},
            "kept_share": round(totals["kept_h"] / totals["span_h"], 4) if totals["span_h"] else 0.0,
        },
        "decisions": {
            "change_sets": len(decisions),
            "evaluated_change_sets": len(evaluated),
            "recorded_reopens": sum(1 for row in decisions if row["recorded"]["reopen"]),
            "decided_reopens": sum(1 for row in decisions if row["decided"]["reopen"]),
            "decided_reopen_pairs": dict(sorted(reopen_pairs.items())),
            "fidelity": {
                "change_sets_under_current_classifier": len(same_classifier),
                "mismatches": [
                    {key: row[key] for key in ("run", "rev", "recorded_stage", "evaluated_stage", "subtask", "recorded", "decided")}
                    for row in fidelity_mismatches
                ],
            },
            "rows": decisions,
        },
        "failures": {
            "total": len(failures),
            "by_cause": dict(Counter(record["cause"] for record in failures)),
            "non_content_consuming_attempts": len(non_content),
            "non_content_attempt_hours": round(sum(record["attempt_hours"] for record in non_content), 2),
            "rows": failures,
        },
    }


def summarize(report: dict[str, Any]) -> dict[str, Any]:
    """The report without per-row detail and with rounded per-run numbers."""

    decisions = {key: value for key, value in report["decisions"].items() if key != "rows"}
    failures = {key: value for key, value in report["failures"].items() if key != "rows"}
    runs = [
        {
            "run": run["run"],
            "final_status": run["final_status"],
            "span_h": round(run["span_h"], 2),
            "idle_h": round(run["idle_h"], 2),
            "kept_h": round(run["fate_h"].get("kept", 0.0), 2),
            "redone_h": round(run["fate_h"].get("redone", 0.0), 2),
        }
        for run in report["runs"]
    ]
    return {**{key: report[key] for key in ("schema", "rule", "current_classifier", "totals")},
            "runs": runs, "decisions": decisions, "failures": failures}


def render_markdown(report: dict[str, Any]) -> str:
    totals = report["totals"]
    decisions = report["decisions"]
    failures = report["failures"]
    lines = [
        f"# Run-history replay — rule `{report['rule']}`",
        "",
        f"Classifier under test: `{report['current_classifier']}`.",
        "",
        "## Totals",
        "",
        "| Metric | Value |",
        "| --- | --- |",
        f"| Runs (completed) | {totals['runs']} ({totals['completed']}) |",
        f"| Wall-clock span | {totals.get('span_h', 0):.1f} h |",
        f"| Kept model work | {totals.get('kept_h', 0):.1f} h ({100 * totals['kept_share']:.1f}%) |",
        f"| Redone after a rewind or retry | {totals.get('redone_h', 0):.1f} h |",
        f"| Interrupted mid-step by a reopen | {totals.get('reopened_mid_h', 0):.1f} h |",
        f"| Idle | {totals.get('idle_h', 0):.1f} h |",
        f"| Change sets recorded / evaluated (commit attempts) | {decisions['change_sets']} / {decisions['evaluated_change_sets']} |",
        f"| Reopens recorded / decided by this rule | {decisions['recorded_reopens']} / {decisions['decided_reopens']} |",
        f"| Fidelity: change sets under this classifier / mismatches | "
        f"{decisions['fidelity']['change_sets_under_current_classifier']} / {len(decisions['fidelity']['mismatches'])} |",
        f"| Failures that consumed an attempt for a non-content cause | "
        f"{failures['non_content_consuming_attempts']} ({failures['non_content_attempt_hours']:.1f} h) |",
        "",
        "## Reopens decided by this rule",
        "",
        "| Initiating stage → owner | Count |",
        "| --- | --- |",
    ]
    lines += [f"| {pair} | {count} |" for pair, count in decisions["decided_reopen_pairs"].items()]
    lines += ["", "## Runs", "", "| Run | Status | Span h | Kept h | Redone h | Idle h |", "| --- | --- | --- | --- | --- | --- |"]
    for run in report["runs"]:
        fate = run["fate_h"]
        lines.append(
            f"| {run['run']} | {run['final_status']} | {run['span_h']:.1f} | {fate.get('kept', 0):.1f} | "
            f"{fate.get('redone', 0):.1f} | {run['idle_h']:.1f} |"
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURES)
    parser.add_argument("--rule", choices=sorted(RULES), default="current")
    parser.add_argument("--json", type=Path, help="write the full report as JSON")
    parser.add_argument("--markdown", type=Path, help="write a Markdown summary")
    parser.add_argument(
        "--summary", type=Path, help="write the report without per-row detail (the committed baseline form)"
    )
    args = parser.parse_args(argv)

    fixtures = load_fixtures(args.fixtures)
    report = build_report(fixtures, args.rule, load_labels(args.fixtures))
    if args.json:
        args.json.write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    if args.summary:
        args.summary.write_text(json.dumps(summarize(report), ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    markdown = render_markdown(report)
    if args.markdown:
        args.markdown.write_text(markdown, encoding="utf-8")
    if not (args.json or args.markdown or args.summary):
        sys.stdout.write(markdown)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
