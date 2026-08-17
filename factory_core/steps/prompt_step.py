from __future__ import annotations

from dataclasses import dataclass

from ..adapters.models.backends import ModelRequest
from ..adapters.models.dispatcher import ModelDispatcher
from ..domain import (
    ExecutionResult,
    PrepareResult,
    RecoveryDecision,
    RecoveryDisposition,
    StepError,
    ValidationResult,
)
from ..effective_prompt import (
    EFFECTIVE_PROMPT_SCHEMA,
    attempt_key,
    build_effective_prompt_receipt,
    verify_effective_prompt_receipt,
)
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

    requires_prompt_input_receipt = True

    def prepare(self, context) -> PrepareResult:
        from ..consultation_projection import (
            ensure_all_consultation_projections,
            verify_consultation_projections,
        )
        from ..selection_projection import (
            rebuild_step3_projections,
            step3_projection_required,
            verify_step3_projections,
        )

        gate = prepare_human_gates(context.project_dir, context.step_id)
        if not gate.ready:
            return gate
        observed = verify_consultation_projections(context.project_dir)
        try:
            consultation = ensure_all_consultation_projections(context.project_dir)
        except (OSError, ValueError) as exc:
            return PrepareResult(
                ready=False,
                reason=f"Consultation projection rebuild failed: {exc}",
                evidence=("human_review.md",),
            )
        if not observed.valid:
            errors = observed.errors or consultation.errors
            return PrepareResult(
                ready=False,
                reason=(
                    "Consultation projection drift: "
                    + "; ".join(errors)
                    + "; deterministic projection rebuilt, retry prepare"
                ),
                evidence=("human_review.md",),
            )
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

    @staticmethod
    def _attempt_selected_revision(store, context) -> int:
        state = store.load()
        baseline = store.stage_cursor_input()
        if (
            state.active_stage is not None
            and baseline is not None
            and int(baseline["stage_id"]) == int(state.active_stage)
            and str(baseline["subtask"]) == str(state.active_subtask or "")
            and int(baseline["source_step_id"]) == int(context.step_id)
        ):
            return int(baseline["selected_revision"])
        for event in reversed(store.events()):
            if (
                event.type == "STEP_STARTED"
                and event.step == context.step_id
                and event.attempt == context.attempt
            ):
                return int(event.revision)
        prior = store.latest_prompt_attempt_input(
            stage_id=state.active_stage,
            subtask=state.active_subtask,
            source_step_id=context.step_id,
            attempt=context.attempt,
        )
        if prior is not None and prior.get("selected_revision") is not None:
            return int(prior["selected_revision"])
        return int(context.revision)

    def _build_inputs(self, context, *, selected_revision: int | None = None):
        from ..storage import SQLiteStateStore

        assert self.contract.prompt is not None
        store = SQLiteStateStore(context.project_dir)
        state = store.load()
        attempt_revision = (
            self._attempt_selected_revision(store, context)
            if selected_revision is None
            else int(selected_revision)
        )
        researcher_note = self.renderer.user_note(
            context.project_dir.name, context.step_id
        )
        prompt = self.renderer.render(
            self.contract.prompt,
            context.project_dir,
            step_key=context.step_id,
            include_preamble=context.step_id != 0,
            researcher_note=researcher_note,
        )
        template = self.renderer.root / "prompts" / self.contract.prompt
        receipt = build_effective_prompt_receipt(
            project_dir=context.project_dir,
            factory_root=self.renderer.root,
            project_id=context.project_id,
            source_step_id=context.step_id,
            stage_id=state.active_stage,
            subtask=state.active_subtask,
            attempt=context.attempt,
            selected_revision=attempt_revision,
            prompt_template=template,
            prompt=prompt,
            researcher_note=researcher_note,
        )
        return prompt, researcher_note, receipt

    def _execute_standalone_compatibility(self, context) -> ExecutionResult:
        """Preserve direct lifecycle use without fabricating durable evidence.

        Native engine execution always has an initialized SQLite store and
        therefore cannot enter this branch. The result deliberately omits the
        ``factory-effective-prompt-v1`` identity fields, so the Stage checkpoint
        guard would reject it if it were ever routed back into an engine run.
        """

        from ..storage import SQLiteStateStore

        assert self.contract.prompt is not None
        store = SQLiteStateStore(context.project_dir)
        if store.exists or int(context.revision) != 0:
            raise RuntimeError(
                "standalone PromptStep compatibility requires revision 0 and no workflow state"
            )
        import hashlib

        review = context.project_dir / "human_review.md"
        human_review_sha256 = (
            hashlib.sha256(review.read_bytes()).hexdigest()
            if review.is_file() and not review.is_symlink()
            else "MISSING"
        )
        researcher_note = self.renderer.user_note(
            context.project_dir.name, context.step_id
        )
        prompt = self.renderer.render(
            self.contract.prompt,
            context.project_dir,
            step_key=context.step_id,
            include_preamble=context.step_id != 0,
            researcher_note=researcher_note,
        )
        if store.exists:
            return ExecutionResult.failed(
                "PERMANENT_PROMPT_INPUT_AUTHORITY_CHANGED",
                returncode=2,
                prompt_input_mode="standalone_compatibility",
                prompt_input_receipt_durable=False,
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
        if store.exists:
            return ExecutionResult.failed(
                "PERMANENT_PROMPT_INPUT_AUTHORITY_CHANGED",
                returncode=2,
                **result.metadata,
                prompt_input_mode="standalone_compatibility",
                prompt_input_receipt_durable=False,
            )
        from ..consultation_projection import verify_consultation_projections

        consultation = verify_consultation_projections(context.project_dir)
        current_human_review_sha256 = (
            hashlib.sha256(review.read_bytes()).hexdigest()
            if review.is_file() and not review.is_symlink()
            else "MISSING"
        )
        if store.exists:
            return ExecutionResult.failed(
                "PERMANENT_PROMPT_INPUT_AUTHORITY_CHANGED",
                returncode=2,
                **result.metadata,
                prompt_input_mode="standalone_compatibility",
                prompt_input_receipt_durable=False,
            )
        if (
            not consultation.valid
            or current_human_review_sha256 != human_review_sha256
        ):
            return ExecutionResult.failed(
                "PERMANENT_CONSULTATION_INPUT_DRIFT",
                returncode=2,
                **result.metadata,
                prompt_input_mode="standalone_compatibility",
                prompt_input_receipt_durable=False,
                consultation_projection_errors=list(consultation.errors),
                human_review_changed_during_execution=(
                    current_human_review_sha256 != human_review_sha256
                ),
            )
        return ExecutionResult(
            returncode=result.returncode,
            error_class=result.error_class,
            metadata={
                **result.metadata,
                "prompt_input_mode": "standalone_compatibility",
                "prompt_input_receipt_durable": False,
            },
        )

    @staticmethod
    def _receipt_metadata(receipt: dict) -> dict:
        return {
            "prompt_input_schema": EFFECTIVE_PROMPT_SCHEMA,
            "prompt_input_receipt_id": receipt["receipt_id"],
            "prompt_input_attempt_key": receipt["attempt_key"],
            "effective_prompt_sha256": receipt["effective_prompt_sha256"],
            "prompt_inputs_sha256": receipt["prompt_inputs_sha256"],
            "consultation_decision_ids": receipt[
                "consultation_decision_ids"
            ],
            "researcher_note_sha256": receipt["researcher_note_sha256"],
            "human_review_sha256": receipt["human_review_sha256"],
            "prompt_template_sha256": receipt["prompt_template_sha256"],
            "model_config_sha256": receipt["model_config_sha256"],
            "prompt_input_receipt": receipt,
        }

    def execute(self, context) -> ExecutionResult:
        from ..storage import SQLiteStateStore

        store = SQLiteStateStore(context.project_dir)
        if not store.exists:
            return self._execute_standalone_compatibility(context)
        if store.load().control_mode != "engine":
            return ExecutionResult.failed(
                "PERMANENT_PROMPT_INPUT_AUTHORITY_INVALID",
                returncode=2,
                prompt_input_mode="non_engine_state",
                prompt_input_receipt_durable=False,
            )
        prompt, _researcher_note, proposed = self._build_inputs(context)
        _bound_state, stored = store.bind_prompt_attempt_input(
            expected_revision=context.revision,
            receipt=proposed,
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
        if not consultation.valid:
            import hashlib

            review = context.project_dir / "human_review.md"
            current_review_sha256 = (
                hashlib.sha256(review.read_bytes()).hexdigest()
                if review.is_file() and not review.is_symlink()
                else "MISSING"
            )
            return ExecutionResult.failed(
                "PERMANENT_CONSULTATION_INPUT_DRIFT",
                returncode=2,
                **result.metadata,
                **self._receipt_metadata(stored),
                consultation_projection_errors=list(consultation.errors),
                human_review_changed_during_execution=(
                    current_review_sha256
                    != stored.get("human_review_sha256")
                ),
            )
        try:
            _current_prompt, _current_note, current = self._build_inputs(
                context,
                selected_revision=int(stored["selected_revision"]),
            )
        except (OSError, ValueError) as exc:
            return ExecutionResult.failed(
                "PERMANENT_PROMPT_INPUT_DRIFT",
                returncode=2,
                **result.metadata,
                **self._receipt_metadata(stored),
                prompt_input_errors=[str(exc)],
            )
        valid, errors = verify_effective_prompt_receipt(stored, current)
        if not valid:
            return ExecutionResult.failed(
                "PERMANENT_PROMPT_INPUT_DRIFT",
                returncode=2,
                **result.metadata,
                **self._receipt_metadata(stored),
                prompt_input_errors=list(errors),
            )
        return ExecutionResult(
            returncode=result.returncode,
            error_class=result.error_class,
            metadata={**result.metadata, **self._receipt_metadata(stored)},
        )

    def validate(self, context):
        return self.validator.validate(context)

    def recover(self, context, error: StepError) -> RecoveryDecision:
        from ..storage import SQLiteStateStore

        store = SQLiteStateStore(context.project_dir)
        if not store.exists:
            return RecoveryDecision.from_validation(
                self.validator.validate(context), active_step=context.step_id
            )
        del error
        state = store.load()
        selected_revision = self._attempt_selected_revision(store, context)
        key = attempt_key(
            stage_id=state.active_stage,
            subtask=state.active_subtask,
            source_step_id=context.step_id,
            attempt=context.attempt,
            selected_revision=selected_revision,
        )
        stored = store.prompt_attempt_input(key)
        if stored is None:
            return RecoveryDecision(
                RecoveryDisposition.RETRY,
                reason="persistent effective prompt input receipt is missing",
                metadata={
                    "prompt_input_schema": EFFECTIVE_PROMPT_SCHEMA,
                    "prompt_input_receipt_valid": False,
                    "prompt_input_attempt_key": key,
                },
            )
        from ..consultation_projection import verify_consultation_projections
        import hashlib

        review = context.project_dir / "human_review.md"
        current_review_sha256 = (
            hashlib.sha256(review.read_bytes()).hexdigest()
            if review.is_file() and not review.is_symlink()
            else "MISSING"
        )
        consultation = verify_consultation_projections(context.project_dir)
        if (
            not consultation.valid
            or current_review_sha256 != stored.get("human_review_sha256")
        ):
            errors = list(consultation.errors)
            if current_review_sha256 != stored.get("human_review_sha256"):
                errors.append("effective prompt input drift: human_review_sha256")
            return RecoveryDecision(
                RecoveryDisposition.RETRY,
                reason="consultation input changed since the interrupted attempt",
                evidence=("human_review.md",),
                metadata={
                    "prompt_input_schema": EFFECTIVE_PROMPT_SCHEMA,
                    "prompt_input_receipt_valid": False,
                    "prompt_input_receipt_id": stored.get("receipt_id"),
                    "prompt_input_errors": errors,
                },
            )
        try:
            _prompt, _note, current = self._build_inputs(
                context,
                selected_revision=int(stored["selected_revision"]),
            )
            valid, errors = verify_effective_prompt_receipt(stored, current)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            valid, errors = False, (str(exc),)
        if not valid:
            return RecoveryDecision(
                RecoveryDisposition.RETRY,
                reason="effective prompt input changed since the interrupted attempt",
                evidence=(".factory/state.db",),
                metadata={
                    "prompt_input_schema": EFFECTIVE_PROMPT_SCHEMA,
                    "prompt_input_receipt_valid": False,
                    "prompt_input_receipt_id": stored.get("receipt_id"),
                    "prompt_input_errors": list(errors),
                },
            )
        validation = self.validator.validate(context)
        metadata = {
            **validation.metadata,
            **self._receipt_metadata(stored),
            "prompt_input_receipt_valid": True,
        }
        bound_validation = ValidationResult(
            is_valid=validation.is_valid,
            reason=validation.reason,
            pending_action=validation.pending_action,
            evidence=validation.evidence,
            metadata=metadata,
        )
        return RecoveryDecision.from_validation(
            bound_validation, active_step=context.step_id
        )
