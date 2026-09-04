from __future__ import annotations

import fcntl
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Protocol

from ..adapters.infrastructure.commands import CommandRunner
from ..adapters.models.backends import build_model_backends
from ..adapters.models.dispatcher import ModelDispatcher
from ..domain import ExecutionResult, StepContext
from ..governance.overrides import (
    DELIVER_SNAPSHOT,
    DeliveryOverride,
    OverrideProvider,
    default_override_provider,
)
from ..steps.catalog import contract_for
from ..steps.prompting import PromptRenderer
from ..steps.validators import NativeArtifactValidator, validator_for
from .domain import AuditOutcome, AuditProfile, AuditRecord, AuditSnapshot, AuditStatus
from .ledger import has_unresolved_blocking
from .acceptance import (
    RECEIPT_PATH as FINAL_ACCEPTANCE_RECEIPT_PATH,
    build_final_acceptance_receipt,
    verify_final_acceptance_receipt,
)
from .incremental import IncrementalAuditService, StageCheck
from .persistence import atomic_write_json as _atomic_write_json
from .persistence import utc_now as _utc_now


class JudgeExecutor(Protocol):
    def execute(self, context: StepContext) -> ExecutionResult: ...


Fingerprinter = Callable[[Path, str], str]


class FinalAuditService:
    """Run final analysis, with an explicit optional acceptance boundary.

    The service may write generated verification reports, compiled PDF output,
    compatibility judge artifacts, and its own ``.factory/audits`` records. It
    never copies into ``papers/``, packages a submission, archives a project,
    or changes workflow state.  ``analysis_only=True`` also forbids final
    submission markers, delivery overrides, and acceptance receipts.
    """

    profile = AuditProfile.FINAL.value

    def __init__(
        self,
        factory_root: Path,
        judge: JudgeExecutor,
        validator: NativeArtifactValidator,
        runner: CommandRunner,
        fingerprinter: Fingerprinter | None = None,
        override_provider: OverrideProvider | None = None,
        technical_flow_validation: bool = False,
    ) -> None:
        self.factory_root = factory_root.resolve()
        self.judge = judge
        self.validator = validator
        self.runner = runner
        self.fingerprinter = fingerprinter
        self.override_provider = override_provider or default_override_provider(
            self.factory_root
        )
        self.technical_flow_validation = bool(technical_flow_validation)

    def _technical_authorization(self, project: Path):
        if not self.technical_flow_validation:
            return None
        from ..solver_input_coverage import technical_solver_drift_authorization

        return technical_solver_drift_authorization(project)

    def run(
        self,
        context: StepContext,
        *,
        compile_pdf: bool = True,
        reuse_pass: bool = True,
        analysis_only: bool = True,
        workflow_id: str | None = None,
        run_generation: str | None = None,
    ) -> AuditOutcome:
        project = context.project_dir.resolve()
        if analysis_only:
            if workflow_id is not None or run_generation is not None:
                raise ValueError(
                    "Phase9 coordinates are not valid for analysis-only final audit"
                )

        def run_with_audit_lock() -> AuditOutcome:
            lock_path = project / ".factory" / "audits" / ".lock"
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            with lock_path.open("a+", encoding="ascii") as lock:
                try:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return self._busy(project)
                try:
                    return self._run_unlocked(
                        context,
                        compile_pdf=compile_pdf,
                        reuse_pass=reuse_pass,
                        analysis_only=analysis_only,
                        workflow_id=workflow_id,
                        run_generation=run_generation,
                    )
                finally:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

        if analysis_only:
            return run_with_audit_lock()

        # A non-analysis invocation asks this service to elevate a completed
        # judgment into acceptance.  Hold the shared Authority/project lease
        # from before the audit lock or judge writes through the acceptance
        # commit.  A sanctioned Phase9 writer therefore linearizes wholly
        # before this call (which refuses without creating the audit lock) or
        # wholly after it.  The leaf acceptance lease is same-thread reentrant.
        from ..phase9_delivery_fence import delivery_side_effect_commit_lease

        with delivery_side_effect_commit_lease(
            project,
            operation="acceptance",
            workflow_id=workflow_id,
            run_generation=run_generation,
        ):
            return run_with_audit_lock()

    def _run_unlocked(
        self,
        context: StepContext,
        *,
        compile_pdf: bool,
        reuse_pass: bool,
        analysis_only: bool,
        workflow_id: str | None,
        run_generation: str | None,
    ) -> AuditOutcome:
        project = context.project_dir.resolve()
        base = project.name
        technical_authorization = self._technical_authorization(project)
        if self.technical_flow_validation and technical_authorization is None:
            return self._failure(
                project,
                decision="TECHNICAL_FLOW_AUTHORIZATION_INVALID",
                status=AuditStatus.FAIL,
                error_class="PERMANENT_TECHNICAL_FLOW_AUTHORIZATION",
                returncode=2,
            )
        if technical_authorization is not None:
            # A technical penetration run must execute the live judges.  It
            # may not consume a cached PASS bound to an earlier snapshot.
            reuse_pass = False

        from ..storage import SQLiteStateStore

        decision_store = SQLiteStateStore(project)
        if decision_store.exists and decision_store.contest_policy() is not None:
            content_freeze = decision_store.decision("content_freeze")
            if technical_authorization is None and not (
                content_freeze is not None
                and content_freeze.get("approved") is True
                and (content_freeze.get("receipt_verification") or {}).get("valid")
                is True
            ):
                return self._failure(
                    project,
                    decision="CONTENT_FREEZE_EVIDENCE_INVALID",
                    status=AuditStatus.FAIL,
                    error_class="PERMANENT_CONTENT_FREEZE_RECEIPT",
                    returncode=2,
                )

        if self._has_stub(project) or (
            self._unresolved_blocking(project) and not self.technical_flow_validation
        ):
            return self._failure(
                project,
                decision="CONTENT_NOT_READY",
                status=AuditStatus.FAIL,
                error_class="PERMANENT_DELIVERY_ACCEPTANCE",
                returncode=2,
            )

        pdf = project / f"{base}_paper.pdf"
        packets_prepared = False
        ablate_judge = os.getenv("ABLATE_NO_JUDGE", "0").lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        if ablate_judge:
            # A no-judge run is an auditable non-delivery terminal, never a
            # request to consume a cached PASS or delivery override.
            reuse_pass = False
        prepare_packets = (
            None if ablate_judge else getattr(self.judge, "prepare_packets", None)
        )

        def prepare_for_snapshot() -> AuditOutcome | None:
            nonlocal packets_prepared
            if packets_prepared or not callable(prepare_packets):
                return None
            prepared = prepare_packets(context)
            if prepared.returncode == 0:
                packets_prepared = True
                return None
            continued = self._judge_failure_override(project, prepared)
            if continued is not None:
                return self._finish_judge_result(
                    project,
                    context,
                    continued,
                    analysis_only=analysis_only,
                    workflow_id=workflow_id,
                    run_generation=run_generation,
                )
            return self._failure(
                project,
                decision="INDETERMINATE_REVIEW",
                status=AuditStatus.INDETERMINATE,
                error_class=prepared.error_class or "TRANSIENT_JUDGE_PACKET",
                returncode=prepared.returncode,
                evidence=prepared.metadata,
            )

        # Reuse is checked before compilation so a standalone audit can be
        # consumed by Step 16 without regenerating timestamp-bearing PDF bytes.
        if (
            reuse_pass
            and pdf.is_file()
            and pdf.stat().st_size > 0
        ):
            packet_failure = prepare_for_snapshot()
            if packet_failure is not None:
                return packet_failure
            candidate = self._snapshot(project)
            cached = self._load_reusable(
                project, candidate, analysis_only=analysis_only
            )
            if cached is not None:
                execution = ExecutionResult.succeeded(
                    audit_status=cached.status.value,
                    audit_snapshot=candidate.snapshot_id,
                    audit_reused=True,
                    audit_result=str(
                        (
                            self._analysis_latest_path(project)
                            if analysis_only
                            else self._latest_path(project)
                        ).relative_to(project)
                    ),
                    final_decision=cached.decision,
                    gate2_delivery_override=cached.override,
                )
                return AuditOutcome(execution, cached, candidate)

        if compile_pdf:
            compiled = self.runner.run(
                project,
                [self.factory_root / "compile_paper.sh", project, base],
                label="compile_paper",
                timeout_seconds=1_800,
                cwd=self.factory_root,
            )
            if not compiled.accepted or not pdf.is_file() or pdf.stat().st_size == 0:
                return self._failure(
                    project,
                    decision="COMPILATION_FAILED",
                    status=AuditStatus.FAIL,
                    error_class="TRANSIENT_COMPILATION",
                    returncode=compiled.returncode,
                )
        else:
            if not pdf.is_file() or pdf.stat().st_size == 0:
                return self._failure(
                    project,
                    decision="CONTENT_NOT_READY",
                    status=AuditStatus.FAIL,
                    error_class="MISSING_COMPILED_PDF",
                    returncode=2,
                )

        if self.fingerprinter is None:
            from ..bibliography import verify_bibliography_receipt

            bibliography_valid, bibliography_errors, _ = (
                verify_bibliography_receipt(project, base)
            )
            if not bibliography_valid:
                return self._failure(
                    project,
                    decision="BIBLIOGRAPHY_EVIDENCE_INVALID",
                    status=AuditStatus.FAIL,
                    error_class="PERMANENT_BIBLIOGRAPHY_RECEIPT",
                    returncode=2,
                    evidence={"bibliography_errors": bibliography_errors},
                )

        acceptance_checks, acceptance = self._run_acceptance_checks(project)
        if acceptance is not None and not self.technical_flow_validation:
            return self._failure(
                project,
                decision="CONTENT_NOT_READY",
                status=AuditStatus.FAIL,
                error_class="PERMANENT_DELIVERY_ACCEPTANCE",
                returncode=acceptance.returncode,
                evidence={
                    "failed_check": acceptance.metadata.get("check"),
                    "paper_checks": acceptance_checks,
                },
            )

        visual = self._run_visual_gate(project, pdf)
        if visual is not None:
            return self._failure(
                project,
                decision="VISUAL_GATE_UNAVAILABLE",
                status=AuditStatus.INDETERMINATE,
                error_class="TRANSIENT_VISUAL_GATE",
                returncode=visual.returncode,
            )

        packet_failure = prepare_for_snapshot()
        if packet_failure is not None:
            return packet_failure

        snapshot = self._snapshot(project)
        if reuse_pass:
            cached = self._load_reusable(
                project, snapshot, analysis_only=analysis_only
            )
            if cached is not None:
                execution = ExecutionResult.succeeded(
                    audit_status=cached.status.value,
                    audit_snapshot=snapshot.snapshot_id,
                    audit_reused=True,
                    audit_result=str(
                        (
                            self._analysis_latest_path(project)
                            if analysis_only
                            else self._latest_path(project)
                        ).relative_to(project)
                    ),
                    final_decision=cached.decision,
                    gate2_delivery_override=cached.override,
                )
                return AuditOutcome(execution, cached, snapshot)

        if ablate_judge:
            return self._finish_ablation(
                project, snapshot, analysis_only=analysis_only
            )

        delivery_override = (
            None
            if analysis_only
            else self._delivery_override(project, snapshot.snapshot_id)
        )
        if delivery_override is not None:
            return self._finish_judge_result(
                project,
                context,
                ExecutionResult.succeeded(
                    judge_completed=False,
                    judge_verdict=delivery_override.source_verdict,
                    gate2_delivery_override=True,
                    gate2_override_id=delivery_override.override_id,
                ),
                snapshot=snapshot,
                analysis_only=analysis_only,
                workflow_id=workflow_id,
                run_generation=run_generation,
            )

        execute_prepared = getattr(self.judge, "execute_prepared", None)
        judge_result = (
            execute_prepared(context)
            if packets_prepared and callable(execute_prepared)
            else self.judge.execute(context)
        )
        return self._finish_judge_result(
            project,
            context,
            judge_result,
            snapshot=snapshot,
            analysis_only=analysis_only,
            workflow_id=workflow_id,
            run_generation=run_generation,
        )

    def run_project(
        self,
        project: Path,
        *,
        compile_pdf: bool = True,
        reuse_pass: bool = True,
        analysis_only: bool = True,
        workflow_id: str | None = None,
        run_generation: str | None = None,
    ) -> AuditOutcome:
        context = StepContext(
            project.resolve(),
            project.name,
            16,
            1,
            contract_for(16).timeout_seconds,
            0,
        )
        return self.run(
            context,
            compile_pdf=compile_pdf,
            reuse_pass=reuse_pass,
            analysis_only=analysis_only,
            workflow_id=workflow_id,
            run_generation=run_generation,
        )

    def _finish_judge_result(
        self,
        project: Path,
        context: StepContext,
        judge_result: ExecutionResult,
        *,
        snapshot: AuditSnapshot | None = None,
        analysis_only: bool,
        workflow_id: str | None,
        run_generation: str | None,
    ) -> AuditOutcome:
        snapshot = snapshot or self._snapshot(project)
        override_record = (
            None
            if analysis_only
            else self._delivery_override(project, snapshot.snapshot_id)
        )
        if judge_result.returncode != 0 and override_record is None:
            return self._failure(
                project,
                snapshot=snapshot,
                decision=str(
                    judge_result.metadata.get("judge_verdict")
                    or "INDETERMINATE_REVIEW"
                ),
                status=AuditStatus.INDETERMINATE,
                error_class=judge_result.error_class or "TRANSIENT_JUDGE_INFRASTRUCTURE",
                returncode=judge_result.returncode,
                evidence=judge_result.metadata,
            )

        resume_after = judge_result.metadata.get("resume_after_step")
        override = override_record is not None
        judge_completed = (
            judge_result.returncode == 0
            and judge_result.metadata.get("judge_completed") is not False
        )
        if judge_completed:
            routed = self._run_decision_router(project)
            if routed is not None:
                if not override:
                    return self._failure(
                        project,
                        snapshot=snapshot,
                        decision="JUDGE_ROUTING_FAILED",
                        status=AuditStatus.INDETERMINATE,
                        error_class="TRANSIENT_JUDGE_ROUTING",
                        returncode=routed.returncode,
                    )
                decision = "JUDGE_ROUTING_FAILED"
            else:
                decision = self._decision(project, prefer_new=override)
        else:
            decision = str(
                judge_result.metadata.get("judge_verdict") or "INDETERMINATE_REVIEW"
            )

        if resume_after is not None or decision in {
            "REOPEN_REVISION_TEXT",
            "REOPEN_REVISION_MODEL",
        }:
            if not override:
                resolved_resume = int(
                    resume_after
                    if resume_after is not None
                    else (
                        11
                        if decision == "REOPEN_REVISION_TEXT"
                        else self.validator._gate2_resume(project, decision)
                    )
                )
                return self._failure(
                    project,
                    snapshot=snapshot,
                    decision=decision,
                    status=AuditStatus.FAIL,
                    error_class="AUDIT_REPAIR_REQUIRED",
                    returncode=0,
                    resume_after_step=resolved_resume,
                    judge_completed=judge_completed,
                    evidence=judge_result.metadata,
                )
        elif decision != "PASS" and not override:
            return self._failure(
                project,
                snapshot=snapshot,
                decision=decision or "INDETERMINATE_REVIEW",
                status=(
                    AuditStatus.INDETERMINATE
                    if "INDETERMINATE" in decision or not decision
                    else AuditStatus.FAIL
                ),
                error_class="PERMANENT_FINAL_JUDGE",
                returncode=2,
                judge_completed=judge_completed,
                evidence=judge_result.metadata,
            )
        elif decision == "PASS" and not judge_completed and not override:
            return self._failure(
                project,
                snapshot=snapshot,
                decision="INDETERMINATE_REVIEW",
                status=AuditStatus.INDETERMINATE,
                error_class="TRANSIENT_JUDGE_INCOMPLETE",
                returncode=judge_result.returncode or 75,
                judge_completed=False,
                evidence=judge_result.metadata,
            )

        if override and (decision != "PASS" or not judge_completed):
            self._record_override_decision(project, decision, judge_result.metadata)

        try:
            current_snapshot = self._snapshot(project)
        except (OSError, ValueError) as exc:
            return self._failure(
                project,
                snapshot=snapshot,
                decision="CONTENT_FREEZE_EVIDENCE_INVALID",
                status=AuditStatus.INDETERMINATE,
                error_class="PERMANENT_CONTENT_FREEZE_RECEIPT",
                returncode=2,
                judge_completed=judge_completed,
                evidence={"approval_error": str(exc)},
            )
        if current_snapshot.snapshot_id != snapshot.snapshot_id:
            return self._failure(
                project,
                snapshot=current_snapshot,
                decision="SNAPSHOT_CHANGED_DURING_FINAL_AUDIT",
                status=AuditStatus.INDETERMINATE,
                error_class="TRANSIENT_FINAL_AUDIT_MUTATION",
                returncode=75,
                judge_completed=judge_completed,
                evidence={
                    "before_snapshot": snapshot.snapshot_id,
                    "after_snapshot": current_snapshot.snapshot_id,
                },
            )
        if decision == "PASS" and judge_completed:
            receipt = self._build_and_verify_receipt(project, snapshot.snapshot_id)
            if receipt is not None:
                return self._failure(
                    project,
                    snapshot=snapshot,
                    decision=decision,
                    status=AuditStatus.INDETERMINATE,
                    error_class="PERMANENT_JUDGMENT_RECEIPT",
                    returncode=receipt.returncode,
                    judge_completed=True,
                )

        if self.technical_flow_validation:
            technical_authorization = self._technical_authorization(project)
            if technical_authorization is None:
                return self._failure(
                    project,
                    snapshot=snapshot,
                    decision="TECHNICAL_FLOW_AUTHORIZATION_INVALID",
                    status=AuditStatus.INDETERMINATE,
                    error_class="PERMANENT_TECHNICAL_FLOW_AUTHORIZATION",
                    returncode=2,
                    judge_completed=judge_completed,
                )
            status = (
                AuditStatus.PASS
                if decision == "PASS" and judge_completed
                else AuditStatus.OVERRIDDEN
            )
            record = AuditRecord(
                snapshot_id=snapshot.snapshot_id,
                base=project.name,
                profile=self.profile,
                status=status,
                decision=decision,
                judge_completed=judge_completed,
                delivery_allowed=False,
                created_at=_utc_now(),
                error_class="PERMANENT_TECHNICAL_FLOW_NO_DELIVERY",
                returncode=2,
                override=False,
                evidence={
                    "judge": judge_result.metadata,
                    "technical_flow_validation": True,
                    "technical_authorization": str(
                        technical_authorization.path.relative_to(project)
                    ),
                    "technical_authorization_sha256": (
                        technical_authorization.sha256
                    ),
                    "content_freeze_approved": False,
                    "quality_pass_fabricated": False,
                    "delivery_allowed": False,
                },
            )
            record = self._persist(project, snapshot, record)
            execution = ExecutionResult.failed(
                "PERMANENT_TECHNICAL_FLOW_NO_DELIVERY",
                returncode=2,
                audit_status=record.status.value,
                audit_snapshot=snapshot.snapshot_id,
                audit_result=str(
                    self._analysis_latest_path(project).relative_to(project)
                ),
                final_decision=decision,
                judge_completed=judge_completed,
                technical_flow_validation=True,
                quality_pass_fabricated=False,
                delivery_allowed=False,
            )
            return AuditOutcome(execution, record, snapshot)

        if analysis_only:
            status = AuditStatus.PASS
            record = AuditRecord(
                snapshot_id=snapshot.snapshot_id,
                base=project.name,
                profile=self.profile,
                status=status,
                decision=decision,
                judge_completed=judge_completed,
                delivery_allowed=False,
                created_at=_utc_now(),
                override=False,
                evidence={
                    "judge": judge_result.metadata,
                    "analysis_only": True,
                    "delivery_allowed": False,
                },
            )
            record = self._persist(project, snapshot, record)
            execution = ExecutionResult.succeeded(
                audit_status=record.status.value,
                audit_snapshot=snapshot.snapshot_id,
                audit_result=str(
                    self._analysis_latest_path(project).relative_to(project)
                ),
                final_decision=decision,
                gate2_delivery_override=False,
                analysis_only=True,
                delivery_allowed=False,
            )
            return AuditOutcome(execution, record, snapshot)

        # A final judgment is not itself authority to create acceptance or
        # final-submission artifacts.  Reclassify immediately before that
        # boundary; the receipt builder independently repeats this check.
        try:
            from ..phase9_delivery_fence import (
                Phase9DeliveryFenceError,
                delivery_side_effect_commit_lease,
                require_delivery_side_effect_authority,
            )

            require_delivery_side_effect_authority(
                project,
                operation="acceptance",
                workflow_id=workflow_id,
                run_generation=run_generation,
            )
        except (OSError, ValueError) as exc:
            return self._failure(
                project,
                snapshot=snapshot,
                decision=decision,
                status=AuditStatus.INDETERMINATE,
                error_class="PERMANENT_PHASE9_DELIVERY_DISABLED",
                returncode=2,
                judge_completed=judge_completed,
                evidence={"delivery_fence_error": str(exc)},
            )

        status = (
            AuditStatus.PASS
            if decision == "PASS" and judge_completed
            else AuditStatus.OVERRIDDEN
        )
        if status is AuditStatus.OVERRIDDEN and override_record is None:
            return self._failure(
                project,
                snapshot=snapshot,
                decision=decision or "INDETERMINATE_REVIEW",
                status=AuditStatus.FAIL,
                error_class="PERMANENT_OVERRIDE_NOT_AUTHORIZED",
                returncode=2,
                judge_completed=judge_completed,
            )
        try:
            with delivery_side_effect_commit_lease(
                project,
                operation="acceptance",
                workflow_id=workflow_id,
                run_generation=run_generation,
            ):
                from ..artifacts import atomic_write_text

                judge_outputs = project / "judge_outputs"
                judge_outputs.mkdir(parents=True, exist_ok=True)
                atomic_write_text(
                    judge_outputs / "final_submission.sha256",
                    snapshot.snapshot_id + "\n",
                    encoding="ascii",
                )
                override_receipt = None
                if override_record is not None:
                    override_receipt = self._write_override_receipt(
                        project, snapshot, override_record, decision
                    )
                final_receipt = build_final_acceptance_receipt(
                    project,
                    snapshot,
                    status=status.value,
                    override_receipt=override_receipt,
                    workflow_id=workflow_id,
                    run_generation=run_generation,
                )
                if override_record is not None and not self.override_provider.consume(
                    override_record.override_id
                ):
                    return self._failure(
                        project,
                        snapshot=snapshot,
                        decision=decision,
                        status=AuditStatus.INDETERMINATE,
                        error_class="PERMANENT_OVERRIDE_CONSUMPTION",
                        returncode=2,
                        judge_completed=judge_completed,
                        evidence={"override_id": override_record.override_id},
                    )
                record = AuditRecord(
                    snapshot_id=snapshot.snapshot_id,
                    base=project.name,
                    profile=self.profile,
                    status=status,
                    decision=decision,
                    judge_completed=judge_completed,
                    delivery_allowed=True,
                    created_at=_utc_now(),
                    override=override,
                    evidence={
                        "judge": judge_result.metadata,
                        "final_acceptance_receipt": str(
                            FINAL_ACCEPTANCE_RECEIPT_PATH
                        ),
                        "final_acceptance_content_sha256": final_receipt.get(
                            "content_sha256"
                        ),
                        "override_id": (
                            override_record.override_id
                            if override_record is not None
                            else None
                        ),
                    },
                )
                record = self._persist(project, snapshot, record)
        except Phase9DeliveryFenceError as exc:
            return self._failure(
                project,
                snapshot=snapshot,
                decision=decision,
                status=AuditStatus.INDETERMINATE,
                error_class="PERMANENT_PHASE9_DELIVERY_DISABLED",
                returncode=2,
                judge_completed=judge_completed,
                evidence={"delivery_fence_error": str(exc)},
            )
        except (OSError, ValueError) as exc:
            return self._failure(
                project,
                snapshot=snapshot,
                decision=decision,
                status=AuditStatus.INDETERMINATE,
                error_class="PERMANENT_FINAL_ACCEPTANCE_RECEIPT",
                returncode=2,
                judge_completed=judge_completed,
                evidence={"receipt_error": str(exc)},
            )
        execution = ExecutionResult.succeeded(
            audit_status=record.status.value,
            audit_snapshot=snapshot.snapshot_id,
            audit_result=str(self._latest_path(project).relative_to(project)),
            final_decision=decision,
            gate2_delivery_override=override,
        )
        return AuditOutcome(execution, record, snapshot)

    def _finish_ablation(
        self,
        project: Path,
        snapshot: AuditSnapshot,
        *,
        analysis_only: bool,
    ) -> AuditOutcome:
        marker = project / "judge_outputs" / "final_submission.ablation.json"
        if not analysis_only:
            _atomic_write_json(
                marker,
                {
                    "schema_version": "final-submission-ablation-v1",
                    "ablation": "ABLATE_NO_JUDGE",
                    "judge_executed": False,
                    "quality_pass_fabricated": False,
                    "snapshot_id": snapshot.snapshot_id,
                    "technical_flow_validation": self.technical_flow_validation,
                    "delivery_allowed": False,
                    "terminal_reason": "PERMANENT_ABLATION_NO_DELIVERY",
                    "returncode": 2,
                },
            )
        evidence: dict[str, object] = {
            "governance": "ABLATE_NO_JUDGE",
            "technical_flow_validation": self.technical_flow_validation,
            "quality_pass_fabricated": False,
            "delivery_allowed": False,
            "analysis_only": analysis_only,
        }
        if not analysis_only:
            evidence["ablation_marker"] = str(marker.relative_to(project))
        record = AuditRecord(
            snapshot_id=snapshot.snapshot_id,
            base=project.name,
            profile=self.profile,
            status=AuditStatus.OVERRIDDEN,
            decision="ABLATE_NO_JUDGE",
            judge_completed=False,
            delivery_allowed=False,
            created_at=_utc_now(),
            error_class="PERMANENT_ABLATION_NO_DELIVERY",
            returncode=2,
            evidence=evidence,
        )
        record = self._persist(project, snapshot, record)
        return AuditOutcome(
            ExecutionResult.failed(
                "PERMANENT_ABLATION_NO_DELIVERY",
                returncode=2,
                audit_status=record.status.value,
                audit_snapshot=snapshot.snapshot_id,
                audit_result=str(
                    (
                        self._analysis_latest_path(project)
                        if analysis_only
                        else self._latest_path(project)
                    ).relative_to(project)
                ),
                final_decision=record.decision,
                gate2_delivery_override=False,
                ablation="ABLATE_NO_JUDGE",
                technical_flow_validation=self.technical_flow_validation,
                quality_pass_fabricated=False,
                delivery_allowed=False,
            ),
            record,
            snapshot,
        )

    def _run_acceptance_checks(
        self, project: Path
    ) -> tuple[list[dict[str, object]], ExecutionResult | None]:
        checks = IncrementalAuditService(
            self.factory_root, runner=self.runner
        ).paper_checks(project)
        provenance_report = project / "provenance_verification.latest.txt"
        provenance = self.runner.python(
            self.factory_root,
            project,
            "scripts/verify_provenance.py",
            [project],
            label="audit_final_provenance",
            timeout_seconds=600,
            accepted=(0,),
            log_path=provenance_report,
        )
        checks.append(
            StageCheck(
                "provenance",
                provenance.accepted,
                "hard",
                "PASS" if provenance.accepted else f"exit={provenance.returncode}",
                str(provenance_report.relative_to(project)),
                provenance.returncode,
                provenance.timed_out,
            )
        )

        evidence: list[dict[str, object]] = []
        for check in checks:
            value = check.to_dict()
            report = value.get("report")
            if isinstance(report, str) and report:
                path = project / report
                value["report_sha256"] = (
                    self._sha256_file(path) if path.is_file() else None
                )
            evidence.append(value)
        _atomic_write_json(
            project / "judge_outputs/final_paper_checks.json",
            {
                "schema_version": "final-paper-checks-v1",
                "base": project.name,
                "checks": evidence,
                "hard_failures": [
                    check.name
                    for check in checks
                    if check.severity == "hard" and not check.passed
                ],
                "warnings": [
                    check.name
                    for check in checks
                    if check.severity == "warning" and not check.passed
                ],
            },
        )
        failed = next(
            (
                check
                for check in checks
                if check.severity == "hard" and not check.passed
            ),
            None,
        )
        if failed is None:
            return evidence, None
        return evidence, ExecutionResult.failed(
            "PERMANENT_DELIVERY_ACCEPTANCE",
            returncode=failed.returncode or 1,
            check=failed.name,
        )

    def _run_visual_gate(self, project: Path, pdf: Path) -> ExecutionResult | None:
        args: list[str | Path] = [
            pdf,
            "--output",
            project / "judge_outputs/visual_gate.json",
        ]
        tex_log = project / f"{project.name}_paper.log"
        if tex_log.is_file():
            args.extend(["--tex-log", tex_log])
        max_pages = self._configured_max_pages(project)
        if max_pages is not None:
            args.extend(["--max-pages", str(max_pages)])
        result = self.runner.python(
            self.factory_root,
            project,
            "scripts/pdf_visual_gate.py",
            args,
            label="pdf_visual_gate",
            timeout_seconds=600,
            accepted=(0, 1, 2),
        )
        if not result.accepted:
            return ExecutionResult.failed(
                "TRANSIENT_VISUAL_GATE", returncode=result.returncode
            )
        return None

    @staticmethod
    def _configured_max_pages(project: Path) -> int | None:
        configured = os.getenv("FINAL_AUDIT_MAX_PAGES", "").strip()
        if configured:
            try:
                value = int(configured)
            except ValueError as exc:
                raise ValueError("FINAL_AUDIT_MAX_PAGES must be a positive integer") from exc
            if value <= 0:
                raise ValueError("FINAL_AUDIT_MAX_PAGES must be a positive integer")
            return value
        path = project / "problem/deliverables.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        paper = payload.get("paper") if isinstance(payload, dict) else None
        value = (
            paper.get("max_pages")
            if isinstance(paper, dict)
            else payload.get("max_pages")
            if isinstance(payload, dict)
            else None
        )
        return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None

    def _run_decision_router(self, project: Path) -> ExecutionResult | None:
        result = self.runner.python(
            self.factory_root,
            project,
            "scripts/judge_decision_router.py",
            [
                "--aggregate",
                project / "judge_outputs/aggregate.json",
                "--visual-gate",
                project / "judge_outputs/visual_gate.json",
                "--policy-mode",
                "enforce",
                "--output",
                project / "judge_outputs/decision_route.json",
            ],
            label="judge_route",
            timeout_seconds=120,
        )
        if not result.accepted:
            return ExecutionResult.failed(
                "TRANSIENT_JUDGE_ROUTING", returncode=result.returncode
            )
        return None

    def _build_and_verify_receipt(
        self, project: Path, snapshot_id: str
    ) -> ExecutionResult | None:
        for command, args, label in (
            (
                "scripts/judgment_receipt.py",
                [
                    "build",
                    project,
                    "--base",
                    project.name,
                    "--input-fingerprint",
                    snapshot_id,
                ],
                "receipt_build",
            ),
            (
                "scripts/judgment_receipt.py",
                [
                    "verify",
                    project,
                    "--base",
                    project.name,
                    "--input-fingerprint",
                    snapshot_id,
                    "--require-pass",
                ],
                "receipt_verify",
            ),
        ):
            result = self.runner.python(
                self.factory_root,
                project,
                command,
                args,
                label=label,
                timeout_seconds=180,
            )
            if not result.accepted:
                return ExecutionResult.failed(
                    "PERMANENT_JUDGMENT_RECEIPT", returncode=result.returncode
                )
        return None

    def _snapshot(self, project: Path) -> AuditSnapshot:
        if self.fingerprinter is None:
            from scripts.submission_fingerprint import (
                submission_fingerprint,
                submission_fingerprint_payload,
            )

            identity = submission_fingerprint_payload(
                project, project.name, policy_mode="enforce"
            )
            snapshot_id = submission_fingerprint(
                project, project.name, policy_mode="enforce"
            )
        else:
            snapshot_id = self.fingerprinter(project, project.name)
            identity = {
                "base": project.name,
                "fingerprint": snapshot_id,
                "source": "injected_fingerprinter",
            }
        if (
            len(snapshot_id) != 64
            or any(character not in "0123456789abcdef" for character in snapshot_id)
        ):
            raise ValueError("audit snapshot fingerprint must be lowercase SHA-256")
        return AuditSnapshot(
            snapshot_id=snapshot_id,
            base=project.name,
            profile=self.profile,
            created_at=_utc_now(),
            identity=identity,
        )

    def _failure(
        self,
        project: Path,
        *,
        decision: str,
        status: AuditStatus,
        error_class: str,
        returncode: int,
        snapshot: AuditSnapshot | None = None,
        resume_after_step: int | None = None,
        judge_completed: bool = False,
        evidence: dict[str, object] | None = None,
    ) -> AuditOutcome:
        failure_evidence = dict(evidence or {})
        if snapshot is None:
            try:
                snapshot = self._snapshot(project)
            except (OSError, ValueError) as exc:
                identity = {
                    "source": "audit_failure_before_snapshot",
                    "base": project.name,
                    "decision": decision,
                    "error_class": error_class,
                }
                snapshot = AuditSnapshot(
                    snapshot_id=hashlib.sha256(
                        json.dumps(
                            identity,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    ).hexdigest(),
                    base=project.name,
                    profile=self.profile,
                    created_at=_utc_now(),
                    identity=identity,
                )
                failure_evidence["snapshot_error"] = str(exc)
        record = AuditRecord(
            snapshot_id=snapshot.snapshot_id,
            base=project.name,
            profile=self.profile,
            status=status,
            decision=decision,
            judge_completed=judge_completed,
            delivery_allowed=False,
            created_at=_utc_now(),
            error_class=error_class,
            returncode=returncode,
            resume_after_step=resume_after_step,
            evidence=failure_evidence,
        )
        record = self._persist(project, snapshot, record)
        metadata = {
            "audit_status": status.value,
            "audit_snapshot": snapshot.snapshot_id,
            "audit_result": str(self._latest_path(project).relative_to(project)),
            "final_decision": decision,
        }
        if resume_after_step is not None:
            metadata["resume_after_step"] = resume_after_step
            execution = ExecutionResult.succeeded(**metadata)
        else:
            execution = ExecutionResult.failed(
                error_class, returncode=returncode or 1, **metadata
            )
        return AuditOutcome(execution, record, snapshot)

    def _busy(self, project: Path) -> AuditOutcome:
        snapshot_id = hashlib.sha256(
            f"audit-busy:{project}".encode("utf-8")
        ).hexdigest()
        snapshot = AuditSnapshot(
            snapshot_id=snapshot_id,
            base=project.name,
            profile=self.profile,
            created_at=_utc_now(),
            identity={"state": "AUDIT_BUSY", "project": str(project)},
        )
        record = AuditRecord(
            snapshot_id=snapshot_id,
            base=project.name,
            profile=self.profile,
            status=AuditStatus.INDETERMINATE,
            decision="AUDIT_BUSY",
            judge_completed=False,
            delivery_allowed=False,
            created_at=_utc_now(),
            error_class="TRANSIENT_AUDIT_BUSY",
            returncode=75,
        )
        return AuditOutcome(
            ExecutionResult.failed(
                "TRANSIENT_AUDIT_BUSY",
                returncode=75,
                audit_status=AuditStatus.INDETERMINATE.value,
                final_decision="AUDIT_BUSY",
            ),
            record,
            snapshot,
        )

    def _persist(
        self, project: Path, snapshot: AuditSnapshot, record: AuditRecord
    ) -> AuditRecord:
        audit_dir = project / ".factory" / "audits" / snapshot.snapshot_id
        snapshot_path = audit_dir / "snapshot.json"
        if snapshot_path.is_file():
            existing = json.loads(snapshot_path.read_text(encoding="utf-8"))
            comparable = snapshot.to_dict()
            comparable["created_at"] = existing.get(
                "created_at", comparable["created_at"]
            )
            if existing != comparable:
                raise ValueError(
                    f"audit snapshot collision for {snapshot.snapshot_id}"
                )
        else:
            _atomic_write_json(snapshot_path, snapshot.to_dict())
        attempt = (
            audit_dir
            / "attempts"
            / f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%S.%fZ')}.json"
        )
        payload = record.to_dict()
        _atomic_write_json(attempt, payload)
        if not record.delivery_allowed:
            _atomic_write_json(audit_dir / "analysis_latest.json", payload)
            _atomic_write_json(
                project / ".factory" / "audits" / "analysis_latest.json",
                payload,
            )
        snapshot_latest = audit_dir / "latest.json"
        global_latest = self._latest_path(project)
        if not self._accepted_latest_for_snapshot(
            project, snapshot_latest, snapshot.snapshot_id
        ):
            _atomic_write_json(snapshot_latest, payload)
        if not self._accepted_latest_for_snapshot(
            project, global_latest, snapshot.snapshot_id
        ):
            _atomic_write_json(global_latest, payload)
        return record

    @staticmethod
    def _accepted_latest_for_snapshot(
        project: Path, path: Path, snapshot_id: str
    ) -> bool:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            status = str(value["status"])
        except (OSError, KeyError, ValueError, json.JSONDecodeError):
            return False
        if (
            value.get("profile") != AuditProfile.FINAL.value
            or value.get("snapshot_id") != snapshot_id
            or value.get("delivery_allowed") is not True
            or status not in {AuditStatus.PASS.value, AuditStatus.OVERRIDDEN.value}
        ):
            return False
        valid, _errors = verify_final_acceptance_receipt(
            project,
            expected_snapshot_id=snapshot_id,
            expected_status=status,
        )
        return valid

    def _load_reusable(
        self,
        project: Path,
        snapshot: AuditSnapshot,
        *,
        analysis_only: bool,
    ) -> AuditRecord | None:
        audit_dir = project / ".factory" / "audits" / snapshot.snapshot_id
        candidates = (
            (audit_dir / "analysis_latest.json", audit_dir / "latest.json")
            if analysis_only
            else (audit_dir / "latest.json",)
        )
        value: dict[str, object] | None = None
        status: AuditStatus | None = None
        for path in candidates:
            try:
                candidate = json.loads(path.read_text(encoding="utf-8"))
                candidate_status = AuditStatus(str(candidate["status"]))
            except (OSError, KeyError, ValueError, json.JSONDecodeError):
                continue
            value = candidate
            status = candidate_status
            break
        if value is None or status is None:
            return None
        if value.get("snapshot_id") != snapshot.snapshot_id or status not in {
            AuditStatus.PASS,
            AuditStatus.OVERRIDDEN,
        } or value.get("profile") != self.profile:
            return None
        if value.get("decision") == "ABLATE_NO_JUDGE":
            return None
        expected_delivery_allowed = not analysis_only
        if value.get("delivery_allowed") is not expected_delivery_allowed:
            return None
        if analysis_only and status is not AuditStatus.PASS:
            return None
        if status is AuditStatus.PASS and (
            value.get("decision") != "PASS"
            or value.get("judge_completed") is not True
        ):
            return None
        override = bool(value.get("override"))
        if analysis_only and override:
            return None
        if not analysis_only:
            try:
                final_hash = (
                    project / "judge_outputs/final_submission.sha256"
                ).read_text(encoding="ascii").strip()
            except OSError:
                return None
            if final_hash != snapshot.snapshot_id:
                return None
        if status is AuditStatus.PASS:
            from scripts.judgment_receipt import verify_receipt

            valid, _errors = verify_receipt(
                project,
                project.name,
                expected_input_fingerprint=snapshot.snapshot_id,
                require_pass=True,
            )
            if not valid:
                return None
            if not analysis_only:
                acceptance_valid, _acceptance_errors = (
                    verify_final_acceptance_receipt(
                        project,
                        snapshot,
                        expected_snapshot_id=snapshot.snapshot_id,
                        expected_status=AuditStatus.PASS.value,
                    )
                )
                if not acceptance_valid:
                    return None
        else:
            if analysis_only:
                return None
            if not override or not self._consumed_delivery_override(
                project, snapshot.snapshot_id, value
            ):
                return None
            try:
                route = json.loads(
                    (project / "judge_outputs/decision_route.json").read_text(
                        encoding="utf-8"
                    )
                )
            except (OSError, json.JSONDecodeError):
                return None
            if (
                route.get("effective_decision") != "CONTINUE_TO_STEP16"
                or route.get("quality_pass_fabricated") is not False
            ):
                return None
            acceptance_valid, _acceptance_errors = verify_final_acceptance_receipt(
                project,
                snapshot,
                expected_snapshot_id=snapshot.snapshot_id,
                expected_status=AuditStatus.OVERRIDDEN.value,
            )
            if not acceptance_valid:
                return None
        return AuditRecord(
            snapshot_id=snapshot.snapshot_id,
            base=project.name,
            profile=self.profile,
            status=status,
            decision=str(value.get("decision") or ""),
            judge_completed=bool(value.get("judge_completed")),
            delivery_allowed=not analysis_only,
            created_at=str(value.get("created_at") or _utc_now()),
            error_class=str(value.get("error_class") or ""),
            returncode=int(value.get("returncode") or 0),
            resume_after_step=value.get("resume_after_step"),
            override=override,
            reused=True,
            evidence=dict(value.get("evidence") or {}),
        )

    def _consumed_delivery_override(
        self,
        project: Path,
        snapshot_id: str,
        audit_record: dict[str, object],
    ) -> bool:
        evidence = audit_record.get("evidence")
        override_id = (
            evidence.get("override_id") if isinstance(evidence, dict) else None
        )
        getter = getattr(self.override_provider, "get_override", None)
        if not isinstance(override_id, str) or not callable(getter):
            return False
        record = getter(override_id)
        return bool(
            record is not None
            and record.base_name == project.name
            and record.scope == DELIVER_SNAPSHOT
            and record.bound_snapshot_id == snapshot_id
            and record.revoked_at is None
            and record.consumed_at is not None
        )

    def _judge_failure_override(
        self, project: Path, failure: ExecutionResult
    ) -> ExecutionResult | None:
        continuation = getattr(self.judge, "_continue_after_failure", None)
        if not callable(continuation):
            return None
        return continuation(project, "packet", failure)

    @staticmethod
    def _latest_path(project: Path) -> Path:
        return project / ".factory" / "audits" / "latest.json"

    @staticmethod
    def _analysis_latest_path(project: Path) -> Path:
        return project / ".factory" / "audits" / "analysis_latest.json"

    @staticmethod
    def _has_stub(project: Path) -> bool:
        models = project / "models"
        return models.is_dir() and any(models.rglob("*.stub"))

    @staticmethod
    def _unresolved_blocking(project: Path) -> bool:
        return has_unresolved_blocking(project / "audit_issue_ledger.md")

    def _delivery_override(
        self, project: Path, snapshot_id: str
    ) -> DeliveryOverride | None:
        return self.override_provider.active_override(
            project.name,
            DELIVER_SNAPSHOT,
            snapshot_id=snapshot_id,
        )

    def _delivery_override_active(
        self, project: Path, snapshot_id: str | None = None
    ) -> bool:
        if snapshot_id is None:
            try:
                snapshot_id = (
                    project / "judge_outputs/final_submission.sha256"
                ).read_text(encoding="ascii").strip()
            except OSError:
                return False
        return self._delivery_override(project, snapshot_id) is not None

    @staticmethod
    def _sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _write_override_receipt(
        project: Path,
        snapshot: AuditSnapshot,
        override: DeliveryOverride,
        decision: str,
    ) -> str:
        relative = "judge_outputs/delivery_override_receipt.json"
        _atomic_write_json(
            project / relative,
            {
                "schema_version": "delivery-override-receipt-v1",
                "snapshot_id": snapshot.snapshot_id,
                "base": project.name,
                "scope": DELIVER_SNAPSHOT,
                "source_verdict": decision or override.source_verdict,
                "quality_pass_fabricated": False,
                "authorization": override.to_dict(),
            },
        )
        return relative

    @staticmethod
    def _decision(project: Path, *, prefer_new: bool = False) -> str:
        try:
            value = json.loads(
                (project / "judge_outputs/decision_route.json").read_text(
                    encoding="utf-8"
                )
            )
            key = "new_decision" if prefer_new else "effective_decision"
            return str(value.get(key, ""))
        except (OSError, json.JSONDecodeError, AttributeError):
            return ""

    @staticmethod
    def _record_override_decision(
        project: Path, decision: str, judge_metadata: dict[str, object]
    ) -> None:
        output = project / "judge_outputs" / "decision_route.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(
            output,
            {
                "schema_version": "judge-decision-route-v1",
                "policy_mode": "delivery_override",
                "new_decision": decision,
                "effective_decision": "CONTINUE_TO_STEP16",
                "quality_pass_fabricated": False,
                "judge_completed": judge_metadata.get("judge_completed", True),
                "judge_failure_stage": judge_metadata.get("judge_failure_stage"),
                "judge_error_class": judge_metadata.get("judge_error_class"),
            },
        )


def build_final_audit_service(
    factory_root: str | Path,
    *,
    dispatcher: ModelDispatcher | None = None,
    renderer: PromptRenderer | None = None,
    runner: CommandRunner | None = None,
    validator: NativeArtifactValidator | None = None,
    fingerprinter: Fingerprinter | None = None,
    override_provider: OverrideProvider | None = None,
) -> FinalAuditService:
    root = Path(factory_root).resolve()
    renderer = renderer or PromptRenderer(root)
    dispatcher = dispatcher or ModelDispatcher(root, build_model_backends(root))
    runner = runner or CommandRunner()
    validator = validator or validator_for(root, 13)
    from ..steps.specialized import JudgeStep

    judge = JudgeStep(
        contract_for(13),
        root,
        renderer,
        dispatcher,
        validator,
        runner,
        override_provider,
    )
    return FinalAuditService(
        root,
        judge,
        validator,
        runner,
        fingerprinter,
        override_provider,
    )
