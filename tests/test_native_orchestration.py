import json
import signal
import sys
import time
import zipfile
from pathlib import Path

from factory_core.adapters.infrastructure.commands import CommandResult
from factory_core.adapters.infrastructure.process import ProcessRequest, ProcessResult, ProcessSupervisor
from factory_core.adapters.models.backends import ApiAgentBackend, CodexCliBackend, ModelRequest
from factory_core.adapters.models.dispatcher import ModelDispatcher
from factory_core.domain import ExecutionResult, StepContext, ValidationResult, WorkflowStatus
from factory_core.engine import FactoryEngine
from factory_core.registry import ModelBackendRegistry
from factory_core.steps import STEP_CONTRACTS, build_native_registry, catalog_payload
from factory_core.steps.prompting import PromptRenderer
from factory_core.steps.prompt_step import PromptStep
from factory_core.steps.specialized import DeliveryStep, JudgeStep, ParallelProposalStep
from factory_core.steps.validators import NativeArtifactValidator
from factory_core.storage import SQLiteStateStore


class RecordingBackend:
    def __init__(self, results):
        self.results = list(results)
        self.requests = []

    def execute(self, request):
        self.requests.append(request)
        return self.results.pop(0)


def test_native_catalog_is_complete_unique_and_machine_readable():
    ids = [contract.id for contract in STEP_CONTRACTS]

    assert ids == list(range(17))
    assert len(ids) == len(set(ids))
    assert catalog_payload()["runtime_generation"] == "native_v2"
    assert all(contract.timeout_seconds > 0 for contract in STEP_CONTRACTS)


def test_process_supervisor_reaps_term_ignoring_process_after_timeout(tmp_path):
    log = tmp_path / "process.log"
    started = time.monotonic()

    result = ProcessSupervisor().run(
        ProcessRequest(
            argv=[
                sys.executable,
                "-c",
                "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)",
            ],
            cwd=tmp_path,
            timeout_seconds=0.05,
            kill_grace_seconds=0.05,
            poll_seconds=0.01,
            stdout_path=log,
        )
    )

    assert result.timed_out is True
    assert result.returncode == 124
    assert time.monotonic() - started < 2


def test_process_supervisor_structures_launch_failure(tmp_path):
    result = ProcessSupervisor().run(
        ProcessRequest(
            argv=["/definitely/missing/factory-command"],
            cwd=tmp_path,
            timeout_seconds=1,
            stdout_path=tmp_path / "missing.log",
        )
    )

    assert result.returncode == 127
    assert result.pid == 0
    assert result.metadata["launch_error"] == "FileNotFoundError"


def test_native_codex_backend_honors_codex_model_for_builtin_fallback(monkeypatch, tmp_path):
    class RecordingSupervisor:
        request = None

        def run(self, request):
            self.request = request
            return ProcessResult(0, False, 0.01, 123)

    supervisor = RecordingSupervisor()
    monkeypatch.setenv("CODEX_MODEL", "gpt-5.6-sol")
    backend = CodexCliBackend(tmp_path, supervisor=supervisor)

    result = backend.execute(
        ModelRequest(
            project_dir=tmp_path,
            step_id=2,
            attempt=1,
            prompt="work",
            timeout_seconds=10,
            hang_timeout_seconds=5,
        )
    )

    assert result.returncode == 0
    assert result.metadata["model"] == "gpt-5.6-sol"
    assert list(supervisor.request.argv[:4]) == [
        "codex", "exec", "--model", "gpt-5.6-sol"
    ]


def test_process_backend_recognizes_unsupported_model_as_permanent(tmp_path):
    log = tmp_path / "model.log"
    log.write_text(
        'The model "gpt-5.6-luna" is not supported.\n', encoding="utf-8"
    )

    assert CodexCliBackend._unsupported_model_error(log) is True


