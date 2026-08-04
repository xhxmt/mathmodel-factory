from __future__ import annotations

import json
import os
import re
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..adapters.infrastructure.commands import CommandRunner
from ..adapters.models.backends import ModelRequest
from ..adapters.models.dispatcher import ModelDispatcher
from ..domain import (
    ExecutionResult,
    PrepareResult,
    RecoveryDecision,
    StepError,
)
from .catalog import StepContract
from .gates import prepare_human_gates
from .prompt_step import PromptStep
from .prompting import PromptRenderer
from .validators import NativeArtifactValidator


_VERDICT_RE = re.compile(r"^VERDICT:\s*(\S+)", re.MULTILINE)
_STREAM_RE = re.compile(r"^## Stream m(\d+)[：:]", re.MULTILINE)


def _verdict(path: Path) -> str:
    if not path.is_file():
        return ""
    match = _VERDICT_RE.search(path.read_text(encoding="utf-8", errors="replace"))
    return match.group(1).upper() if match else ""


def _recover(validator: NativeArtifactValidator, context) -> RecoveryDecision:
    return RecoveryDecision.from_validation(
        validator.validate(context), active_step=context.step_id
    )


@dataclass
class ParallelProposalStep:
    contract: StepContract
    renderer: PromptRenderer
    dispatcher: ModelDispatcher
    validator: NativeArtifactValidator
    max_rounds: int = 4

    def prepare(self, context) -> PrepareResult:
        return PrepareResult.prepared("viable_streams.md")

    def execute(self, context) -> ExecutionResult:
        streams = self._stream_ids(context.project_dir)
        if len(streams) < 2:
            return ExecutionResult.failed(
                "TRANSIENT_INSUFFICIENT_STREAMS",
                reason="fewer than two active streams",
                streams=streams,
            )
        with ThreadPoolExecutor(max_workers=min(6, len(streams))) as executor:
            futures = {
                executor.submit(self._run_stream, context, stream, index == len(streams) - 1): stream
                for index, stream in enumerate(streams)
            }
            failures = []
            for future in as_completed(futures):
                stream = futures[future]
                try:
                    if not future.result():
                        failures.append(stream)
                except Exception:
                    failures.append(stream)
        failures.sort()
        if not failures:
            return ExecutionResult.succeeded(streams=streams)
        missing_artifacts = sorted(
            relative
            for stream in failures
            for relative in (
                f"m{stream}_spec.md",
                f"m{stream}_demo_result.json",
                f"m{stream}_critique.md",
            )
            if not (context.project_dir / relative).is_file()
        )
        return ExecutionResult.failed(
            "TRANSIENT_ARTIFACT_MISSING"
            if missing_artifacts
            else "TRANSIENT_PARALLEL_PROPOSALS",
            failed_streams=failures,
            missing_artifacts=missing_artifacts,
        )

    def _run_stream(self, context, stream: int, last_stream: bool) -> bool:
        project = context.project_dir
        prefix = f"m{stream}"
        if _verdict(project / f"{prefix}_critique.md") in {"VALIDATED", "ABANDONED"}:
            return True
        for round_number in range(1, self.max_rounds + 1):
            proposal = self.renderer.render(
                "step2_modeling_proposal.txt",
                project,
                step_key=f"2_proposal_{stream}",
                replacements={"__STREAM_ID__": str(stream), "__STREAM_PREFIX__": prefix},
            )
            proposal_result = self.dispatcher.execute(
                ModelRequest(
                    project_dir=project,
                    step_id=2,
                    attempt=round_number,
                    prompt=proposal,
                    timeout_seconds=min(context.timeout_seconds, 18_000),
                    hang_timeout_seconds=self.contract.hang_timeout_seconds,
                ),
                step_key=2,
                defaults=("claude", "codex") if last_stream else ("codex", "claude"),
            )
            if proposal_result.returncode != 0:
                continue
            if not (project / f"{prefix}_spec.md").is_file() or not (
                project / f"{prefix}_demo_result.json"
            ).is_file():
                continue
            critic = self.renderer.render(
                "step2_modeling_critic.txt",
                project,
                step_key=f"2_critic_{stream}",
                replacements={"__STREAM_ID__": str(stream), "__STREAM_PREFIX__": prefix},
            )
            critic_result = self.dispatcher.execute(
                ModelRequest(
                    project_dir=project,
                    step_id=2,
                    attempt=round_number,
                    prompt=critic,
                    timeout_seconds=min(context.timeout_seconds, 7_200),
                    hang_timeout_seconds=self.contract.hang_timeout_seconds,
                ),
                step_key=2,
                defaults=("codex", "claude"),
            )
            verdict = _verdict(project / f"{prefix}_critique.md")
            if critic_result.returncode == 0 and verdict in {"VALIDATED", "ABANDONED"}:
                return True
        return False

    @staticmethod
    def _stream_ids(project: Path) -> list[int]:
        path = project / "viable_streams.md"
        text = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
        return sorted({int(value) for value in _STREAM_RE.findall(text)})

    def validate(self, context):
        return self.validator.validate(context)

    def recover(self, context, error: StepError) -> RecoveryDecision:
        return _recover(self.validator, context)


