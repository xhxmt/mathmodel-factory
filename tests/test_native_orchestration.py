import json
import signal
import sys
import time
import zipfile
from pathlib import Path

from factory_core.adapters.infrastructure.commands import CommandResult
from factory_core.adapters.infrastructure.process import ProcessRequest, ProcessSupervisor
from factory_core.adapters.models.backends import ModelRequest
from factory_core.adapters.models.dispatcher import ModelDispatcher
from factory_core.domain import ExecutionResult, ValidationResult, WorkflowStatus
from factory_core.engine import FactoryEngine
from factory_core.registry import ModelBackendRegistry
from factory_core.steps import STEP_CONTRACTS, build_native_registry, catalog_payload
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


def test_native_registry_has_no_legacy_lifecycle():
    registry = build_native_registry(Path(__file__).resolve().parents[1])

    modules = {type(definition.lifecycle).__module__ for definition in registry}
    assert len(list(registry)) == 17
    assert all("legacy" not in module for module in modules)
    assert all(definition.step is not None for definition in registry)


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
