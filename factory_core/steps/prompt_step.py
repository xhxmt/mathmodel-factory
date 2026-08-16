from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from ..adapters.models.backends import ModelRequest
from ..adapters.models.dispatcher import ModelDispatcher
from ..domain import ExecutionResult, PrepareResult, RecoveryDecision, StepError
from .catalog import StepContract
from .gates import prepare_human_gates
from .prompting import PromptRenderer
from .validators import NativeArtifactValidator


@dataclass
class PromptStep:
    contract: StepContract
    renderer: PromptRenderer
    dispatcher: ModelDispatcher
    validator: NativeArtifactValidator

    def prepare(self, context) -> PrepareResult:
        from ..consultation_projection import verify_consultation_projections
        from ..selection_projection import (
            rebuild_step3_projections,
            step3_projection_required,
            verify_step3_projections,
        )

        gate = prepare_human_gates(context.project_dir, context.step_id)
        if not gate.ready:
            return gate
        consultation = verify_consultation_projections(context.project_dir)
        if not consultation.valid:
            return PrepareResult(
                ready=False,
                reason=(
                    "Consultation projection drift: "
                    + "; ".join(consultation.errors)
                ),
                evidence=("human_review.md",),
            )
        if context.step_id == 3 and step3_projection_required(context.project_dir):
            try:
                rebuild_step3_projections(context.project_dir)
            except ValueError as exc:
                return PrepareResult(ready=False, reason=str(exc))
        if context.step_id == 4 and step3_projection_required(context.project_dir):
            verification = verify_step3_projections(context.project_dir)
            if not verification.valid:
                return PrepareResult(
                    ready=False,
                    reason=(
                        "Step 3 selection projection drift: "
                        + "; ".join(verification.errors)
                    ),
                    evidence=("chosen_method.md", "method_decision.md"),
                )
        return PrepareResult.prepared()

    def execute(self, context) -> ExecutionResult:
        assert self.contract.prompt is not None
        researcher_note = self.renderer.user_note(
            context.project_dir.name, context.step_id
        )
        human_review = context.project_dir / "human_review.md"
        human_review_sha256 = (
            hashlib.sha256(human_review.read_bytes()).hexdigest()
            if human_review.is_file() and not human_review.is_symlink()
            else "MISSING"
        )
        prompt = self.renderer.render(
            self.contract.prompt,
            context.project_dir,
            step_key=context.step_id,
            include_preamble=context.step_id != 0,
            researcher_note=researcher_note,
        )
        result = self.dispatcher.execute(
            ModelRequest(
                project_dir=context.project_dir,
                step_id=context.step_id,
                attempt=context.attempt,
                prompt=prompt,
                timeout_seconds=context.timeout_seconds,
                hang_timeout_seconds=self.contract.hang_timeout_seconds,
                deadline_epoch=context.deadline_epoch,
            ),
            step_key=context.step_id,
            defaults=self.contract.default_models,
        )
        from ..consultation_projection import verify_consultation_projections

        consultation = verify_consultation_projections(context.project_dir)
        input_receipt = {
            "schema_version": "factory-effective-prompt-v1",
            "consultation_decision_ids": [
                str(decision.get("decision_id") or "")
                for decision in consultation.decisions
            ],
            "researcher_note_sha256": hashlib.sha256(
                researcher_note.encode("utf-8")
            ).hexdigest(),
            "human_review_sha256": human_review_sha256,
        }
        input_receipt["effective_prompt_sha256"] = hashlib.sha256(
            prompt.encode("utf-8")
        ).hexdigest()
        input_receipt["prompt_inputs_sha256"] = hashlib.sha256(
            json.dumps(
                input_receipt, ensure_ascii=False, sort_keys=True
            ).encode("utf-8")
        ).hexdigest()
        current_human_review_sha256 = (
            hashlib.sha256(human_review.read_bytes()).hexdigest()
            if human_review.is_file() and not human_review.is_symlink()
            else "MISSING"
        )
        if (
            not consultation.valid
            or current_human_review_sha256 != human_review_sha256
        ):
            return ExecutionResult.failed(
                "PERMANENT_CONSULTATION_INPUT_DRIFT",
                returncode=2,
                **{
                    **result.metadata,
                    **input_receipt,
                    "consultation_projection_errors": list(
                        consultation.errors
                    ),
                    "human_review_changed_during_execution": (
                        current_human_review_sha256 != human_review_sha256
                    ),
                },
            )
        return ExecutionResult(
            returncode=result.returncode,
            error_class=result.error_class,
            metadata={**result.metadata, **input_receipt},
        )

    def validate(self, context):
        return self.validator.validate(context)

    def recover(self, context, error: StepError) -> RecoveryDecision:
        return RecoveryDecision.from_validation(
            self.validator.validate(context), active_step=context.step_id
        )