@dataclass
class PrecheckedPromptStep:
    prompt_step: PromptStep
    runner: CommandRunner
    factory_root: Path

    @property
    def contract(self):
        return self.prompt_step.contract

    def prepare(self, context):
        return self.prompt_step.prepare(context)

    def execute(self, context) -> ExecutionResult:
        precheck = self.runner.python(
            self.factory_root,
            context.project_dir,
            "scripts/step6_coverage_precheck.py",
            [context.project_dir],
            label="step6_precheck",
            timeout_seconds=300,
            accepted=(0, 1),
        )
        if not precheck.accepted:
            return ExecutionResult.failed(
                "TRANSIENT_STEP6_PRECHECK", returncode=precheck.returncode
            )
        return self.prompt_step.execute(context)

    def validate(self, context):
        return self.prompt_step.validate(context)

    def recover(self, context, error):
        return self.prompt_step.recover(context, error)


@dataclass
class PaperDraftStep:
    prompt_step: PromptStep

    @property
    def contract(self):
        return self.prompt_step.contract

    def prepare(self, context):
        return self.prompt_step.prepare(context)

    def execute(self, context) -> ExecutionResult:
        gate = self.prompt_step.validator.validate(context)
        if gate.pending_action is not None and gate.pending_action.gate == "step8_5":
            prompt = self.prompt_step.renderer.render(
                "step8_5_reviewer_entry.txt",
                context.project_dir,
                step_key="8_5",
            )
            result = self.prompt_step.dispatcher.execute(
                ModelRequest(
                    project_dir=context.project_dir,
                    step_id=9,
                    attempt=context.attempt,
                    prompt=prompt,
                    timeout_seconds=min(context.timeout_seconds, 7_200),
                    hang_timeout_seconds=1_800,
                ),
                step_key="8_5",
                defaults=("claude", "codex"),
            )
            if result.returncode != 0:
                return result
            if self.prompt_step.validator.validate(context).pending_action is not None:
                return ExecutionResult.failed("TRANSIENT_STEP8_5_GATE", returncode=42)
        return self.prompt_step.execute(context)

    def validate(self, context):
        return self.prompt_step.validate(context)

    def recover(self, context, error):
        return self.prompt_step.recover(context, error)