def test_configured_models_fall_back_to_catalog_defaults(monkeypatch, tmp_path):
    configured = RecordingBackend([ExecutionResult.failed("TRANSIENT", returncode=1)])
    default = RecordingBackend([ExecutionResult.succeeded(model="default-model")])
    backends = ModelBackendRegistry()
    backends.register("configured", configured)
    backends.register("codex", default)
    monkeypatch.setattr(
        "factory_core.adapters.models.dispatcher.get_step_model_ids",
        lambda *_args: ("custom", ""),
    )
    monkeypatch.setattr(
        "factory_core.adapters.models.dispatcher.get_model_entry",
        lambda _path, model_id: {
            "backend": "configured",
            "model": model_id,
            "effort": "",
            "base_url": "",
            "key_env": "",
        },
    )
    dispatcher = ModelDispatcher(tmp_path, backends)

    result = dispatcher.execute(
        ModelRequest(
            project_dir=tmp_path,
            step_id=4,
            attempt=1,
            prompt="work",
            timeout_seconds=10,
            hang_timeout_seconds=5,
        ),
        step_key=4,
        defaults=("codex",),
    )

    assert result.returncode == 0
    assert result.metadata["model_id"] == "codex"
    assert len(configured.requests) == 1
    assert len(default.requests) == 1


def test_dispatcher_quarantines_permanently_unsupported_model(monkeypatch, tmp_path):
    unsupported = RecordingBackend(
        [ExecutionResult.failed("PERMANENT_MODEL_UNSUPPORTED", returncode=2)]
    )
    fallback = RecordingBackend(
        [ExecutionResult.succeeded(model="fallback-one"), ExecutionResult.succeeded(model="fallback-two")]
    )
    backends = ModelBackendRegistry()
    backends.register("configured", unsupported)
    backends.register("codex", fallback)
    monkeypatch.setattr(
        "factory_core.adapters.models.dispatcher.get_step_model_ids",
        lambda *_args: ("unsupported", "codex"),
    )
    monkeypatch.setattr(
        "factory_core.adapters.models.dispatcher.get_model_entry",
        lambda _path, model_id: {
            "backend": "configured",
            "model": model_id,
            "effort": "",
            "base_url": "",
            "key_env": "",
        },
    )
    dispatcher = ModelDispatcher(tmp_path, backends)
    request = ModelRequest(
        project_dir=tmp_path,
        step_id=13,
        attempt=1,
        prompt="judge",
        timeout_seconds=10,
        hang_timeout_seconds=5,
    )

    first = dispatcher.execute(request, step_key=13, defaults=("codex",))
    second = dispatcher.execute(request, step_key=13, defaults=("codex",))

    assert first.returncode == second.returncode == 0
    assert len(unsupported.requests) == 1
    assert len(fallback.requests) == 2


def test_api_backend_passes_project_relative_output_paths(tmp_path):
    class RecordingSupervisor:
        request = None

        def run(self, request):
            self.request = request
            return ProcessResult(0, False, 0.01, 123)

    supervisor = RecordingSupervisor()
    project = tmp_path / "project"
    project.mkdir()
    backend = ApiAgentBackend(tmp_path, supervisor=supervisor)

    result = backend.execute(
        ModelRequest(
            project_dir=project,
            step_id=13,
            attempt=1,
            prompt="judge",
            timeout_seconds=100,
            hang_timeout_seconds=50,
            output_file=project / "judge_outputs/math.md",
            effective_prompt_file=project / "judge_outputs/math.rendered_prompt.txt",
        )
    )

    assert result.returncode == 0
    argv = list(supervisor.request.argv)
    assert argv[argv.index("--output-file") + 1] == "judge_outputs/math.md"
    assert argv[argv.index("--effective-prompt-file") + 1] == (
        "judge_outputs/math.rendered_prompt.txt"
    )


def test_native_registry_has_no_legacy_lifecycle():
    registry = build_native_registry(Path(__file__).resolve().parents[1])

    modules = {type(definition.lifecycle).__module__ for definition in registry}
    assert len(list(registry)) == 17
    assert all("legacy" not in module for module in modules)
    assert all(definition.step is not None for definition in registry)


