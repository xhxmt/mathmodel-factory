from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

from .domain import InvalidTransition
from .paper_sources import require_safe_latex_dependencies
from .workflow_events import canonical_hash


class HumanDecisionKind(str, Enum):
    SELECTION = "selection"
    APPROVAL = "approval"
    CONSULTATION = "consultation"


@dataclass(frozen=True)
class HumanDecisionRequest:
    request_id: str
    gate: str
    generation: int
    kind: HumanDecisionKind
    action_type: str
    requested_revision: int
    subject_fingerprint: str
    options_fingerprint: str
    reason: dict[str, Any]
    evidence: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "gate": self.gate,
            "generation": self.generation,
            "kind": self.kind.value,
            "type": self.action_type,
            "requested_revision": self.requested_revision,
            "subject_fingerprint": self.subject_fingerprint,
            "options_fingerprint": self.options_fingerprint,
            "reason": self.reason,
            "evidence": list(self.evidence),
            "metadata": self.metadata,
        }


def decision_kind(action_type: str, gate: str | None = None) -> HumanDecisionKind:
    normalized = action_type.lower()
    if "consult" in normalized:
        return HumanDecisionKind.CONSULTATION
    if (
        "approval" in normalized
        or "override" in normalized
        or (gate or "").endswith("_approval")
        or gate in {"content_freeze", "delivery_freeze_override"}
    ):
        return HumanDecisionKind.APPROVAL
    return HumanDecisionKind.SELECTION


