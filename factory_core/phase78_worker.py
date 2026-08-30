"""One explicit local Phase 7+8 shadow worker execution.

The worker is synchronous by contract.  It consumes one durable ledger job,
uses one caller-owned total deadline, and publishes only non-authoritative
Phase-7/8 facts.  It has no provider, network, outbox, dispatch or hidden
thread/process lifecycle.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from .canonical import canonical_sha256
from .durable_operation import OperationStatus
from .phase78_config import Phase78Settings
from .phase78_deadline import (
    Phase78CancellationError,
    Phase78CancellationReason,
    Phase78DeadlineError,
    Phase78OutcomeUncertain,
    TotalDeadline,
)
from .phase78_scheduler import (
    PHASE78_LOCAL_WORKER_EPOCH,
    PHASE78_LOCAL_WORKER_ID,
    Phase78ShadowScheduler,
    local_worker_nonce,
)
from .phase78_work_ledger import Phase78WorkContractError, Phase78WorkView
from .phase7_grounding_runtime import (
    GroundingCommitResult,
    Phase7GroundingNotFound,
    Phase7GroundingStore,
)
from .phase8_evidence_egress_runtime import (
    ApprovalResult,
    DecisionResult,
    Phase8EvidenceEgressStore,
    Phase8NotFound,
    ReferenceBindingResult,
)
from .reference_materializer import (
    ReferenceMaterializationError,
    structured_reference_unavailable,
)


PHASE78_PIPELINE_RESULT_SCHEMA = "phase78-shadow-pipeline-result-v1"


@dataclass(frozen=True, slots=True)
class _WorkLeaseToken:
    """Exact durable worker generation captured at the checkpoint boundary."""

    operation_identity_sha256: str
    claim_generation: int
    claim_owner_id: str
    claim_owner_epoch: int
    local_worker_nonce: str


class _WorkAlreadyCompleted(RuntimeError):
    def __init__(self, view: Phase78WorkView) -> None:
        self.view = view
        super().__init__("the identical durable work generation already completed")


@dataclass(frozen=True, slots=True)
class Phase78PipelineResult:
    project_id: str
    idempotency_key: str
    outcome: str
    work: Mapping[str, object]
    phase7: Mapping[str, object] | None = None
    reference_binding: Mapping[str, object] | None = None
    approval: Mapping[str, object] | None = None
    decision: Mapping[str, object] | None = None
    blocker: Mapping[str, object] | None = None
    replayed: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": PHASE78_PIPELINE_RESULT_SCHEMA,
            "project_id": self.project_id,
            "idempotency_key": self.idempotency_key,
            "outcome": self.outcome,
            "work": dict(self.work),
            "phase7": None if self.phase7 is None else dict(self.phase7),
            "reference_binding": (
                None if self.reference_binding is None else dict(self.reference_binding)
            ),
            "approval": None if self.approval is None else dict(self.approval),
            "decision": None if self.decision is None else dict(self.decision),
            "blocker": None if self.blocker is None else dict(self.blocker),
            "replayed": self.replayed,
            "authoritative": False,
            "authority_transferred": False,
            "dispatch_performed": False,
            "provider_call_performed": False,
            "outbox_dispatch_performed": False,
        }


def _phase7_key(key: str) -> str:
    return f"{key}:phase7"


def _binding_key(key: str) -> str:
    return f"{key}:phase8-binding"


def _approval_key(key: str) -> str:
    return f"{key}:phase8-approval"


def _decision_key(key: str) -> str:
    return f"{key}:phase8-decision"


def _binding_summary(value: ReferenceBindingResult) -> dict[str, object]:
    return {
        "binding_sha256": value.binding_sha256,
        "scope_key": value.scope_key,
        "sequence": value.sequence,
        "previous_binding_sha256": value.previous_binding_sha256,
        "replayed": value.replayed,
        "current": value.current,
        "authoritative": False,
        "authority_transferred": False,
        "dispatch_performed": False,
    }


def _approval_summary(value: ApprovalResult) -> dict[str, object]:
    approval = value.approval
    event = value.lifecycle_event
    return {
        "approval_id": approval["approval_id"],
        "approval_sha256": approval["approval_sha256"],
        "scope_key": approval["scope_key"],
        "state": event["state"],
        "event_sha256": event["event_sha256"],
        "replayed": value.replayed,
        "authoritative": False,
        "authority_transferred": False,
        "dispatch_performed": False,
    }


def _decision_summary(value: DecisionResult | Mapping[str, object]) -> dict[str, object]:
    if isinstance(value, DecisionResult):
        decision = dict(value.decision)
        replayed = value.replayed
    else:
        decision = dict(value)
        replayed = False
    return {
        "decision_sha256": decision.get(
            "decision_sha256", decision.get("source_decision_sha256")
        ),
        "scope_key": decision["scope_key"],
        "status": decision["status"],
        "reason_code": decision["reason_code"],
        "approval_id": decision.get("approval_id"),
        "replayed": replayed,
        "authoritative": False,
        "authority_transferred": False,
        "dispatch_performed": False,
    }


def _p7_current_callback(
    store: Phase7GroundingStore,
    upstream_current,
    deadline: TotalDeadline,
):
    def verify(scope_key: str, commit_sha256: str) -> bool:
        loaded = store.load_current_bundle(
            scope_key,
            current_head_verifier=upstream_current,
            deadline=deadline,
        )
        return loaded["result"]["commit_sha256"] == commit_sha256

    return verify


def _phase8_fence(
    *,
    deadline: TotalDeadline,
    current_verifier,
    request,
    p7_current,
    p7_scope_key: str,
    p7_commit_sha256: str,
    work_fence,
):
    def fence(_stage: str) -> None:
        work_fence(_stage)
        deadline.check(_stage)
        current_verifier.verify(
            phase3_artifact_state=request.phase3_artifact_state,
            phase3_artifact_occurrence=request.phase3_artifact_occurrence,
            phase6_access_proof=request.phase6_access_proof,
            deadline=deadline,
        )
        if not p7_current(p7_scope_key, p7_commit_sha256):
            from .phase8_evidence_egress_runtime import Phase8CurrentConflict

            raise Phase8CurrentConflict("Phase-7 head changed before Phase-8 commit")

    return fence


def _work_lease_token(view: Phase78WorkView, idempotency_key: str) -> _WorkLeaseToken:
    nonce = local_worker_nonce(idempotency_key)
    state = view.state
    if (
        view.status not in {
            OperationStatus.DISPATCH_CHECKPOINTED,
            OperationStatus.ACTIVE,
        }
        or state.claim_owner_id != PHASE78_LOCAL_WORKER_ID
        or state.claim_owner_epoch != PHASE78_LOCAL_WORKER_EPOCH
        or state.operation.dispatch_nonce != nonce
    ):
        raise Phase78CancellationError(Phase78CancellationReason.SUPERSEDED)
    return _WorkLeaseToken(
        operation_identity_sha256=view.operation_identity_sha256,
        claim_generation=state.operation.claim_generation,
        claim_owner_id=PHASE78_LOCAL_WORKER_ID,
        claim_owner_epoch=PHASE78_LOCAL_WORKER_EPOCH,
        local_worker_nonce=nonce,
    )


def _durable_cancellation_reason(
    view: Phase78WorkView,
) -> Phase78CancellationReason:
    receipt = view.cancellation
    if receipt is None:
        # A cancel state without the immutable receipt cannot be classified
        # after restart.  Fail closed as superseded instead of inventing a
        # caller cancellation reason.
        return Phase78CancellationReason.SUPERSEDED
    return Phase78CancellationReason(receipt.cancellation_reason)


def _work_fence(
    scheduler: Phase78ShadowScheduler,
    request,
    token: _WorkLeaseToken,
    deadline: TotalDeadline,
):
    """Reject every late adapter publication after cancel/reclaim/supersede."""

    def fence(stage: str) -> None:
        deadline.check(stage)
        current = scheduler.ledger.load(request.idempotency_key, deadline=deadline)
        state = current.state
        exact_generation = (
            current.operation_identity_sha256 == token.operation_identity_sha256
            and state.operation.claim_generation == token.claim_generation
            and state.claim_owner_id == token.claim_owner_id
            and state.claim_owner_epoch == token.claim_owner_epoch
            and state.operation.dispatch_nonce == token.local_worker_nonce
        )
        if exact_generation and current.status is OperationStatus.SUCCEEDED:
            raise _WorkAlreadyCompleted(current)
        if not exact_generation:
            raise Phase78CancellationError(Phase78CancellationReason.SUPERSEDED)
        if current.cancellation is not None:
            raise Phase78CancellationError(
                _durable_cancellation_reason(current)
            )
        if current.status in {
            OperationStatus.CANCEL_REQUESTED,
            OperationStatus.CANCEL_SIGNALLED,
            OperationStatus.CANCELLED,
        }:
            raise Phase78CancellationError(
                _durable_cancellation_reason(current)
            )
        if current.status not in {
            OperationStatus.DISPATCH_CHECKPOINTED,
            OperationStatus.ACTIVE,
        }:
            raise Phase78CancellationError(Phase78CancellationReason.SUPERSEDED)

    return fence


def _terminal_transition(
    scheduler: Phase78ShadowScheduler,
    request,
    *,
    succeeded: bool,
    reason_code: str,
    token: _WorkLeaseToken,
    deadline: object | None,
) -> Phase78WorkView:
    current = scheduler.ledger.load(request.idempotency_key, deadline=deadline)
    state = current.state
    if (
        current.operation_identity_sha256 != token.operation_identity_sha256
        or state.operation.claim_generation != token.claim_generation
        or state.claim_owner_id != token.claim_owner_id
        or state.claim_owner_epoch != token.claim_owner_epoch
        or state.operation.dispatch_nonce != token.local_worker_nonce
    ):
        raise Phase78CancellationError(Phase78CancellationReason.SUPERSEDED)
    if current.status is OperationStatus.SUCCEEDED and succeeded:
        return current
    if current.status is OperationStatus.FAILED and not succeeded:
        return current
    if current.cancellation is not None:
        raise Phase78CancellationError(
            _durable_cancellation_reason(current)
        )
    if current.status in {
        OperationStatus.CANCEL_REQUESTED,
        OperationStatus.CANCEL_SIGNALLED,
        OperationStatus.CANCELLED,
    }:
        raise Phase78CancellationError(
            _durable_cancellation_reason(current)
        )
    if current.status not in {
        OperationStatus.DISPATCH_CHECKPOINTED,
        OperationStatus.ACTIVE,
    }:
        raise Phase78CancellationError(Phase78CancellationReason.SUPERSEDED)
    method = scheduler.ledger.complete if succeeded else scheduler.ledger.fail
    try:
        result = method(
            request.idempotency_key,
            request_idempotency_key=(
                f"{request.idempotency_key}:complete"
                if succeeded
                else f"{request.idempotency_key}:fail:{reason_code.lower()}"
            ),
            expected_claim_generation=token.claim_generation,
            claim_owner_id=token.claim_owner_id,
            claim_owner_epoch=token.claim_owner_epoch,
            local_worker_nonce=token.local_worker_nonce,
            reason_code=reason_code,
            occurred_at=request.work["completed_at"],
            deadline=deadline,
        )
    except Phase78WorkContractError as primary:
        # A cancellation can linearize after the pre-transition load but
        # before Phase 4 acquires its commit guard.  Translate that race from
        # the ledger's generic transition refusal back to the durable reason.
        refreshed = scheduler.ledger.load(
            request.idempotency_key, deadline=deadline
        )
        if refreshed.cancellation is not None:
            raise Phase78CancellationError(
                _durable_cancellation_reason(refreshed)
            ) from primary
        raise
    return result.view


def _phase8_scope(request, project_id: str) -> str:
    return canonical_sha256(
        {
            "schema_version": "phase8-reference-scope-v1",
            "workflow_id": request.workflow_id,
            "project_id": project_id,
            "normalized_path": request.phase3_artifact_occurrence["normalized_path"],
            "reference_id": request.reference["reference_id"],
        }
    )


def run_local_phase78_worker(
    *,
    settings: Phase78Settings,
    scheduler: Phase78ShadowScheduler,
    request,
    project_id: str,
    actor_id: str,
    current_verifier,
    deadline: TotalDeadline,
    trusted_evaluated_at: int,
) -> Phase78PipelineResult:
    """Run one durable job synchronously through every real local adapter."""

    del actor_id  # caller/subject equality was verified at the service boundary
    claim = scheduler.claim_and_checkpoint(
        idempotency_key=request.idempotency_key,
        claimed_at=request.work["claimed_at"],
        checkpointed_at=request.work["checkpointed_at"],
        deadline=deadline,
    )
    if claim.view.status is OperationStatus.SUCCEEDED:
        return load_local_phase78_status(
            settings=settings,
            view=claim.view,
            request=request,
            project_id=project_id,
            current_verifier=current_verifier,
            deadline=deadline,
            trusted_evaluated_at=trusted_evaluated_at,
        )
    work_token = _work_lease_token(claim.view, request.idempotency_key)
    work_fence = _work_fence(scheduler, request, work_token, deadline)

    upstream_current = current_verifier.current_callback(deadline=deadline)
    upstream_fence = current_verifier.fence(
        phase3_artifact_state=request.phase3_artifact_state,
        phase3_artifact_occurrence=request.phase3_artifact_occurrence,
        phase6_access_proof=request.phase6_access_proof,
        deadline=deadline,
    )

    def phase7_fence(stage: str) -> None:
        work_fence(stage)
        upstream_fence(stage)

    p7_store = Phase7GroundingStore(settings.required_path("phase7_database"))
    p7_result: GroundingCommitResult | None = None
    binding: ReferenceBindingResult | None = None
    approval: ApprovalResult | None = None
    decision: DecisionResult | None = None
    try:
        p7_result = p7_store.record_grounding_bundle(
            idempotency_key=_phase7_key(request.idempotency_key),
            phase3_artifact_state=request.phase3_artifact_state,
            phase3_artifact_occurrence=request.phase3_artifact_occurrence,
            phase6_access_proof=request.phase6_access_proof,
            role_output_bytes=request.role_outputs,
            manifest_bytes=request.manifests,
            context_bytes=request.contexts,
            current_head_verifier=upstream_current,
            deadline=deadline,
            fence_hook=phase7_fence,
        )
        p7_bundle = p7_store.load_bundle(
            _phase7_key(request.idempotency_key),
            deadline=deadline,
            fence_hook=phase7_fence,
        )
        if (
            p7_result.aggregate_verdict != "PASS"
            or p7_result.grounding_valid is not True
        ):
            work = _terminal_transition(
                scheduler,
                request,
                succeeded=False,
                reason_code="PHASE7_GROUNDING_INDETERMINATE",
                token=work_token,
                deadline=deadline,
            )
            return Phase78PipelineResult(
                project_id,
                request.idempotency_key,
                "indeterminate",
                work.as_dict(),
                phase7=p7_result.as_dict(),
                replayed=claim.replayed,
            )

        p7_current = _p7_current_callback(p7_store, upstream_current, deadline)
        phase8_fence = _phase8_fence(
            deadline=deadline,
            current_verifier=current_verifier,
            request=request,
            p7_current=p7_current,
            p7_scope_key=p7_result.scope_key,
            p7_commit_sha256=p7_result.commit_sha256,
            work_fence=work_fence,
        )
        p8_store = Phase8EvidenceEgressStore(
            settings.required_path("phase8_database"),
            settings.required_path("cas_root"),
            enabled=True,
        )
        phase8_fence("trusted_preflight_load_before")
        trusted_preflight = p8_store.load_trusted_approval_preflight(
            request.approval["trusted_preflight_sha256"],
            deadline=deadline,
        ).preflight
        phase8_fence("trusted_preflight_load_after")
        preflight_binding_sha256 = str(trusted_preflight["binding_sha256"])
        persisted_binding = p8_store.load_current_reference_binding(
            str(trusted_preflight["scope_key"]),
            phase7_current_head_verifier=p7_current,
            expected_binding_sha256=preflight_binding_sha256,
            deadline=deadline,
        )
        phase8_fence("persisted_reference_load_after")
        if (
            trusted_preflight["phase7_scope_key"] != p7_result.scope_key
            or trusted_preflight["phase7_commit_sha256"]
            != p7_result.commit_sha256
        ):
            from .phase8_evidence_egress_runtime import Phase8CurrentConflict

            raise Phase8CurrentConflict(
                "trusted preflight differs from the current Phase-7 result"
            )
        binding = p8_store.record_reference_binding(
            idempotency_key=_binding_key(request.idempotency_key),
            logical_id=request.reference["logical_id"],
            phase3_artifact_state=request.phase3_artifact_state,
            phase3_artifact_occurrence=request.phase3_artifact_occurrence,
            phase6_access_proof=request.phase6_access_proof,
            phase7_result=p7_bundle["result"],
            phase7_receipt=p7_bundle["receipt"],
            phase7_effective_verdict=p7_bundle["effective_verdict"],
            reference_package_blob=persisted_binding.binding[
                "reference_package_blob"
            ],
            reference_receipt_blob=persisted_binding.binding[
                "reference_receipt_blob"
            ],
            phase7_current_head_verifier=p7_current,
            expected_current_binding_sha256=request.reference[
                "expected_current_binding_sha256"
            ],
            deadline=deadline,
            adapter_fence=phase8_fence,
        )
        if binding.binding_sha256 != preflight_binding_sha256:
            from .phase8_evidence_egress_runtime import Phase8CurrentConflict

            raise Phase8CurrentConflict(
                "trusted preflight reference binding differs from exact replay"
            )
        approval = p8_store.issue_approval(
            idempotency_key=_approval_key(request.idempotency_key),
            approval_id=request.approval["approval_id"],
            binding_sha256=binding.binding_sha256,
            issuer_id=request.approval["issuer_id"],
            issuer_generation=request.approval["issuer_generation"],
            subject_id=request.approval["subject_id"],
            subject_generation=request.approval["subject_generation"],
            logical_issued_at=request.approval["logical_issued_at"],
            not_before=request.approval["not_before"],
            expires_at=request.approval["expires_at"],
            data_egress_request=request.approval["data_egress_request"],
            trusted_preflight_sha256=request.approval[
                "trusted_preflight_sha256"
            ],
            phase7_current_head_verifier=p7_current,
            successor_of=request.approval["successor_of"],
            expected_predecessor_event_sha256=request.approval[
                "expected_predecessor_event_sha256"
            ],
            deadline=deadline,
            adapter_fence=phase8_fence,
        )
        decision = p8_store.evaluate_egress(
            idempotency_key=_decision_key(request.idempotency_key),
            binding_sha256=binding.binding_sha256,
            data_egress_request=request.approval["data_egress_request"],
            evaluated_at=int(trusted_preflight["decision_evaluated_at"]),
            phase7_current_head_verifier=p7_current,
            expected_previous_decision_sha256=request.approval[
                "expected_previous_decision_sha256"
            ],
            deadline=deadline,
            adapter_fence=phase8_fence,
        )
        phase8_fence("effective_decision_load_before")
        effective_decision = p8_store.load_current_decision(
            binding.scope_key,
            evaluated_at=trusted_evaluated_at,
            phase7_current_head_verifier=p7_current,
            deadline=deadline,
        )
        phase8_fence("effective_decision_load_after")
        if effective_decision["status"] not in {"AUTHORIZED", "DENIED"}:
            raise RuntimeError("Phase-8 returned an unsupported shadow decision")
        try:
            work = _terminal_transition(
                scheduler,
                request,
                succeeded=True,
                reason_code="LOCAL_PHASE78_SHADOW_SUCCEEDED",
                token=work_token,
                deadline=deadline,
            )
        except Phase78DeadlineError as exc:
            raise Phase78OutcomeUncertain(request.idempotency_key) from exc
        return Phase78PipelineResult(
            project_id,
            request.idempotency_key,
            "shadow_authorized"
            if effective_decision["status"] == "AUTHORIZED"
            else "denied",
            work.as_dict(),
            phase7=p7_result.as_dict(),
            reference_binding=_binding_summary(binding),
            approval=_approval_summary(approval),
            decision=_decision_summary(effective_decision),
            replayed=claim.replayed or all(
                item.replayed for item in (p7_result, binding, approval, decision)
            ),
        )
    except _WorkAlreadyCompleted as completed:
        return load_local_phase78_status(
            settings=settings,
            view=completed.view,
            request=request,
            project_id=project_id,
            current_verifier=current_verifier,
            deadline=deadline,
            trusted_evaluated_at=trusted_evaluated_at,
        )
    except ReferenceMaterializationError as exc:
        blocker = structured_reference_unavailable(exc)
        work = _terminal_transition(
            scheduler,
            request,
            succeeded=False,
            reason_code="PHASE8_REFERENCE_UNAVAILABLE",
            token=work_token,
            deadline=deadline,
        )
        return Phase78PipelineResult(
            project_id,
            request.idempotency_key,
            "unavailable",
            work.as_dict(),
            phase7=None if p7_result is None else p7_result.as_dict(),
            blocker=blocker,
            replayed=claim.replayed,
        )
    except Phase78DeadlineError as exc:
        # The durable checkpoint deliberately remains replayable.  A commit in
        # an inner store may already exist, so callers must reuse this exact key.
        raise Phase78OutcomeUncertain(request.idempotency_key) from exc
    except Phase78OutcomeUncertain:
        # The terminal commit may already have crossed its durable boundary.
        # Preserve the checkpoint for exact-key query/replay; never rewrite an
        # uncertain result into a deterministic FAILED terminal state.
        raise
    except Phase78CancellationError:
        raise
    except BaseException as primary:
        try:
            _terminal_transition(
                scheduler,
                request,
                succeeded=False,
                reason_code="LOCAL_PHASE78_SHADOW_FAILED",
                token=work_token,
                deadline=deadline,
            )
        except BaseException as cleanup:
            if hasattr(primary, "add_note"):
                primary.add_note(
                    "Phase 7+8 work-ledger failure recording also failed: "
                    f"{type(cleanup).__name__}"
                )
        raise


def load_local_phase78_status(
    *,
    settings: Phase78Settings,
    view: Phase78WorkView,
    request,
    project_id: str,
    current_verifier,
    deadline: TotalDeadline,
    trusted_evaluated_at: int,
) -> Phase78PipelineResult:
    """Load history while deriving effective currentness from live heads."""

    upstream_current = current_verifier.current_callback(deadline=deadline)
    p7_store = Phase7GroundingStore(settings.required_path("phase7_database"))
    phase7_wire: dict[str, object] | None = None
    decision_wire: dict[str, object] | None = None
    binding_wire: dict[str, object] | None = None
    work_succeeded = (
        view.status is OperationStatus.SUCCEEDED
        and view.cancellation is None
    )
    outcome = (
        "cancelled" if view.cancellation is not None else view.status.value
    )
    blocker: dict[str, object] | None = None
    try:
        history = p7_store.load_bundle(_phase7_key(request.idempotency_key), deadline=deadline)
        phase7_wire = dict(history["result"])
        try:
            current_verifier.verify(
                phase3_artifact_state=request.phase3_artifact_state,
                phase3_artifact_occurrence=request.phase3_artifact_occurrence,
                phase6_access_proof=request.phase6_access_proof,
                deadline=deadline,
            )
        except (Phase78DeadlineError, Phase78CancellationError):
            raise
        except Exception as exc:
            return Phase78PipelineResult(
                project_id,
                request.idempotency_key,
                "denied" if work_succeeded else outcome,
                view.as_dict(),
                phase7=phase7_wire,
                blocker={
                    "schema_version": "phase78-current-unavailable-v1",
                    "reason_code": "SOURCE_HEAD_DRIFT",
                    "error_code": getattr(
                        exc, "code", "PHASE78_CURRENT_HEAD_MISMATCH"
                    ),
                    "authoritative": False,
                    "authority_transferred": False,
                    "dispatch_performed": False,
                },
                replayed=True,
            )
        p7_current = _p7_current_callback(p7_store, upstream_current, deadline)
        p8_store = Phase8EvidenceEgressStore(
            settings.required_path("phase8_database"),
            settings.required_path("cas_root"),
            enabled=True,
        )
        scope = _phase8_scope(request, project_id)
        try:
            current_binding = p8_store.load_current_reference_binding(
                scope,
                phase7_current_head_verifier=p7_current,
                deadline=deadline,
            )
            binding_wire = _binding_summary(current_binding)
        except Phase8NotFound:
            pass
        try:
            current_decision = p8_store.load_current_decision(
                scope,
                evaluated_at=trusted_evaluated_at,
                phase7_current_head_verifier=p7_current,
                deadline=deadline,
            )
            decision_wire = _decision_summary(current_decision)
            if work_succeeded:
                outcome = (
                    "shadow_authorized"
                    if decision_wire["status"] == "AUTHORIZED"
                    else "denied"
                )
        except Phase8NotFound:
            if (
                work_succeeded
                and phase7_wire["aggregate_verdict"] != "PASS"
            ):
                outcome = "indeterminate"
    except Phase7GroundingNotFound:
        pass
    except Exception as exc:
        # History remains visible, but a current head can never be inferred
        # from an unverifiable cross-store state.
        if work_succeeded:
            outcome = "denied"
        blocker = {
            "schema_version": "phase78-current-unavailable-v1",
            "reason_code": "CURRENT_HEAD_UNAVAILABLE",
            "error_code": getattr(exc, "code", "PHASE78_CURRENT_HEAD_MISMATCH"),
            "authoritative": False,
            "authority_transferred": False,
            "dispatch_performed": False,
        }
    return Phase78PipelineResult(
        project_id,
        request.idempotency_key,
        outcome,
        view.as_dict(),
        phase7=phase7_wire,
        reference_binding=binding_wire,
        decision=decision_wire,
        blocker=blocker,
        replayed=True,
    )


def revoke_local_phase78_approval(
    *,
    settings: Phase78Settings,
    project_id: str,
    actor_id: str,
    idempotency_key: str,
    approval_id: str,
    expected_event_sha256: str,
    revoked_at: int,
    reason_code: str,
    deadline: TotalDeadline,
) -> dict[str, object]:
    """Revoke only a locally issued approval belonging to the caller/project."""

    store = Phase8EvidenceEgressStore(
        settings.required_path("phase8_database"),
        settings.required_path("cas_root"),
        enabled=True,
    )
    loaded = store.load_approval(approval_id, deadline=deadline)
    if loaded.approval["issuer"]["id"] != actor_id:
        from .phase8_evidence_egress_runtime import Phase8CurrentConflict

        raise Phase8CurrentConflict("approval issuer differs from authenticated actor")
    binding = store.load_reference_binding(
        loaded.approval["binding_sha256"], deadline=deadline
    )
    if binding.binding["authority_coordinate"]["project_id"] != project_id:
        from .phase8_evidence_egress_runtime import Phase8CurrentConflict

        raise Phase8CurrentConflict("approval belongs to another project")
    result = store.revoke_approval(
        idempotency_key=idempotency_key,
        approval_id=approval_id,
        expected_event_sha256=expected_event_sha256,
        revoked_at=revoked_at,
        reason_code=reason_code,
        deadline=deadline,
    )
    return {
        "schema_version": "phase78-shadow-approval-revoke-result-v1",
        "project_id": project_id,
        "approval": _approval_summary(result),
        "authoritative": False,
        "authority_transferred": False,
        "dispatch_performed": False,
        "provider_call_performed": False,
        "outbox_dispatch_performed": False,
    }


__all__ = [
    "PHASE78_PIPELINE_RESULT_SCHEMA",
    "Phase78PipelineResult",
    "load_local_phase78_status",
    "revoke_local_phase78_approval",
    "run_local_phase78_worker",
]