def test_prompt_step_validates_once_in_engine_not_during_model_call(tmp_path):
    class CountingValidator:
        calls = 0

        def validate(self, _context):
            self.calls += 1
            return ValidationResult.valid("artifact")

    class SuccessfulDispatcher:
        @staticmethod
        def execute(_request, **_kwargs):
            return ExecutionResult.succeeded(model_id="codex-test")

    contract = next(item for item in STEP_CONTRACTS if item.id == 4)
    validator = CountingValidator()
    step = PromptStep(
        contract,
        PromptRenderer(Path(__file__).resolve().parents[1]),
        SuccessfulDispatcher(),
        validator,
    )
    context = StepContext(tmp_path, tmp_path.name, 4, 1, 30, 0)

    result = step.execute(context)

    assert result.returncode == 0
    assert validator.calls == 0
    assert step.validate(context).is_valid is True
    assert validator.calls == 1


def test_parallel_proposals_report_exit_zero_missing_artifacts(tmp_path):
    (tmp_path / "viable_streams.md").write_text(
        "## Stream m1: first\n## Stream m2: second\n", encoding="utf-8"
    )

    class EmptyDispatcher:
        @staticmethod
        def execute(_request, **_kwargs):
            return ExecutionResult.succeeded(model_id="empty")

    contract = next(item for item in STEP_CONTRACTS if item.id == 2)
    step = ParallelProposalStep(
        contract,
        PromptRenderer(Path(__file__).resolve().parents[1]),
        EmptyDispatcher(),
        AlwaysValidValidator(),
        max_rounds=1,
    )

    result = step.execute(StepContext(tmp_path, tmp_path.name, 2, 1, 30, 0))

    assert result.error_class == "TRANSIENT_ARTIFACT_MISSING"
    assert result.metadata["failed_streams"] == [1, 2]
    assert result.metadata["missing_artifacts"] == [
        "m1_critique.md",
        "m1_demo_result.json",
        "m1_spec.md",
        "m2_critique.md",
        "m2_demo_result.json",
        "m2_spec.md",
    ]


class AlwaysValidValidator:
    def validate(self, _context):
        return ValidationResult.valid("fake-contract")


class ArtifactWritingBackend:
    def execute(self, request):
        prompt = request.prompt
        project = request.project_dir
        if "2_proposal_" in prompt:
            stream = prompt.split("2_proposal_", 1)[1].splitlines()[0].strip()
            (project / f"m{stream}_spec.md").write_text("spec\n", encoding="utf-8")
            (project / f"m{stream}_demo_result.json").write_text("{}\n", encoding="utf-8")
        if "2_critic_" in prompt:
            stream = prompt.split("2_critic_", 1)[1].splitlines()[0].strip()
            (project / f"m{stream}_critique.md").write_text(
                "VERDICT: VALIDATED\n", encoding="utf-8"
            )
        if request.output_file is not None:
            role = request.output_file.stem
            request.output_file.parent.mkdir(parents=True, exist_ok=True)
            request.output_file.write_text(
                f"VERDICT: PASS\n{{\"role\": \"{role}\"}}\n", encoding="utf-8"
            )
        return ExecutionResult.succeeded(model="fake-model")


class FakeCommandRunner:
    def _ok(self, project, label):
        log = project / "logs" / f"{label}.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("ok\n", encoding="utf-8")
        return CommandResult(0, True, log)

    def python(self, factory_root, project, script, args, *, label, **_kwargs):
        project = Path(project)
        if script.endswith("build_objective_evidence.py"):
            output = project / "judge_packets/objective_evidence.json"
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text("{}\n", encoding="utf-8")
        elif script.endswith("judge_packet.py"):
            for role in ("math", "execution", "paper"):
                packet = project / "judge_packets" / role
                packet.mkdir(parents=True, exist_ok=True)
                (packet / "context.txt").write_text("context\n", encoding="utf-8")
                (packet / "manifest.json").write_text("{}\n", encoding="utf-8")
        elif script.endswith("aggregate_judges.py"):
            (project / "judge_evaluation.md").write_text("VERDICT: PASS\n", encoding="utf-8")
            output = project / "judge_outputs/aggregate.json"
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text('{"verdict":"PASS"}\n', encoding="utf-8")
        elif script.endswith("pdf_visual_gate.py"):
            output = project / "judge_outputs/visual_gate.json"
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text('{"status":"PASS"}\n', encoding="utf-8")
        elif script.endswith("judge_decision_router.py"):
            output = project / "judge_outputs/decision_route.json"
            output.write_text('{"effective_decision":"PASS"}\n', encoding="utf-8")
        elif script.endswith("package_submission.py"):
            output = Path(args[-1])
            output.parent.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(output, "w") as archive:
                archive.writestr("fixture.txt", "ok")
        return self._ok(project, label)

    def run(self, project, argv, *, label, **_kwargs):
        project = Path(project)
        if str(argv[0]).endswith("compile_paper.sh"):
            (project / f"{project.name}_paper.pdf").write_bytes(b"%PDF-1.4\nfixture")
        return self._ok(project, label)


