from __future__ import annotations

import json
import os
import re
import shutil
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
        validation = self.validator.validate(context)
        if validation.is_valid:
            return ExecutionResult.succeeded(streams=streams)
        return ExecutionResult.failed(
            "TRANSIENT_PARALLEL_PROPOSALS",
            failed_streams=failures,
            reason=validation.reason,
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
            (project / "judge_evaluation.md").write_text(
                "VERDICT: PASS\n\nAblation: automated judge disabled.\n",
                encoding="utf-8",
            )
            return ExecutionResult.succeeded(ablation="ABLATE_NO_JUDGE")
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
        for role, template in self.ROLE_PROMPTS.items():
            result = self._run_role(context, role, template)
            if result.returncode != 0:
                return result
        bound = self.runner.python(
            self.factory_root,
            project,
            "scripts/judgment_receipt.py",
            ["bind-group", project],
            label="judge_bind_group",
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
            label="judge_aggregate",
            timeout_seconds=300,
        )
        if not aggregate.accepted:
            return ExecutionResult.failed(
                "TRANSIENT_JUDGE_AGGREGATION", returncode=aggregate.returncode
            )
        validation = self.validator.validate(context)
        resume_after = validation.metadata.get("resume_after_step")
        if resume_after is not None:
            return ExecutionResult.succeeded(
                resume_after_step=int(resume_after), judge_verdict=_verdict(project / "judge_evaluation.md")
            )
        return ExecutionResult.succeeded(judge_verdict=_verdict(project / "judge_evaluation.md"))

    def _run_role(self, context, role: str, template: str) -> ExecutionResult:
        project = context.project_dir
        output = project / "judge_outputs" / f"{role}.md"
        snapshot = project / "judge_outputs" / f"{role}.rendered_prompt.txt"
        prompt = self.renderer.render(template, project, step_key=f"13_{role}")
        snapshot.write_text(prompt, encoding="utf-8")
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
                final_response_file=output,
            ),
            step_key=13,
            defaults=self.contract.default_models,
        )
        if result.returncode != 0 or not output.is_file() or not _verdict(output):
            return ExecutionResult.failed(
                "TRANSIENT_JUDGE_ROLE", returncode=result.returncode or 1, role=role
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

    def prepare(self, context):
        return prepare_human_gates(context.project_dir, context.step_id)

    def execute(self, context) -> ExecutionResult:
        project = context.project_dir
        base = project.name
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
        if self._has_stub(project) or self._unresolved_blocking(project):
            return ExecutionResult.failed("PERMANENT_DELIVERY_ACCEPTANCE", returncode=2)
        checks = [
            (
                "scripts/verify_provenance.py",
                [project],
                "provenance_verification",
                project / "provenance_verification.latest.txt",
                (0,),
            ),
            (
                "scripts/verify_quality_contract.py",
                [
                    project,
                    "--factory-root", self.factory_root,
                    "--json-out", project / "quality_contract_verification.latest.json",
                    "--text-out", project / "quality_contract_verification.latest.txt",
                ],
                "quality_contract_verification",
                project / "logs/native_quality_contract.log",
                (0, 2),
            ),
        ]
        for script, args, label, log_path, accepted in checks:
            result = self.runner.python(
                self.factory_root,
                project,
                script,
                args,
                label=label,
                timeout_seconds=600,
                accepted=accepted,
                log_path=log_path,
            )
            if not result.accepted:
                return ExecutionResult.failed(
                    "PERMANENT_DELIVERY_ACCEPTANCE", returncode=result.returncode, check=label
                )
        compile_result = self.runner.run(
            project,
            [self.factory_root / "compile_paper.sh", project, base],
            label="compile_paper",
            timeout_seconds=1_800,
            cwd=self.factory_root,
        )
        pdf = project / f"{base}_paper.pdf"
        if not compile_result.accepted or not pdf.is_file() or pdf.stat().st_size == 0:
            return ExecutionResult.failed("TRANSIENT_COMPILATION", returncode=compile_result.returncode)
        visual_args: list[str | Path] = [pdf, "--output", project / "judge_outputs/visual_gate.json"]
        tex_log = project / f"{base}_paper.log"
        if tex_log.is_file():
            visual_args.extend(["--tex-log", tex_log])
        visual = self.runner.python(
            self.factory_root,
            project,
            "scripts/pdf_visual_gate.py",
            visual_args,
            label="pdf_visual_gate",
            timeout_seconds=600,
            accepted=(0, 1, 2),
        )
        if not visual.accepted:
            return ExecutionResult.failed("TRANSIENT_VISUAL_GATE", returncode=visual.returncode)
        judge = self.judge_step.execute(context)
        if judge.returncode != 0:
            return judge
        if judge.metadata.get("resume_after_step") is not None:
            return judge
        route = self.runner.python(
            self.factory_root,
            project,
            "scripts/judge_decision_router.py",
            [
                "--aggregate", project / "judge_outputs/aggregate.json",
                "--visual-gate", project / "judge_outputs/visual_gate.json",
                "--policy-mode", os.getenv("JUDGE_POLICY_MODE", "shadow").lower(),
                "--output", project / "judge_outputs/decision_route.json",
            ],
            label="judge_route",
            timeout_seconds=120,
        )
        if not route.accepted:
            return ExecutionResult.failed("TRANSIENT_JUDGE_ROUTING", returncode=route.returncode)
        decision = self._decision(project)
        if decision in {"REOPEN_REVISION_TEXT", "REOPEN_REVISION_MODEL"}:
            resume = 11 if decision == "REOPEN_REVISION_TEXT" else self.validator._gate2_resume(project, decision)
            return ExecutionResult.succeeded(resume_after_step=resume, final_decision=decision)
        if decision != "PASS":
            return ExecutionResult.failed("PERMANENT_FINAL_JUDGE", returncode=2, decision=decision)
        if self.fingerprinter is None:
            from scripts.submission_fingerprint import submission_fingerprint

            fingerprint = submission_fingerprint(project, base)
        else:
            fingerprint = self.fingerprinter(project, base)
        for command, args, label in (
            (
                "scripts/judgment_receipt.py",
                ["build", project, "--base", base, "--input-fingerprint", fingerprint],
                "receipt_build",
            ),
            (
                "scripts/judgment_receipt.py",
                ["verify", project, "--base", base, "--input-fingerprint", fingerprint, "--require-pass"],
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
                return ExecutionResult.failed("PERMANENT_JUDGMENT_RECEIPT", returncode=result.returncode)
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
        (project / "judge_outputs/final_submission.sha256").write_text(
            fingerprint + "\n", encoding="ascii"
        )
        return ExecutionResult.succeeded(input_fingerprint=fingerprint)

    def validate(self, context):
        return self.validator.validate(context)

    def recover(self, context, error):
        return _recover(self.validator, context)

    @staticmethod
    def _has_stub(project: Path) -> bool:
        models = project / "models"
        return models.is_dir() and any(models.rglob("*.stub"))

    @staticmethod
    def _unresolved_blocking(project: Path) -> bool:
        ledger = project / "audit_issue_ledger.md"
        if not ledger.is_file():
            return False
        for line in ledger.read_text(encoding="utf-8", errors="replace").splitlines():
            cells = [cell.strip().replace("*", "").replace("`", "") for cell in line.split("|")]
            if len(cells) > 6 and cells[3].upper() == "BLOCKING" and "RESOLVED" not in cells[6].upper():
                return True
        return False

    @staticmethod
    def _decision(project: Path) -> str:
        try:
            value = json.loads((project / "judge_outputs/decision_route.json").read_text(encoding="utf-8"))
            return str(value.get("effective_decision", ""))
        except (OSError, json.JSONDecodeError, AttributeError):
            return ""
