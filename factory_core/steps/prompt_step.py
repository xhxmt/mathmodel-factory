from __future__ import annotations

from dataclasses import dataclass

from ..adapters.models.backends import ModelRequest
from ..adapters.models.dispatcher import ModelDispatcher
from ..domain import (
    ExecutionResult,
    InvalidTransition,
    PrepareResult,
    RecoveryDecision,
    RecoveryDisposition,
    RevisionConflict,
    StepError,
    ValidationResult,
)
from ..effective_prompt import (
    EFFECTIVE_PROMPT_SCHEMA,
    attempt_key,
    build_effective_prompt_receipt,
)
from .catalog import StepContract
from .gates import prepare_human_gates
from .prompting import PromptRenderer
from .validators import NativeArtifactValidator


def _project_store(context):
    """An internal store for this project that inherits the engine's clock.

    These steps open their own store to read or bind the prompt-input receipt.
    With the default clock the ``PROMPT_INPUT_BOUND`` event is stamped from the
    real wall clock, so its timestamp - and the ``event_id`` hashed from it - is
    not reproducible, and two runs of the same work stop being comparable.  The
    engine puts its store's clock on the context for exactly this reason; the
    fallback keeps the previous behaviour for callers that supply none.
    """

    from ..storage import SQLiteStateStore

    clock = getattr(context, "clock", None)
    if clock is None:
        return SQLiteStateStore(context.project_dir)
    return SQLiteStateStore(context.project_dir, clock=clock)


@dataclass
class PromptStep:
    contract: StepContract
    renderer: PromptRenderer
    dispatcher: ModelDispatcher
    validator: NativeArtifactValidator

    def prepare(self, context) -> PrepareResult:
        from ..consultation_projection import ensure_all_consultation_projections
        from ..selection_projection import (
            rebuild_step3_projections,
            step3_projection_required,
            verify_step3_projections,
        )

        gate = prepare_human_gates(context.project_dir, context.step_id)
        if not gate.ready:
            return gate
        # Projections are rebuilt deterministically from SQLite.  A drifted
        # projection is repaired here and the Step goes on; only a projection
        # that cannot be rebuilt (or still disagrees after the rebuild) stops it.
        try:
            consultation = ensure_all_consultation_projections(context.project_dir)
        except (OSError, ValueError) as exc:
            return PrepareResult(
                ready=False,
                reason=f"Consultation projection rebuild failed: {exc}",
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
                try:
                    rebuild_step3_projections(context.project_dir)
                except ValueError as exc:
                    return PrepareResult(
                        ready=False,
                        reason=f"Step 3 selection projection rebuild failed: {exc}",
                        evidence=("chosen_method.md", "method_decision.md"),
                    )
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
            selected = int(baseline["selected_revision"])
            # An operator retry keeps the Stage cursor but restarts the attempt
            # count, so without this the retried attempt 1 would reuse the
            # failed attempt 1's immutable key and any input corrected for the
            # retry (a researcher note, human_review.md) would collide with it.
            for event in reversed(store.events()):
                if event.revision <= selected:
                    break
                if (
                    event.type == "STEP_RETRY_AUTHORIZED"
                    and event.payload.get("stage") == state.active_stage
                    and str(event.payload.get("subtask") or "")
                    == str(state.active_subtask or "")
                    and event.payload.get("source_step") == int(context.step_id)
                ):
                    return int(event.revision)
            return selected
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
        store = _project_store(context)
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
        store = _project_store(context)
        if not store.exists or store.load().control_mode != "engine":
            return ExecutionResult.failed(
                "PERMANENT_PROMPT_INPUT_AUTHORITY_INVALID",
                returncode=2,
                prompt_input_mode="non_engine_state",
                prompt_input_receipt_durable=False,
            )
        state = store.load()
        from ..stages import stage_for_step

        if (state.active_step != context.step_id
                or (state.active_stage is not None
                    and state.active_stage != stage_for_step(context.step_id).id)):
            return ExecutionResult.failed(
                "PERMANENT_EXECUTION_IDENTITY_MISMATCH", returncode=2,
                requested_step=context.step_id, active_step=state.active_step,
                active_stage=state.active_stage,
            )
        prompt, _researcher_note, proposed = self._build_inputs(context)
        # The receipt records which inputs this attempt was sent.  It never
        # decides whether the Step may run or whether its work counts: inputs
        # that change during or after the run (a researcher note, the model
        # configuration, the ledger a rework block quotes) are recorded, not
        # rejected.
        try:
            _bound_state, stored = store.bind_prompt_attempt_input(
                expected_revision=context.revision,
                receipt=proposed,
            )
            receipt_metadata = self._receipt_metadata(stored)
        except RevisionConflict:
            raise
        except InvalidTransition as exc:
            # The attempt key already holds a receipt for other inputs.  That
            # stored record stays as it is; this attempt carries its own.
            receipt_metadata = {
                **self._receipt_metadata(proposed),
                "prompt_input_receipt_durable": False,
                "prompt_input_errors": [str(exc)],
            }
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
        return ExecutionResult(
            returncode=result.returncode,
            error_class=result.error_class,
            metadata={**result.metadata, **receipt_metadata},
        )

    def validate(self, context):
        return self.validator.validate(context)

    def recover(self, context, error: StepError) -> RecoveryDecision:
        from ..storage import SQLiteStateStore

        store = _project_store(context)
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