def test_native_judge_stages_codex_final_response_and_marks_review_phase(tmp_path):
    root = Path(__file__).resolve().parents[1]
    project = tmp_path / "judge_fixture"
    project.mkdir()

    class FinalResponseWritingDispatcher:
        requests = []

        def execute(self, request, **_kwargs):
            self.requests.append(request)
            assert not request.output_file.with_suffix(
                request.output_file.suffix + ".llm-result.json"
            ).exists()
            assert not request.output_file.with_name("paper.grounding.json").exists()
            request.output_file.parent.mkdir(parents=True, exist_ok=True)
            if len(self.requests) == 1:
                request.output_file.write_text("VERDICT: PASS\n{}\n", encoding="utf-8")
                request.final_response_file.write_text("judge summary\n", encoding="utf-8")
            else:
                request.final_response_file.write_text(
                    "VERDICT: PASS\n{}\n", encoding="utf-8"
                )
            return ExecutionResult.succeeded(
                model_id="codex-test", backend="codex", model="test-model"
            )

    dispatcher = FinalResponseWritingDispatcher()
    contract = next(item for item in STEP_CONTRACTS if item.id == 13)
    step = JudgeStep(
        contract,
        root,
        PromptRenderer(root),
        dispatcher,
        AlwaysValidValidator(),
        FakeCommandRunner(),
    )

    provisional = StepContext(project, project.name, 13, 1, 3600, 0)
    stale_output = project / "judge_outputs/paper.md"
    stale_output.parent.mkdir(parents=True, exist_ok=True)
    stale_output.write_text("VERDICT: PASS\nold\n", encoding="utf-8")
    stale_output.with_suffix(".md.llm-result.json").write_text("{}\n", encoding="utf-8")
    (project / "judge_outputs/paper.grounding.json").write_text("{}\n", encoding="utf-8")
    result = step._run_role(provisional, "paper", "judges/paper_reviewer.txt")

    assert result.returncode == 0
    request = dispatcher.requests[-1]
    assert request.output_file != request.final_response_file
    assert request.output_file.read_text(encoding="utf-8").startswith("VERDICT: PASS\n")
    assert "REVIEW_PHASE: PROVISIONAL_STEP_13" in request.prompt
    assert "expected at this phase" in request.prompt
    assert "only permitted inputs are exactly judge_packets/paper/context.txt" in request.prompt
    assert "Never omit the paper/ directory" in request.prompt

    final = StepContext(project, project.name, 16, 1, 3600, 0)
    result = step._run_role(final, "paper", "judges/paper_reviewer.txt")

    assert result.returncode == 0
    assert "REVIEW_PHASE: FINAL_SUBMISSION" in dispatcher.requests[-1].prompt
    assert "blocking delivery defect" in dispatcher.requests[-1].prompt
    assert (project / "judge_outputs/paper.md").read_text(
        encoding="utf-8"
    ).startswith("VERDICT: PASS\n")