def _file_record(project: Path, path: Path) -> dict[str, Any]:
    try:
        relative = path.resolve(strict=False).relative_to(project).as_posix()
    except ValueError:
        return {"path": str(path), "exists": False, "outside_project": True}
    if not path.is_file() or path.is_symlink():
        return {"path": relative, "exists": False}
    data = path.read_bytes()
    return {
        "path": relative,
        "exists": True,
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def _contained_files(project: Path, value: str) -> list[Path]:
    candidate = (project / value).resolve(strict=False)
    try:
        candidate.relative_to(project)
    except ValueError:
        return []
    if candidate.is_file() and not candidate.is_symlink():
        return [candidate]
    if not candidate.is_dir() or candidate.is_symlink():
        return []
    return [
        path
        for path in sorted(candidate.rglob("*"))
        if path.is_file()
        and not path.is_symlink()
        and not any(part in {"archive", "__pycache__"} for part in path.relative_to(project).parts)
    ]


def _option_evidence(project: Path, gate: str) -> tuple[list[str], Path]:
    options_path = project / "selection" / f"{gate}_options.json"
    evidence: list[str] = []
    try:
        payload = json.loads(options_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        payload = {}
    for option in payload.get("options", []) if isinstance(payload, dict) else []:
        if not isinstance(option, Mapping):
            continue
        for value in option.get("evidence_files") or option.get("evidence") or ():
            if isinstance(value, str) and value:
                evidence.append(value)
    return evidence, options_path


def _decision_fingerprints_with_metadata(
    project_dir: str | Path,
    gate: str,
    evidence: tuple[str, ...] | list[str] = (),
) -> tuple[str, str, dict[str, Any]]:
    """Bind one decision request to exact options and subject bytes."""

    project = Path(project_dir).resolve()
    option_evidence, options_path = _option_evidence(project, gate)
    subject_paths: set[Path] = set()
    subject_metadata: list[dict[str, Any]] = []
    fingerprint_metadata: dict[str, Any] = {}

    def bind_record(relative: str) -> None:
        subject_metadata.append(_file_record(project, project / relative))

    if gate == "preflight":
        for value in (
            "problem/source.md",
            "problem/problem.md",
            "problem/problem_brief.md",
            "problem/problem_plan.json",
            "problem/data_inventory.md",
            "problem/feasibility_constraints.md",
        ):
            bind_record(value)
    elif gate == "step4":
        from .storage import SQLiteStateStore

        for value in (
            "problem/problem_plan.json",
            "problem/feasibility_constraints.md",
            "chosen_method.md",
            "method_decision.md",
        ):
            bind_record(value)
        store = SQLiteStateStore(project)
        step3 = store.decision("step3") if store.exists else None
        if step3 is None:
            subject_metadata.append(
                {
                    "path": "<step3-decision>",
                    "exists": False,
                    "sha256": canonical_hash(None),
                }
            )
        else:
            identity = {
                key: step3.get(key)
                for key in (
                    "request_id",
                    "decision_id",
                    "generation",
                    "selected_option_id",
                    "selected_aux_id",
                    "subject_fingerprint",
                    "options_fingerprint",
                    "receipt_sha256",
                    "receipt_path",
                )
            }
            subject_metadata.append(
                {
                    "path": "<step3-decision>",
                    "exists": True,
                    "sha256": canonical_hash(identity),
                    "identity": identity,
                }
            )
            for value in (
                step3.get("candidate_evidence")
                or step3.get("evidence")
                or ()
            ):
                if isinstance(value, str) and value:
                    subject_paths.update(_contained_files(project, value))
            for reference in step3.get("artifact_refs") or ():
                if isinstance(reference, Mapping) and reference.get("path"):
                    subject_paths.update(
                        _contained_files(project, str(reference["path"]))
                    )
    elif gate == "dynamic":
        from .storage import SQLiteStateStore

        bind_record("consultation/REQUEST.md")
        store = SQLiteStateStore(project)
        cursor = store.stage_cursor_input() if store.exists else None
        checkpoints = store.stage_checkpoints() if store.exists else []
        cursor_identity = (
            {
                key: cursor.get(key)
                for key in (
                    "stage_id",
                    "subtask",
                    "source_step_id",
                    "input_fingerprint",
                    "selected_revision",
                )
            }
            if isinstance(cursor, Mapping)
            else None
        )
        checkpoint_identity = [
            {
                key: item.get(key)
                for key in (
                    "stage_id",
                    "subtask",
                    "source_step_id",
                    "completed_step_id",
                    "input_fingerprint",
                    "output_fingerprint",
                    "completed_revision",
                )
            }
            for item in checkpoints
            if isinstance(item, Mapping)
        ]
        subject_metadata.extend(
            (
                {
                    "path": "<stage-cursor-input>",
                    "exists": cursor_identity is not None,
                    "sha256": canonical_hash(cursor_identity),
                    "identity": cursor_identity,
                },
                {
                    "path": "<stage-checkpoints>",
                    "exists": bool(checkpoint_identity),
                    "sha256": canonical_hash(checkpoint_identity),
                    "identity": checkpoint_identity,
                },
            )
        )
    elif gate == "content_freeze":
        dependency_graph = require_safe_latex_dependencies(project)
        subject_paths.update(dependency_graph.files)
        subject_metadata.append(
            {
                "path": "<latex-dependency-graph>",
                "exists": True,
                "sha256": canonical_hash(dependency_graph.manifest()),
                "manifest": dependency_graph.manifest(),
            }
        )
        subject_paths.update(
            path
            for path in sorted(project.glob("*.bib"))
            if path.is_file() and not path.is_symlink()
        )
        for value in (
            "results/canonical_results.json",
            "figures",
            "tables",
            "problem/deliverables.json",
        ):
            subject_paths.update(_contained_files(project, value))
        from .solver_input_coverage import (
            SolverInputDriftError,
            solver_declared_input_coverage,
        )

        try:
            solver_coverage = solver_declared_input_coverage(project)
        except SolverInputDriftError as exc:
            drift = exc.to_dict()
            fingerprint_metadata["solver_input_drift"] = drift
            subject_metadata.append(
                {
                    "path": "<solver-input-drift>",
                    "exists": True,
                    "sha256": drift["fingerprint"],
                    "identity": drift,
                }
            )
            subject_paths.add(exc.current_path)
            for receipt in exc.receipts:
                subject_paths.update(_contained_files(project, str(receipt["path"])))
        else:
            subject_paths.update(solver_coverage.included_paths)
            subject_paths.update(solver_coverage.evidence_paths)
            if solver_coverage.authorized_drifts:
                fingerprint_metadata["authorized_solver_input_drifts"] = list(
                    solver_coverage.authorized_drifts
                )
    elif gate == "delivery_freeze_override":
        for value in (
            "judge_outputs/final_submission_fingerprint.json",
            "judge_outputs/final_paper_checks.json",
            ".factory/audits/latest.json",
        ):
            subject_paths.update(_contained_files(project, value))
    else:
        for value in option_evidence:
            subject_paths.update(_contained_files(project, value))

    for value in evidence:
        if value != options_path.relative_to(project).as_posix():
            subject_paths.update(_contained_files(project, value))
    subject_records = subject_metadata + [
        _file_record(project, path)
        for path in sorted(
            subject_paths,
            key=lambda item: item.relative_to(project).as_posix(),
        )
    ]
    options_records = [_file_record(project, options_path)]
    return (
        canonical_hash(subject_records),
        canonical_hash(options_records),
        fingerprint_metadata,
    )


def decision_fingerprints(
    project_dir: str | Path,
    gate: str,
    evidence: tuple[str, ...] | list[str] = (),
) -> tuple[str, str]:
    """Bind one decision request while preserving the public two-value API."""

    subject, options, _metadata = _decision_fingerprints_with_metadata(
        project_dir, gate, evidence
    )
    return subject, options

def build_decision_request(
    *,
    project_id: str,
    requested_revision: int,
    project_dir: str | Path | None = None,
    generation: int = 1,
    action: Mapping[str, Any],
    reason: str | Mapping[str, Any],
    evidence: tuple[str, ...] = (),
) -> HumanDecisionRequest:
    action_type = str(action.get("type") or "human_selection")
    gate = str(action.get("gate") or action_type)
    kind = decision_kind(action_type, gate)
    if project_dir is None:
        subject_fingerprint = "LEGACY_UNBOUND"
        options_fingerprint = "LEGACY_UNBOUND"
        fingerprint_metadata: dict[str, Any] = {}
    else:
        (
            subject_fingerprint,
            options_fingerprint,
            fingerprint_metadata,
        ) = _decision_fingerprints_with_metadata(
            project_dir, gate, evidence
        )
    request_id = hashlib.sha256(
        (
            f"{project_id}:{requested_revision}:{gate}:{action_type}:{generation}:"
            f"{subject_fingerprint}:{options_fingerprint}"
        ).encode("utf-8")
    ).hexdigest()[:24]
    structured_reason = (
        dict(reason)
        if isinstance(reason, Mapping)
        else {"code": f"awaiting_{kind.value}", "message": str(reason), "evidence": list(evidence)}
    )
    request_metadata = dict(action.get("metadata") or {})
    request_metadata.pop("human_decision", None)
    request_metadata.update(fingerprint_metadata)
    return HumanDecisionRequest(
        request_id=request_id,
        gate=gate,
        generation=int(generation),
        kind=kind,
        action_type=action_type,
        requested_revision=requested_revision,
        subject_fingerprint=subject_fingerprint,
        options_fingerprint=options_fingerprint,
        reason=structured_reason,
        evidence=evidence,
        metadata=request_metadata,
    )


def validate_resolution(
    pending_action: Mapping[str, Any], resolution: Mapping[str, Any]
) -> dict[str, Any]:
    """Validate Selection and Approval as distinct domain decisions."""

    pending_gate = str(pending_action.get("gate") or "")
    resolution_gate = str(resolution.get("gate") or pending_gate)
    if pending_gate and resolution_gate != pending_gate:
        raise InvalidTransition(
            f"pending gate {pending_gate} cannot be resolved as {resolution_gate}"
        )
    metadata = pending_action.get("metadata")
    request = metadata.get("human_decision") if isinstance(metadata, Mapping) else None
    if not isinstance(request, Mapping):
        normalized = dict(resolution)
        if resolution_gate:
            normalized["gate"] = resolution_gate
        return normalized
    kind = (
        HumanDecisionKind(str(request.get("kind")))
        if isinstance(request, Mapping) and request.get("kind")
        else decision_kind(str(pending_action.get("type") or ""), pending_gate)
    )
    normalized = dict(resolution)
    normalized["gate"] = resolution_gate
    normalized["kind"] = kind.value
    if kind is HumanDecisionKind.SELECTION:
        selected = (
            normalized.get("selected_option")
            or normalized.get("selected_option_id")
            or normalized.get("selected_primary")
            or normalized.get("selected")
        )
        if selected in {None, ""}:
            raise InvalidTransition("selection decisions require a selected option")
    elif kind is HumanDecisionKind.APPROVAL:
        approved = normalized.get("approved")
        if approved is None:
            selected = str(
                normalized.get("selected_option")
                or normalized.get("selected_option_id")
                or normalized.get("selected")
                or ""
            ).lower()
            if selected.startswith("approve") or selected in {
                "approved",
                "yes",
                "allow",
                "override",
            }:
                approved = True
            elif selected.startswith("reject") or selected in {"rejected", "no", "deny"}:
                approved = False
        if not isinstance(approved, bool):
            raise InvalidTransition("approval decisions require an explicit approved boolean")
        normalized["approved"] = approved
    elif kind is HumanDecisionKind.CONSULTATION:
        answer = normalized.get("answer") or normalized.get("response")
        if not isinstance(answer, str) or not answer.strip():
            raise InvalidTransition("consultation decisions require a nonempty answer")
        normalized["answer"] = answer.strip()
        normalized.pop("response", None)
    if isinstance(request, Mapping):
        request_id = request.get("request_id")
        supplied_request_id = normalized.get("request_id")
        if supplied_request_id not in {None, "", request_id}:
            raise InvalidTransition("resolution does not match the pending request id")
        for field_name in (
            "generation",
            "subject_fingerprint",
            "options_fingerprint",
        ):
            supplied_value = normalized.get(field_name)
            expected_value = request.get(field_name)
            if supplied_value not in {None, "", expected_value}:
                raise InvalidTransition(
                    f"resolution does not match the pending {field_name}"
                )
        normalized["request_id"] = request_id
        normalized["generation"] = request.get("generation")
        normalized["subject_fingerprint"] = request.get("subject_fingerprint")
        normalized["options_fingerprint"] = request.get("options_fingerprint")
    return normalized
