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
from ..steps.catalog import contract_for
from ..steps.prompting import PromptRenderer
from ..steps.validators import NativeArtifactValidator, validator_for
from .domain import AuditOutcome, AuditProfile, AuditRecord, AuditSnapshot, AuditStatus
from .ledger import has_unresolved_blocking
from .persistence import atomic_write_json as _atomic_write_json
from .persistence import utc_now as _utc_now


class JudgeExecutor(Protocol):
    def execute(self, context: StepContext) -> ExecutionResult: ...


Fingerprinter = Callable[[Path, str], str]


class FinalAuditService:
    """Run final acceptance and judge checks without publishing the project.

    The service may write generated verification reports, compiled PDF output,
    compatibility judge artifacts, and its own ``.factory/audits`` records. It
    never copies into ``papers/``, packages a submission, archives a project,
    or changes workflow state.
    """

    profile = AuditProfile.FINAL.value

    def __init__(
        self,
        factory_root: Path,
        judge: JudgeExecutor,
        validator: NativeArtifactValidator,
        runner: CommandRunner,
        fingerprinter: Fingerprinter | None = None,
    ) -> None:
        self.factory_root = factory_root.resolve()
        self.judge = judge
        self.validator = validator
        self.runner = runner
        self.fingerprinter = fingerprinter

    def run(
        self,
        context: StepContext,
        *,
        compile_pdf: bool = True,
        reuse_pass: bool = True,
    ) -> AuditOutcome:
        project = context.project_dir.resolve()
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
                )
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _run_unlocked(
        self,
        context: StepContext,
        *,
        compile_pdf: bool,
        reuse_pass: bool,
    ) -> AuditOutcome:
        project = context.project_dir.resolve()
        base = project.name

        if self._has_stub(project) or self._unresolved_blocking(project):
            return self._failure(
                project,
                decision="CONTENT_NOT_READY",
                status=AuditStatus.FAIL,
                error_class="PERMANENT_DELIVERY_ACCEPTANCE",
                returncode=2,
            )

        acceptance = self._run_acceptance_checks(project)
        if acceptance is not None:
            return self._failure(
                project,
                decision="CONTENT_NOT_READY",
                status=AuditStatus.FAIL,
                error_class="PERMANENT_DELIVERY_ACCEPTANCE",
                returncode=acceptance.returncode,
                evidence={"failed_check": acceptance.metadata.get("check")},
            )

        pdf = project / f"{base}_paper.pdf"
        packets_prepared = False
        ablate_judge = os.getenv("ABLATE_NO_JUDGE", "0").lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
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
                return self._finish_judge_result(project, context, continued)
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
            and not self._delivery_override_active(project)
            and pdf.is_file()
            and pdf.stat().st_size > 0
        ):
            packet_failure = prepare_for_snapshot()
            if packet_failure is not None:
                return packet_failure
            candidate = self._snapshot(project)
            cached = self._load_reusable(project, candidate)
            if cached is not None:
                execution = ExecutionResult.succeeded(
                    audit_status=cached.status.value,
                    audit_snapshot=candidate.snapshot_id,
                    audit_reused=True,
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
            cached = self._load_reusable(project, snapshot)
            if cached is not None:
                execution = ExecutionResult.succeeded(
                    audit_status=cached.status.value,
                    audit_snapshot=snapshot.snapshot_id,
                    audit_reused=True,
                    final_decision=cached.decision,
                    gate2_delivery_override=cached.override,
                )
                return AuditOutcome(execution, cached, snapshot)

        if ablate_judge:
            return self._finish_ablation(
                project,
                self.judge.execute(context),
                snapshot,
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
        )

    def run_project(
        self,
        project: Path,
        *,
        compile_pdf: bool = True,
        reuse_pass: bool = True,
    ) -> AuditOutcome:
        context = StepContext(
            project.resolve(),
            project.name,
            16,
            1,
            contract_for(16).timeout_seconds,
            0,
        )
        return self.run(context, compile_pdf=compile_pdf, reuse_pass=reuse_pass)

    def _finish_judge_result(
        self,
        project: Path,
        context: StepContext,
        judge_result: ExecutionResult,
        *,
        snapshot: AuditSnapshot | None = None,
    ) -> AuditOutcome:
        snapshot = snapshot or self._snapshot(project)
        if judge_result.returncode != 0:
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
        override = self._delivery_override_active(project)
        judge_completed = judge_result.metadata.get("judge_completed") is not False
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

        if override and (decision != "PASS" or not judge_completed):
            self._record_override_decision(project, decision, judge_result.metadata)

        snapshot = self._snapshot(project)
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

        (project / "judge_outputs").mkdir(parents=True, exist_ok=True)
        (project / "judge_outputs/final_submission.sha256").write_text(
            snapshot.snapshot_id + "\n", encoding="ascii"
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
            delivery_allowed=True,
            created_at=_utc_now(),
            override=override,
            evidence={"judge": judge_result.metadata},
        )
        record = self._persist(project, snapshot, record)
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
        judge_result: ExecutionResult,
        snapshot: AuditSnapshot,
    ) -> AuditOutcome:
        if judge_result.returncode != 0:
            return self._failure(
                project,
                snapshot=snapshot,
                decision="ABLATE_NO_JUDGE_FAILED",
                status=AuditStatus.INDETERMINATE,
                error_class=judge_result.error_class or "TRANSIENT_JUDGE_INFRASTRUCTURE",
                returncode=judge_result.returncode,
                evidence=judge_result.metadata,
            )
        marker = project / "judge_outputs" / "final_submission.ablation.json"
        _atomic_write_json(
            marker,
            {
                "schema_version": "final-submission-ablation-v1",
                "ablation": "ABLATE_NO_JUDGE",
                "judge_executed": False,
                "quality_pass_fabricated": False,
                "snapshot_id": snapshot.snapshot_id,
            },
        )
        (project / "judge_outputs/final_submission.sha256").write_text(
            snapshot.snapshot_id + "\n", encoding="ascii"
        )
        record = AuditRecord(
            snapshot_id=snapshot.snapshot_id,
            base=project.name,
            profile=self.profile,
            status=AuditStatus.OVERRIDDEN,
            decision="ABLATE_NO_JUDGE",
            judge_completed=False,
            delivery_allowed=True,
            created_at=_utc_now(),
            evidence={"governance": "ABLATE_NO_JUDGE"},
        )
        record = self._persist(project, snapshot, record)
        return AuditOutcome(
            ExecutionResult.succeeded(
                audit_status=record.status.value,
                audit_snapshot=snapshot.snapshot_id,
                audit_result=str(self._latest_path(project).relative_to(project)),
                final_decision=record.decision,
                gate2_delivery_override=False,
                ablation="ABLATE_NO_JUDGE",
            ),
            record,
            snapshot,
        )

    def _run_acceptance_checks(self, project: Path) -> ExecutionResult | None:
        checks = (
            (
                "scripts/verify_provenance.py",
                [project],
                "provenance_verification",
                project / "provenance_verification.latest.txt",
            ),
            (
                "scripts/verify_quality_contract.py",
                [
                    project,
                    "--factory-root",
                    self.factory_root,
                    "--json-out",
                    project / "quality_contract_verification.latest.json",
                    "--text-out",
                    project / "quality_contract_verification.latest.txt",
                ],
                "quality_contract_verification",
                project / "logs/native_quality_contract.log",
            ),
        )
        for script, args, label, log_path in checks:
            result = self.runner.python(
                self.factory_root,
                project,
                script,
                args,
                label=label,
                timeout_seconds=600,
                accepted=(0,),
                log_path=log_path,
            )
            if not result.accepted:
                return ExecutionResult.failed(
                    "PERMANENT_DELIVERY_ACCEPTANCE",
                    returncode=result.returncode,
                    check=label,
                )
        return None

    def _run_visual_gate(self, project: Path, pdf: Path) -> ExecutionResult | None:
        args: list[str | Path] = [
            pdf,
            "--output",
            project / "judge_outputs/visual_gate.json",
        ]
        tex_log = project / f"{project.name}_paper.log"
        if tex_log.is_file():
            args.extend(["--tex-log", tex_log])
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
                os.getenv("JUDGE_POLICY_MODE", "shadow").lower(),
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

            identity = submission_fingerprint_payload(project, project.name)
            snapshot_id = submission_fingerprint(project, project.name)
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
        snapshot = snapshot or self._snapshot(project)
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
            evidence=dict(evidence or {}),
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
        _atomic_write_json(attempt, record.to_dict())
        _atomic_write_json(audit_dir / "latest.json", record.to_dict())
        _atomic_write_json(self._latest_path(project), record.to_dict())
        return record

    def _load_reusable(
        self, project: Path, snapshot: AuditSnapshot
    ) -> AuditRecord | None:
        path = project / ".factory" / "audits" / snapshot.snapshot_id / "latest.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            status = AuditStatus(str(value["status"]))
        except (OSError, KeyError, ValueError, json.JSONDecodeError):
            return None
        if value.get("snapshot_id") != snapshot.snapshot_id or status not in {
            AuditStatus.PASS,
            AuditStatus.OVERRIDDEN,
        } or value.get("profile") != self.profile:
            return None
        if value.get("delivery_allowed") is not True:
            return None
        if status is AuditStatus.PASS and (
            value.get("decision") != "PASS"
            or value.get("judge_completed") is not True
        ):
            return None
        try:
            final_hash = (project / "judge_outputs/final_submission.sha256").read_text(
                encoding="ascii"
            ).strip()
        except OSError:
            return None
        if final_hash != snapshot.snapshot_id:
            return None
        override = bool(value.get("override"))
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
        else:
            ablation = value.get("decision") == "ABLATE_NO_JUDGE"
            if ablation:
                try:
                    marker = json.loads(
                        (
                            project
                            / "judge_outputs/final_submission.ablation.json"
                        ).read_text(encoding="utf-8")
                    )
                except (OSError, json.JSONDecodeError):
                    return None
                if (
                    os.getenv("ABLATE_NO_JUDGE", "0").lower()
                    not in {"1", "true", "yes", "on"}
                    or marker.get("judge_executed") is not False
                    or marker.get("snapshot_id") != snapshot.snapshot_id
                ):
                    return None
            elif not override or not self._delivery_override_active(project):
                return None
            if ablation:
                route = None
            else:
                try:
                    route = json.loads(
                        (project / "judge_outputs/decision_route.json").read_text(
                            encoding="utf-8"
                        )
                    )
                except (OSError, json.JSONDecodeError):
                    return None
            if route is not None and (
                route.get("effective_decision") != "CONTINUE_TO_STEP16"
                or route.get("quality_pass_fabricated") is not False
            ):
                return None
        return AuditRecord(
            snapshot_id=snapshot.snapshot_id,
            base=project.name,
            profile=self.profile,
            status=status,
            decision=str(value.get("decision") or ""),
            judge_completed=bool(value.get("judge_completed")),
            delivery_allowed=True,
            created_at=str(value.get("created_at") or _utc_now()),
            error_class=str(value.get("error_class") or ""),
            returncode=int(value.get("returncode") or 0),
            resume_after_step=value.get("resume_after_step"),
            override=override,
            reused=True,
            evidence=dict(value.get("evidence") or {}),
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
    def _has_stub(project: Path) -> bool:
        models = project / "models"
        return models.is_dir() and any(models.rglob("*.stub"))

    @staticmethod
    def _unresolved_blocking(project: Path) -> bool:
        return has_unresolved_blocking(project / "audit_issue_ledger.md")

    @staticmethod
    def _delivery_override_active(project: Path) -> bool:
        from scripts.workflow_state import gate2_delivery_override

        return gate2_delivery_override(project)

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
) -> FinalAuditService:
    root = Path(factory_root).resolve()
    renderer = renderer or PromptRenderer(root)
    dispatcher = dispatcher or ModelDispatcher(root, build_model_backends(root))
    runner = runner or CommandRunner()
    validator = validator or validator_for(root, 13)
    from ..steps.specialized import JudgeStep

    judge = JudgeStep(
        contract_for(13), root, renderer, dispatcher, validator, runner
    )
    return FinalAuditService(root, judge, validator, runner, fingerprinter)