def test_native_judge_failure_continues_only_with_delivery_override(tmp_path):
    root = Path(__file__).resolve().parents[1]
    project = tmp_path / "judge_fixture"
    project.mkdir()

    class FailingDispatcher:
        calls = 0

        def execute(self, _request, **_kwargs):
            self.calls += 1
            return ExecutionResult.failed("TRANSIENT_MODEL_BACKEND", returncode=2)

    contract = next(item for item in STEP_CONTRACTS if item.id == 13)
    step = JudgeStep(
        contract,
        root,
        PromptRenderer(root),
        FailingDispatcher(),
        AlwaysValidValidator(),
        FakeCommandRunner(),
    )
    context = StepContext(project, project.name, 13, 1, 3600, 0)

    strict_result = step.execute(context)

    assert strict_result.returncode == 2
    assert strict_result.error_class == "PERMANENT_JUDGE_INFRASTRUCTURE"
    assert strict_result.metadata["exhausted_error_class"] == "TRANSIENT_JUDGE_ROLE"
    assert strict_result.metadata["role_attempts"] == 2
    assert step.dispatcher.calls == 2

    (project / "gate2_delivery_override.json").write_text(
        json.dumps(
            {
                "enabled": True,
                "scope": "continue_to_step16",
                "reason": "user requested continuation",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    override_result = step.execute(context)

    assert override_result.returncode == 0
    assert override_result.metadata == {
        "judge_completed": False,
        "judge_verdict": "",
        "judge_failure_stage": "precheck:math",
        "judge_error_class": "PERMANENT_JUDGE_INFRASTRUCTURE",
        "judge_returncode": 2,
        "gate2_delivery_override": True,
    }
    assert step.dispatcher.calls == 4
    log = (project / "logs/gate2_continuation_override.log").read_text(
        encoding="utf-8"
    )
    assert "stage=precheck:math" in log
    assert "quality PASS not fabricated" in log


def test_native_step13_runs_math_precheck_without_full_role_aggregate(tmp_path):
    root = Path(__file__).resolve().parents[1]
    project = tmp_path / "judge_fixture"
    project.mkdir()

    class RecordingJudgeDispatcher:
        roles = []

        def execute(self, request, **_kwargs):
            role = request.output_file.stem
            self.roles.append(role)
            request.output_file.parent.mkdir(parents=True, exist_ok=True)
            request.output_file.write_text("VERDICT: PASS\n{}\n", encoding="utf-8")
            return ExecutionResult.succeeded(
                model_id="judge-test", backend="codex", model="judge-model"
            )

    class RecordingAggregateRunner(FakeCommandRunner):
        aggregate_calls = 0

        def python(self, factory_root, project, script, args, *, label, **kwargs):
            if script.endswith("aggregate_judges.py"):
                self.aggregate_calls += 1
            return super().python(
                factory_root, project, script, args, label=label, **kwargs
            )

    dispatcher = RecordingJudgeDispatcher()
    runner = RecordingAggregateRunner()
    contract = next(item for item in STEP_CONTRACTS if item.id == 13)
    step = JudgeStep(
        contract,
        root,
        PromptRenderer(root),
        dispatcher,
        NativeArtifactValidator(root, 13),
        runner,
    )

    result = step.execute(StepContext(project, project.name, 13, 1, 3600, 0))

    assert result.returncode == 0
    assert result.metadata["judge_verdict"] == "PRECHECK_PASS"
    assert result.metadata["reviewed_roles"] == ["math"]
    assert dispatcher.roles == ["math"]
    assert runner.aggregate_calls == 0
    precheck = json.loads(
        (project / "judge_outputs/precheck.json").read_text(encoding="utf-8")
    )
    assert precheck["review_mode"] == "math_only"
    assert precheck["delivery_allowed"] is False


def test_native_delivery_override_packages_after_final_judge_failure(tmp_path):
    root = tmp_path / "factory"
    project = root / "ongoing" / "judge_fixture"
    project.mkdir(parents=True)
    (project / "judge_evaluation.md").write_text(
        "VERDICT: REOPEN_REVISION_MODEL\n", encoding="utf-8"
    )
    (project / "gate2_delivery_override.json").write_text(
        json.dumps(
            {
                "enabled": True,
                "scope": "continue_to_step16",
                "reason": "user requested continuation",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    class FailedJudge:
        @staticmethod
        def _delivery_override_active(_project):
            return True

        @staticmethod
        def execute(_context):
            return ExecutionResult.succeeded(
                judge_completed=False,
                judge_verdict="REOPEN_REVISION_MODEL",
                judge_failure_stage="role:paper",
                judge_error_class="TRANSIENT_JUDGE_ROLE",
                gate2_delivery_override=True,
            )

    contract = next(item for item in STEP_CONTRACTS if item.id == 16)
    step = DeliveryStep(
        contract,
        root,
        FailedJudge(),
        AlwaysValidValidator(),
        FakeCommandRunner(),
        fingerprinter=lambda _project, _base: "a" * 64,
    )

    result = step.execute(StepContext(project, project.name, 16, 1, 3600, 0))

    assert result.returncode == 0
    assert result.metadata["final_decision"] == "REOPEN_REVISION_MODEL"
    assert result.metadata["gate2_delivery_override"] is True
    assert (root / "papers/judge_fixture_submission.zip").is_file()
    route = json.loads(
        (project / "judge_outputs/decision_route.json").read_text(encoding="utf-8")
    )
    assert route["effective_decision"] == "CONTINUE_TO_STEP16"
    assert route["quality_pass_fabricated"] is False
    audit = json.loads(
        (project / ".factory/audits/latest.json").read_text(encoding="utf-8")
    )
    assert audit["status"] == "OVERRIDDEN"
    assert audit["snapshot_id"] == "a" * 64
    assert not (project / "judge_outputs/judgment_receipt.json").exists()


def test_native_delivery_override_prefers_new_shadow_decision(tmp_path):
    route = tmp_path / "judge_outputs/decision_route.json"
    route.parent.mkdir(parents=True)
    route.write_text(
        json.dumps(
            {
                "policy_mode": "shadow",
                "new_decision": "REOPEN_REVISION_TEXT",
                "effective_decision": "PASS",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    assert DeliveryStep._decision(tmp_path) == "PASS"
    assert DeliveryStep._decision(tmp_path, prefer_new=True) == "REOPEN_REVISION_TEXT"


def test_native_validator_retries_judge_for_indeterminate_gate2(tmp_path):
    project = tmp_path / "judge_fixture"
    project.mkdir()
    (project / "judge_evaluation.md").write_text(
        "VERDICT: INDETERMINATE_REVIEW\nmath: INDETERMINATE\n",
        encoding="utf-8",
    )
    validator = NativeArtifactValidator(Path(__file__).resolve().parents[1], 13)

    valid, reason, _evidence, metadata = validator._step_13(project)

    assert valid is False
    assert reason == "Gate 2 evidence is indeterminate"
    assert "resume_after_step" not in metadata
    assert metadata == {
        "error_class": "TRANSIENT_JUDGE_INFRASTRUCTURE",
        "normalized_verdict": "INFRA_RETRY",
        "retry_scope": "step_13",
    }


def test_native_validator_reopens_true_math_failure_to_model_owner(tmp_path):
    project = tmp_path / "judge_fixture"
    project.mkdir()
    (project / "judge_evaluation.md").write_text(
        "VERDICT: REOPEN_REVISION_MODEL\nCorrectness vetoes: math\n",
        encoding="utf-8",
    )
    validator = NativeArtifactValidator(Path(__file__).resolve().parents[1], 13)

    valid, _reason, _evidence, metadata = validator._step_13(project)

    assert valid is False
    assert metadata["resume_after_step"] == 3


def test_native_validator_routes_missing_packet_source_to_its_owner(tmp_path):
    project = tmp_path / "judge_fixture"
    project.mkdir()
    (project / "judge_evaluation.md").write_text(
        "VERDICT: INDETERMINATE_REVIEW\n", encoding="utf-8"
    )
    packet = project / "judge_packets/execution"
    packet.mkdir(parents=True)
    (packet / "manifest.json").write_text(
        json.dumps(
            {
                "completeness": {
                    "status": "INCOMPLETE",
                    "requirements": [
                        {
                            "id": "canonical",
                            "paths": ["results/canonical_results.json"],
                            "satisfied": False,
                        }
                    ],
                }
            }
        ),
        encoding="utf-8",
    )
    validator = NativeArtifactValidator(Path(__file__).resolve().parents[1], 13)

    valid, reason, _evidence, metadata = validator._step_13(project)

    assert valid is False
    assert reason == "Gate 2 packet is missing upstream artifacts"
    assert metadata["resume_after_step"] == 4
    assert metadata["missing_artifacts"] == ["results/canonical_results.json"]


def test_native_validator_routes_empty_claim_requirement_to_step4(tmp_path):
    project = tmp_path / "judge_fixture"
    project.mkdir()
    (project / "judge_evaluation.md").write_text(
        "VERDICT: INDETERMINATE_REVIEW\n", encoding="utf-8"
    )
    packet = project / "judge_packets/math"
    packet.mkdir(parents=True)
    (packet / "manifest.json").write_text(
        json.dumps(
            {
                "completeness": {
                    "status": "INCOMPLETE",
                    "requirements": [
                        {
                            "id": "question:Q2:registered_claim",
                            "paths": [],
                            "satisfied": False,
                        }
                    ],
                }
            }
        ),
        encoding="utf-8",
    )
    validator = NativeArtifactValidator(Path(__file__).resolve().parents[1], 13)

    valid, _reason, _evidence, metadata = validator._step_13(project)

    assert valid is False
    assert metadata["resume_after_step"] == 3
    assert metadata["missing_artifacts"] == ["claim_registry.json"]


def test_native_step10_reports_the_exact_failed_incremental_check(monkeypatch, tmp_path):
    (tmp_path / "code_review.md").write_text(
        "\n".join(["review"] * 20) + "\n", encoding="utf-8"
    )
    (tmp_path / f"{tmp_path.name}_paper.tex").write_text(
        "\\begin{document}\nfixture\n\\end{document}\n", encoding="utf-8"
    )

    def fake_python(
        _self,
        _root,
        _project,
        script,
        _args,
        *,
        log_path,
        **_kwargs,
    ):
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("report\n", encoding="utf-8")
        failed = script == "scripts/verify_numbers.py"
        return CommandResult(7 if failed else 0, not failed, log_path)

    monkeypatch.setattr(
        "factory_core.audit.incremental.CommandRunner.python", fake_python
    )
    validator = NativeArtifactValidator(Path(__file__).resolve().parents[1], 10)

    valid, reason, evidence, metadata = validator._step_10(tmp_path)

    assert valid is False
    assert reason == "paper audit FAIL: PAPER_AUDIT_FAILED"
    assert "number_verification.latest.stdout" in evidence
    assert metadata["error_class"] == "AUDIT_REPAIR_REQUIRED"
    assert metadata["failed_check"] == "numbers"
    assert metadata["returncode"] == 1
    assert metadata["repair_step"] == 9


def test_fake_backends_drive_native_steps_zero_through_sixteen(tmp_path):
    root = Path(__file__).resolve().parents[1]
    project = tmp_path / "ongoing" / "native_fixture"
    project.mkdir(parents=True)
    (project / "viable_streams.md").write_text(
        "## Stream m1: first\n## Stream m2: second\n", encoding="utf-8"
    )
    backends = ModelBackendRegistry()
    backend = ArtifactWritingBackend()
    for name in ("codex", "claude", "openai"):
        backends.register(name, backend)
    dispatcher = ModelDispatcher(root, backends)
    registry = build_native_registry(
        root,
        dispatcher=dispatcher,
        runner=FakeCommandRunner(),
        validator_factory=lambda _root, _step: AlwaysValidValidator(),
        fingerprinter=lambda _project, _base: "0" * 64,
    )
    store = SQLiteStateStore(project)
    store.initialize(project_id=project.name, project_type="modeling")

    state = FactoryEngine(project, store=store, registry=registry, sleeper=lambda _: None).run()

    assert state.status is WorkflowStatus.COMPLETED
    assert state.last_completed_step == 16
    assert [event.type for event in store.events()].count("STEP_SUCCEEDED") == 17
    assert not any("legacy_runner" in json.dumps(event.payload) for event in store.events())
