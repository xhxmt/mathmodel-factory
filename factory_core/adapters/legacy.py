from __future__ import annotations

import os
import re
import signal
import subprocess
import threading
from pathlib import Path

from ..domain import ExecutionResult, PendingAction, StepContext, ValidationResult
from ..registry import StepDefinition, StepRegistry


STEP_NAMES = {
    0: "problem_setup",
    1: "research_and_viability",
    2: "parallel_model_proposals",
    3: "method_selection",
    4: "model_construction",
    5: "solve",
    6: "sensitivity",
    7: "model_evaluation",
    8: "visualization",
    9: "paper_draft",
    10: "numerical_gate",
    11: "constructive_review",
    12: "revision",
    13: "judge_gate",
    14: "abstract",
    15: "polish",
    16: "delivery",
}

STEP_TIMEOUTS = {
    0: 3_600,
    1: 14_400,
    2: 28_800,
    3: 7_200,
    4: 14_400,
    5: 14_400,
    6: 10_800,
    7: 7_200,
    8: 10_800,
    9: 14_400,
    10: 10_800,
    11: 7_200,
    12: 14_400,
    13: 10_800,
    14: 7_200,
    15: 10_800,
    16: 3_600,
}


def _checkpoint_step(project: Path) -> int:
    path = project / "checkpoint.md"
    if not path.is_file():
        return -1
    match = re.search(
        r"Last completed step\*{0,2}\s*[:：]\s*(-?\d+)",
        path.read_text(encoding="utf-8", errors="replace"),
    )
    return int(match.group(1)) if match else -1


class LegacyStepHandler:
    def __init__(self, factory_root: str | Path, runner: str | Path):
        self.factory_root = Path(factory_root).resolve()
        self.runner = Path(runner).resolve()

    def execute(self, context: StepContext) -> ExecutionResult:
        env = {
            **os.environ,
            "FACTORY": str(self.factory_root),
            "FACTORY_ENGINE_SINGLE_STEP": "1",
            "FACTORY_ENGINE_STEP_ID": str(context.step_id),
            "FACTORY_MODELING_ONLY": "1",
        }
        process: subprocess.Popen | None = None
        manage_signal = threading.current_thread() is threading.main_thread()
        previous_term = signal.getsignal(signal.SIGTERM) if manage_signal else None

        def terminate_child(_signum, _frame):
            if process is not None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            raise SystemExit(143)

        if manage_signal:
            signal.signal(signal.SIGTERM, terminate_child)
        try:
            process = subprocess.Popen(
                [str(self.runner), str(context.project_dir)],
                cwd=self.factory_root,
                env=env,
                start_new_session=True,
            )
            returncode = process.wait(timeout=context.timeout_seconds + 180)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=10)
            except (PermissionError, ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except (PermissionError, ProcessLookupError):
                    pass
                process.wait()
            return ExecutionResult.failed("TRANSIENT_TIMEOUT", returncode=124)
        finally:
            if manage_signal:
                signal.signal(signal.SIGTERM, previous_term)
        metadata = self._outcome_metadata(context.project_dir)
        if returncode == 75:
            metadata["resume_after_step"] = _checkpoint_step(context.project_dir)
            return ExecutionResult.succeeded(**metadata)
        if returncode == 0:
            completed_through = _checkpoint_step(context.project_dir)
            if completed_through > context.step_id:
                metadata["completed_through_step"] = completed_through
            return ExecutionResult.succeeded(**metadata)
        error_class = "PERMANENT_LEGACY" if returncode in {2, 64} else "TRANSIENT_LEGACY"
        return ExecutionResult.failed(error_class, returncode=returncode, **metadata)

    @staticmethod
    def _outcome_metadata(project: Path) -> dict:
        metadata: dict = {}
        heartbeat = project / ".heartbeat"
        if heartbeat.is_file():
            metadata["legacy_heartbeat"] = heartbeat.read_text(
                encoding="utf-8", errors="replace"
            ).strip()
        if (project / ".killed").is_file():
            metadata["killed"] = True
        return metadata


class LegacyArtifactValidator:
    def __init__(self, factory_root: str | Path, runner: str | Path):
        self.factory_root = Path(factory_root).resolve()
        self.runner = Path(runner).resolve()

    def validate(self, context: StepContext) -> ValidationResult:
        if context.step_id in {13, 16} and any(
            (context.project_dir / marker).is_file()
            for marker in (
                ".gate2_reopen_to_revision",
                ".final_judge_reopen_pending",
            )
        ):
            resume_after = _checkpoint_step(context.project_dir)
            if -1 <= resume_after < context.step_id:
                return ValidationResult.invalid(
                    f"legacy reopen requests resume after step {resume_after}",
                    metadata={"resume_after_step": resume_after},
                )
        selection = context.project_dir / "selection" / "step3_options.json"
        decision = context.project_dir / "selection" / "step3_decision.json"
        if context.step_id == 3 and selection.is_file() and not decision.is_file():
            return ValidationResult.awaiting(
                PendingAction(type="step3_selection", gate="step3"),
                "selection/step3_options.json",
            )
        consultation = context.project_dir / ".awaiting_consultation"
        if consultation.is_file():
            text = consultation.read_text(encoding="utf-8", errors="replace")
            gate = re.search(r"GATE:([^\s]+)", text)
            gate_name = gate.group(1) if gate else None
            if not self._consultation_ready(context.project_dir, gate_name):
                return ValidationResult.awaiting(
                    PendingAction(type="human_consultation", gate=gate_name),
                    ".awaiting_consultation",
                )
        heartbeat = context.project_dir / ".heartbeat"
        if heartbeat.is_file() and heartbeat.read_text(
            encoding="utf-8", errors="replace"
        ).startswith("AWAITING_STEP8_5"):
            return ValidationResult.awaiting(
                PendingAction(type="human_consultation", gate="step8_5"),
                "entry_gate.md",
            )
        inferred = self.infer_step(context.project_dir)
        if inferred >= context.step_id:
            return ValidationResult.valid(
                f"legacy_inferred_step:{inferred}",
                metadata={"completed_through_step": inferred},
            )
        return ValidationResult.invalid(
            f"legacy validator inferred step {inferred}, expected at least {context.step_id}"
        )

    def infer_step(self, project_dir: str | Path) -> int:
        result = subprocess.run(
            [str(self.runner), "--infer-step", str(project_dir)],
            cwd=self.factory_root,
            env={**os.environ, "FACTORY": str(self.factory_root)},
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "legacy infer-step failed")
        return int(result.stdout.strip())

    @staticmethod
    def _consultation_ready(project: Path, gate: str | None) -> bool:
        review = project / "human_review.md"
        if not review.is_file():
            return False
        text = review.read_text(encoding="utf-8", errors="replace")
        if gate is None:
            return "STATUS: READY" in text
        pattern = rf"##\s+CONSULT\s+{re.escape(gate)}\b[\s\S]*?STATUS:\s*READY"
        return re.search(pattern, text, re.IGNORECASE) is not None


def build_legacy_registry(factory_root: str | Path, runner: str | Path) -> StepRegistry:
    handler = LegacyStepHandler(factory_root, runner)
    validator = LegacyArtifactValidator(factory_root, runner)
    registry = StepRegistry()
    for step_id, name in STEP_NAMES.items():
        max_attempts = (
            1 if step_id == 0 else 3 if step_id == 6 else 2 if step_id == 13 else 5
        )
        registry.register(
            StepDefinition(
                id=step_id,
                name=name,
                timeout_seconds=STEP_TIMEOUTS[step_id],
                max_attempts=max_attempts,
                handler=handler,
                validator=validator,
            )
        )
    return registry