@dataclass
class JudgeStep:
    contract: StepContract
    factory_root: Path
    renderer: PromptRenderer
    dispatcher: ModelDispatcher
    validator: NativeArtifactValidator
    runner: CommandRunner

    ROLE_PROMPTS = {
        "paper": "judges/paper_reviewer.txt",
        "math": "judges/math_auditor.txt",
        "execution": "judges/execution_auditor.txt",
    }

    def prepare(self, context):
        return prepare_human_gates(context.project_dir, context.step_id)

    def execute(self, context) -> ExecutionResult:
        project = context.project_dir
        if os.getenv("ABLATE_NO_JUDGE", "0").lower() in {"1", "true", "yes", "on"}:
            verdict = "PRECHECK_PASS" if context.step_id == 13 else "PASS"
            (project / "judge_evaluation.md").write_text(
                f"VERDICT: {verdict}\n\nAblation: automated judge disabled.\n",
                encoding="utf-8",
            )
            return ExecutionResult.succeeded(ablation="ABLATE_NO_JUDGE")
        prepared = self.prepare_packets(context)
        if prepared.returncode != 0:
            return (
                self._continue_after_failure(project, "packet", prepared)
                or prepared
            )
        if context.step_id == 13:
            return self.execute_precheck(context)
        return self.execute_prepared(context)

    def execute_precheck(self, context) -> ExecutionResult:
        """Run the in-loop math precheck; full three-role review belongs to final audit."""

        project = context.project_dir
        role_result = self._run_role_with_retry(
            context, "math", self.ROLE_PROMPTS["math"]
        )
        if role_result.returncode != 0:
            return (
                self._continue_after_failure(project, "precheck:math", role_result)
                or role_result
            )
        source_verdict = _verdict(project / "judge_outputs" / "math.md")
        if source_verdict == "PASS":
            verdict = "PRECHECK_PASS"
            self._write_precheck(project, verdict, source_verdict, role_result.metadata)
            return ExecutionResult.succeeded(
                judge_completed=False,
                precheck_completed=True,
                judge_verdict=verdict,
                reviewed_roles=["math"],
                **role_result.metadata,
            )
        if source_verdict == "FAIL":
            verdict = "REOPEN_REVISION_MODEL"
            self._write_precheck(project, verdict, source_verdict, role_result.metadata)
            return ExecutionResult.succeeded(
                resume_after_step=self.validator._gate2_resume(project, verdict),
                judge_completed=False,
                precheck_completed=True,
                judge_verdict=verdict,
                reviewed_roles=["math"],
                **role_result.metadata,
            )

        verdict = "INDETERMINATE_REVIEW"
        self._write_precheck(project, verdict, source_verdict, role_result.metadata)
        failure = ExecutionResult.failed(
            "TRANSIENT_JUDGE_INDETERMINATE",
            returncode=1,
            judge_completed=False,
            precheck_completed=True,
            judge_verdict=verdict,
            reviewed_roles=["math"],
            source_verdict=source_verdict or "MISSING",
            **role_result.metadata,
        )
        return (
            self._continue_after_failure(project, "precheck:math", failure)
            or failure
        )

    @staticmethod
    def _write_precheck(
        project: Path,
        verdict: str,
        source_verdict: str,
        metadata: dict[str, object],
    ) -> None:
        outputs = project / "judge_outputs"
        outputs.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": "judge-precheck-v1",
            "review_mode": "math_only",
            "verdict": verdict,
            "source_role": "math",
            "source_verdict": source_verdict or "MISSING",
            "model_id": metadata.get("model_id"),
            "backend": metadata.get("backend"),
            "quality_pass_fabricated": False,
            "delivery_allowed": False,
        }
        (outputs / "precheck.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        (project / "judge_evaluation.md").write_text(
            f"VERDICT: {verdict}\n\n"
            "Step 13 preliminary math-only review. Full math, execution, and paper "
            "review is owned by the final audit.\n\n"
            "```json\n"
            + json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n```\n",
            encoding="utf-8",
        )

    def prepare_packets(self, context) -> ExecutionResult:
        """Build deterministic judge inputs without invoking a judge model."""

        project = context.project_dir
        (project / "judge_packets").mkdir(parents=True, exist_ok=True)
        (project / "judge_outputs").mkdir(parents=True, exist_ok=True)
        commands = [
            (
                "scripts/build_objective_evidence.py",
                [project, project.name, "--output", project / "judge_packets/objective_evidence.json"],
                "objective_evidence",
            ),
            (
                "scripts/judge_packet.py",
                [project, "--base", project.name, "--objective-evidence", project / "judge_packets/objective_evidence.json"],
                "judge_packet",
            ),
        ]
        for script, args, label in commands:
            result = self.runner.python(
                self.factory_root,
                project,
                script,
                args,
                label=label,
                timeout_seconds=600,
            )
            if not result.accepted:
                return ExecutionResult.failed(
                    "TRANSIENT_JUDGE_PACKET", returncode=result.returncode, command=label
                )
        return ExecutionResult.succeeded(packets_prepared=True)

    def execute_prepared(self, context) -> ExecutionResult:
        """Run isolated roles against packets prepared for this content snapshot."""

        project = context.project_dir
        for role, template in self.ROLE_PROMPTS.items():
            result = self._run_role_with_retry(context, role, template)
            if result.returncode != 0:
                return self._continue_after_failure(project, f"role:{role}", result) or result
        bound = self.runner.python(
            self.factory_root,
            project,
            "scripts/judgment_receipt.py",
            ["bind-group", project],
            label="judge_bind_group",
            timeout_seconds=120,
        )
        if not bound.accepted:
            failure = ExecutionResult.failed(
                "TRANSIENT_JUDGE_PROVENANCE", returncode=bound.returncode
            )
            return self._continue_after_failure(project, "bind_group", failure) or failure
        aggregate = self.runner.python(
            self.factory_root,
            project,
            "scripts/aggregate_judges.py",
            [
                "--math", project / "judge_outputs/math.md",
                "--execution", project / "judge_outputs/execution.md",
                "--paper", project / "judge_outputs/paper.md",
                "--math-manifest", project / "judge_packets/math/manifest.json",
                "--execution-manifest", project / "judge_packets/execution/manifest.json",
                "--paper-manifest", project / "judge_packets/paper/manifest.json",
                "--output", project / "judge_evaluation.md",
                "--json", project / "judge_outputs/aggregate.json",
                "--base", project.name,
            ],
            label="judge_aggregate",
            timeout_seconds=300,
        )
        if not aggregate.accepted:
            failure = ExecutionResult.failed(
                "TRANSIENT_JUDGE_AGGREGATION", returncode=aggregate.returncode
            )
            return self._continue_after_failure(project, "aggregate", failure) or failure
        validation = self.validator.validate(context)
        if (
            not validation.is_valid
            and validation.metadata.get("normalized_verdict") == "INFRA_RETRY"
        ):
            retried_roles = self._indeterminate_roles(project)
            if retried_roles:
                for role in retried_roles:
                    result = self._run_role_with_retry(
                        context, role, self.ROLE_PROMPTS[role]
                    )
                    if result.returncode != 0:
                        return self._continue_after_failure(
                            project, f"role:{role}", result
                        ) or result
                failure = self._reaggregate(project)
                if failure is not None:
                    return self._continue_after_failure(
                        project, "aggregate_retry", failure
                    ) or failure
                validation = self.validator.validate(context)
                if (
                    not validation.is_valid
                    and validation.metadata.get("normalized_verdict") == "INFRA_RETRY"
                ):
                    retry_metadata = {
                        key: value
                        for key, value in validation.metadata.items()
                        if key != "error_class"
                    }
                    failure = ExecutionResult.failed(
                        "PERMANENT_JUDGE_INFRASTRUCTURE",
                        returncode=2,
                        exhausted_error_class=str(
                            validation.metadata.get("error_class")
                            or "TRANSIENT_JUDGE_INFRASTRUCTURE"
                        ),
                        retried_roles=retried_roles,
                        **retry_metadata,
                    )
                    return self._continue_after_failure(
                        project, "aggregate_retry", failure
                    ) or failure
        resume_after = validation.metadata.get("resume_after_step")
        verdict = _verdict(project / "judge_evaluation.md")
        result_metadata = {
            "judge_verdict": verdict,
            "judge_completed": True,
            "gate2_delivery_override": self._record_delivery_override(
                project, verdict, stage="aggregate"
            ),
        }
        if resume_after is not None:
            return ExecutionResult.succeeded(
                resume_after_step=int(resume_after), **result_metadata
            )
        if not validation.is_valid:
            error_class = str(
                validation.metadata.get("error_class")
                or "TRANSIENT_JUDGE_INFRASTRUCTURE"
            )
            failure_metadata = {
                key: value
                for key, value in validation.metadata.items()
                if key != "error_class"
            }
            failure = ExecutionResult.failed(
                error_class,
                returncode=2,
                **failure_metadata,
                **result_metadata,
            )
            return self._continue_after_failure(project, "aggregate", failure) or failure
        return ExecutionResult.succeeded(**result_metadata)

    def _run_role_with_retry(
        self, context, role: str, template: str
    ) -> ExecutionResult:
        last = ExecutionResult.failed(
            "TRANSIENT_JUDGE_ROLE", returncode=1, role=role
        )
        for role_attempt in (1, 2):
            last = self._run_role(context, role, template)
            if last.returncode == 0:
                return ExecutionResult.succeeded(
                    **last.metadata, role_attempts=role_attempt
                )
        return ExecutionResult.failed(
            "PERMANENT_JUDGE_INFRASTRUCTURE",
            returncode=last.returncode,
            exhausted_error_class=last.error_class,
            role_attempts=2,
            **last.metadata,
        )

    def _reaggregate(self, project: Path) -> ExecutionResult | None:
        bound = self.runner.python(
            self.factory_root,
            project,
            "scripts/judgment_receipt.py",
            ["bind-group", project],
            label="judge_bind_group_retry",
            timeout_seconds=120,
        )
        if not bound.accepted:
            return ExecutionResult.failed(
                "TRANSIENT_JUDGE_PROVENANCE", returncode=bound.returncode
            )
        aggregate = self.runner.python(
            self.factory_root,
            project,
            "scripts/aggregate_judges.py",
            [
                "--math", project / "judge_outputs/math.md",
                "--execution", project / "judge_outputs/execution.md",
                "--paper", project / "judge_outputs/paper.md",
                "--math-manifest", project / "judge_packets/math/manifest.json",
                "--execution-manifest", project / "judge_packets/execution/manifest.json",
                "--paper-manifest", project / "judge_packets/paper/manifest.json",
                "--output", project / "judge_evaluation.md",
                "--json", project / "judge_outputs/aggregate.json",
                "--base", project.name,
            ],
            label="judge_aggregate_retry",
            timeout_seconds=300,
        )
        if not aggregate.accepted:
            return ExecutionResult.failed(
                "TRANSIENT_JUDGE_AGGREGATION", returncode=aggregate.returncode
            )
        return None

    @staticmethod
    def _indeterminate_roles(project: Path) -> list[str]:
        try:
            aggregate = json.loads(
                (project / "judge_outputs/aggregate.json").read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            return []
        declared = aggregate.get("indeterminate_roles")
        if not isinstance(declared, list):
            return []
        return [role for role in JudgeStep.ROLE_PROMPTS if role in declared]

    @staticmethod
    def _delivery_override_active(project: Path) -> bool:
        path = project / "gate2_delivery_override.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        return bool(
            payload.get("enabled") is True
            and payload.get("scope") == "continue_to_step16"
            and str(payload.get("reason", "")).strip()
        )

    @classmethod
    def _record_delivery_override(
        cls,
        project: Path,
        verdict: str,
        *,
        stage: str,
        error_class: str = "",
        returncode: int | None = None,
    ) -> bool:
        """Record an explicit continuation without changing the Gate 2 verdict."""
        if not cls._delivery_override_active(project):
            return False
        log_path = project / "logs" / "gate2_continuation_override.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        failure = ""
        if error_class:
            failure = f" error_class={error_class} returncode={returncode};"
        with log_path.open("a", encoding="utf-8") as stream:
            stream.write(
                f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} "
                f"Gate2 stage={stage} verdict={verdict or 'MISSING'};{failure} "
                "user continuation override active; scope=continue_to_step16; "
                "quality PASS not fabricated.\n"
            )
        return True

    @classmethod
    def _continue_after_failure(
        cls, project: Path, stage: str, failure: ExecutionResult
    ) -> ExecutionResult | None:
        verdict = _verdict(project / "judge_evaluation.md")
        if not cls._record_delivery_override(
            project,
            verdict,
            stage=stage,
            error_class=failure.error_class,
            returncode=failure.returncode,
        ):
            return None
        return ExecutionResult.succeeded(
            judge_completed=False,
            judge_verdict=verdict,
            judge_failure_stage=stage,
            judge_error_class=failure.error_class,
            judge_returncode=failure.returncode,
            gate2_delivery_override=True,
        )

    def _run_role(self, context, role: str, template: str) -> ExecutionResult:
        project = context.project_dir
        output = project / "judge_outputs" / f"{role}.md"
        snapshot = project / "judge_outputs" / f"{role}.rendered_prompt.txt"
        output.parent.mkdir(parents=True, exist_ok=True)
        prompt = self.renderer.render(template, project, step_key=f"13_{role}")
        prompt += self._phase_instructions(context.step_id, role)
        snapshot.write_text(prompt, encoding="utf-8")
        final_response = project / "tmp" / "native_judges" / role / "final_response.md"
        final_response.parent.mkdir(parents=True, exist_ok=True)
        for stale in (
            output,
            output.with_suffix(output.suffix + ".llm-result.json"),
            output.with_name(f"{role}.grounding.json"),
            final_response,
        ):
            stale.unlink(missing_ok=True)
        result = self.dispatcher.execute(
            ModelRequest(
                project_dir=project,
                step_id=13,
                attempt=context.attempt,
                prompt=prompt,
                timeout_seconds=min(context.timeout_seconds, 3_600),
                hang_timeout_seconds=1_800,
                output_file=output,
                context_files=(
                    f"judge_packets/{role}/context.txt",
                    f"judge_packets/{role}/manifest.json",
                    "judge_packets/objective_evidence.json",
                ),
                effective_prompt_file=snapshot,
                isolated=True,
                final_response_file=final_response,
            ),
            step_key=13,
            defaults=self.contract.default_models,
        )
        if not _verdict(output) and _verdict(final_response):
            shutil.copyfile(final_response, output)
        if result.returncode != 0 or not output.is_file() or not _verdict(output):
            missing_artifacts = []
            if not output.is_file():
                missing_artifacts.append(str(output.relative_to(project)))
            elif not _verdict(output):
                missing_artifacts.append(f"{output.relative_to(project)}::VERDICT")
            return ExecutionResult.failed(
                "TRANSIENT_JUDGE_ROLE",
                returncode=result.returncode or 1,
                role=role,
                dispatcher_error_class=result.error_class,
                missing_artifacts=missing_artifacts,
                **result.metadata,
            )
        model_id = str(result.metadata.get("model_id") or self.contract.default_models[0])
        backend = str(result.metadata.get("backend") or "unknown")
        model = str(result.metadata.get("model") or model_id)
        annotated = self.runner.python(
            self.factory_root,
            project,
            "scripts/judgment_receipt.py",
            [
                "annotate-role", project,
                "--role", role,
                "--registry-model-id", model_id,
                "--backend", backend,
                "--model", model,
                "--transport", "native_model_backend",
                "--prompt-file", snapshot,
                "--timeout-seconds", "3600",
            ],
            label=f"judge_annotate_{role}",
            timeout_seconds=120,
        )
        if not annotated.accepted:
            return ExecutionResult.failed(
                "TRANSIENT_JUDGE_PROVENANCE", returncode=annotated.returncode, role=role
            )
        return ExecutionResult.succeeded(role=role, model_id=model_id, backend=backend)

    @staticmethod
    def _phase_instructions(step_id: int, role: str) -> str:
        instructions = [
            "",
            "NATIVE ISOLATED JUDGE OUTPUT CONTRACT:",
            "- These role-specific instructions override any general startup request to read "
            "project guides, human review, memory, git status, or worktrees.",
            "- Do not read those general project files. The only permitted inputs are exactly "
            f"judge_packets/{role}/context.txt, judge_packets/{role}/manifest.json, and "
            "judge_packets/objective_evidence.json.",
            f"- The generic paths judge_packets/context.txt and judge_packets/manifest.json do "
            f"not exist. Never omit the {role}/ directory.",
            "- Write only the required judge output file.",
            "- Return the exact same protocol text as the final response; do not return a summary.",
        ]
        if role == "paper" and step_id == 13:
            instructions.extend(
                [
                    "- REVIEW_PHASE: PROVISIONAL_STEP_13.",
                    "- Step 14 has deliberately not run yet. The exact LaTeX abstract placeholder "
                    "required by the workflow is expected at this phase.",
                    "- Exclude that expected abstract placeholder from scoring and do not report it "
                    "as an issue or use it to determine the verdict.",
                ]
            )
        elif role == "paper":
            instructions.extend(
                [
                    "- REVIEW_PHASE: FINAL_SUBMISSION.",
                    "- Step 14 and Step 15 must already be complete. Any remaining abstract "
                    "placeholder is a blocking delivery defect.",
                ]
            )
        return "\n".join(instructions) + "\n"

    def validate(self, context):
        return self.validator.validate(context)

    def recover(self, context, error):
        return _recover(self.validator, context)


@dataclass
class DeliveryStep:
    contract: StepContract
    factory_root: Path
    judge_step: JudgeStep
    validator: NativeArtifactValidator
    runner: CommandRunner
    fingerprinter: Callable[[Path, str], str] | None = None
    audit_service: object | None = None

    def prepare(self, context):
        return prepare_human_gates(context.project_dir, context.step_id)

    def execute(self, context) -> ExecutionResult:
        project = context.project_dir
        base = project.name
        if self.audit_service is None:
            from ..audit.service import FinalAuditService

            audit_service = FinalAuditService(
                self.factory_root,
                self.judge_step,
                getattr(self.judge_step, "validator", self.validator),
                self.runner,
                self.fingerprinter,
            )
        else:
            audit_service = self.audit_service
        outcome = audit_service.run(context)
        audit = outcome.execution
        if audit.returncode != 0 or audit.metadata.get("resume_after_step") is not None:
            return audit
        if not outcome.record.delivery_allowed:
            return ExecutionResult.failed(
                "PERMANENT_AUDIT_NOT_APPROVED",
                returncode=2,
                audit_status=outcome.record.status.value,
                audit_snapshot=outcome.snapshot.snapshot_id,
            )

        pdf = project / f"{base}_paper.pdf"
        papers = self.factory_root / "papers"
        papers.mkdir(parents=True, exist_ok=True)
        published = papers / f"{base}_paper.pdf"
        temporary = papers / f".{base}_paper.pdf.tmp"
        shutil.copyfile(pdf, temporary)
        temporary.replace(published)
        package = self.runner.python(
            self.factory_root,
            project,
            "scripts/package_submission.py",
            [project, base, papers / f"{base}_submission.zip"],
            label="package_submission",
            timeout_seconds=600,
        )
        if not package.accepted:
            return ExecutionResult.failed("PERMANENT_PACKAGING", returncode=package.returncode)

        cleanup = self.factory_root / "scripts/cleanup_project_artifacts.py"
        if cleanup.is_file():
            self.runner.python(
                self.factory_root,
                project,
                "scripts/cleanup_project_artifacts.py",
                [project],
                label="delivery_cleanup",
                timeout_seconds=300,
                accepted=(0, 1),
            )
        return ExecutionResult.succeeded(
            **audit.metadata,
            input_fingerprint=outcome.snapshot.snapshot_id,
            published_pdf=str(published),
            submission_zip=str(papers / f"{base}_submission.zip"),
        )

    def validate(self, context):
        return self.validator.validate(context)

    def recover(self, context, error):
        return _recover(self.validator, context)

    @staticmethod
    def _decision(project: Path, *, prefer_new: bool = False) -> str:
        """Compatibility alias; decision routing is owned by the audit service."""

        from ..audit.service import FinalAuditService

        return FinalAuditService._decision(project, prefer_new=prefer_new)
