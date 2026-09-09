import json
from pathlib import Path

import pytest

from factory_core.domain import ExecutionResult, StepContext
from factory_core.steps import STEP_CONTRACTS
from factory_core.steps.prompting import PromptRenderer
from factory_core.steps.specialized import JudgeStep
from factory_core.judge_batch import JudgeBatchError, verify
from tests.test_native_orchestration import FakeCommandRunner, AlwaysValidValidator


def fixture(tmp_path):
    root = Path(__file__).resolve().parents[1]
    runner = FakeCommandRunner()
    runner.python(root, tmp_path, "scripts/build_objective_evidence.py", [], label="objective")
    runner.python(root, tmp_path, "scripts/judge_packet.py", [], label="packets")

    class Dispatcher:
        calls = 0
        exit_code = 0
        mutate = False
        def execute(self, request, **kwargs):
            self.calls += 1
            request.output_file.write_text("VERDICT: PASS\n{}\n")
            if self.mutate:
                (tmp_path / "judge_packets/math/context.txt").write_text("changed")
            return ExecutionResult(self.exit_code, "" if not self.exit_code else "TRANSIENT_MODEL_BACKEND",
                                   {"model_id": "mock", "backend": "codex", "model": "mock"})

    dispatcher = Dispatcher()
    step = JudgeStep(next(c for c in STEP_CONTRACTS if c.id == 13), root,
                     PromptRenderer(root), dispatcher, AlwaysValidValidator(), runner)
    def run(*, role="math", step_id=16, **kw):
        context = StepContext(tmp_path, tmp_path.name, step_id, 1, 60, 0)
        template = "paper_reviewer" if role == "paper" else role + "_auditor"
        return step._run_role(context, role, f"judges/{template}.txt", **kw)
    return run, dispatcher


def test_exact_committed_call_reuses_verified_result(tmp_path):
    run, dispatcher = fixture(tmp_path)
    first = run()
    second = run()
    assert first.returncode == second.returncode == 0
    assert second.metadata["reused"] is True
    assert first.metadata["call_id"] == second.metadata["call_id"]
    assert dispatcher.calls == 1


@pytest.mark.parametrize("fault", ["response", "receipt", "seal", "partial"])
def test_corrupt_or_partial_call_never_reused(tmp_path, fault):
    run, dispatcher = fixture(tmp_path)
    first = run()
    assert first.returncode == 0
    folder = tmp_path / first.metadata["audit_binding"]["archive"]
    if fault == "response":
        (tmp_path / "judge_outputs/math.md").write_text("VERDICT: FAIL\n")
    elif fault == "receipt":
        path = tmp_path / "judge_outputs/math.md.llm-result.json"
        value = json.loads(path.read_text())
        value["actual_model"] = "stale"
        path.write_text(json.dumps(value))
    elif fault == "seal":
        (folder / "committed.json").write_text("{}")
    else:
        (folder / "metadata").unlink()
    assert run().error_class == "PERMANENT_JUDGE_EVIDENCE_BINDING"
    assert dispatcher.calls == 1


def test_changed_packet_and_grounding_retry_start_new_calls(tmp_path):
    run, dispatcher = fixture(tmp_path)
    first = run()
    (tmp_path / "judge_packets/paper/manifest.json").write_text('{"changed":true}')
    with pytest.raises(JudgeBatchError):
        verify(tmp_path, first.metadata["audit_binding"])
    second = run()
    third = run(retry_instructions="exact quote repair")
    assert second.returncode == third.returncode == 0
    assert len({first.metadata["call_id"], second.metadata["call_id"], third.metadata["call_id"]}) == 3
    assert dispatcher.calls == 3


def test_nonzero_exit_leaves_no_committed_evidence(tmp_path):
    run, dispatcher = fixture(tmp_path)
    dispatcher.exit_code = 2
    assert run().returncode == 2
    assert not list((tmp_path / "judge_outputs/batches").rglob("committed.json"))
    dispatcher.exit_code = 0
    assert run().returncode == 0
    assert dispatcher.calls == 2


def test_input_mutation_during_call_is_evidence_error(tmp_path):
    run, dispatcher = fixture(tmp_path)
    dispatcher.mutate = True
    assert run().error_class == "PERMANENT_JUDGE_EVIDENCE_BINDING"
    assert not list((tmp_path / "judge_outputs/batches").rglob("committed.json"))


def test_concurrent_exact_calls_commit_once(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    run, dispatcher = fixture(tmp_path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: run(), range(2)))
    assert all(result.returncode == 0 for result in results)
    assert dispatcher.calls == 1
    assert len({result.metadata["call_id"] for result in results}) == 1


def test_three_native_roles_bind_and_reverify_one_current_batch(tmp_path):
    from scripts.judgment_receipt import bind_configuration_group, _native_batch_group_errors
    run, dispatcher = fixture(tmp_path)
    for role in ("math", "paper", "execution"):
        assert run(role=role).returncode == 0
    assert bind_configuration_group(tmp_path)
    assert _native_batch_group_errors(tmp_path) == []
    for role in ("math", "paper", "execution"):
        assert run(role=role).metadata["reused"] is True
    assert dispatcher.calls == 3


@pytest.mark.parametrize("fault", ["step13", "packet", "unbound"])
def test_three_role_binding_refuses_mixed_execution_or_input_batches(tmp_path, fault):
    from scripts.judgment_receipt import bind_configuration_group, ReceiptError
    run, dispatcher = fixture(tmp_path)
    for role in ("math", "paper", "execution"):
        assert run(role=role, step_id=13 if fault == "step13" and role == "math" else 16).returncode == 0
    if fault == "packet":
        (tmp_path / "judge_packets/math/context.txt").write_text("new packet")
        assert run(role="math").returncode == 0
    elif fault == "unbound":
        path = tmp_path / "judge_outputs/math.md.llm-result.json"
        metadata = json.loads(path.read_text())
        metadata.pop("audit_binding")
        path.write_text(json.dumps(metadata))
    before = {p: p.read_bytes() for p in (tmp_path / "judge_outputs").glob("*.json")}
    with pytest.raises(ReceiptError):
        bind_configuration_group(tmp_path)
    assert all(p.read_bytes() == data for p, data in before.items())


@pytest.mark.parametrize("fault", ["asset", "image_order"])
def test_three_role_group_accepts_distinct_images_and_rejects_tampering(tmp_path, fault):
    from dataclasses import replace
    import subprocess
    import sys
    from scripts import document_evidence_view as doc
    from scripts.judgment_receipt import bind_configuration_group, ReceiptError
    from tests.test_document_evidence import document_project

    run, dispatcher = fixture(tmp_path)
    manifests = document_project(tmp_path, pdf=True)
    execute = dispatcher.execute

    def deliver_images(request, **kwargs):
        result = execute(request, **kwargs)
        return replace(result, metadata={**result.metadata,
            "image_inputs": doc.image_inputs(tmp_path, request.image_files)})

    dispatcher.execute = deliver_images
    for role in ("math", "paper", "execution"):
        assert run(role=role).returncode == 0
    assert [len(doc.image_records(manifests[r])) for r in ("math", "paper", "execution")] == [0, 0, 2]

    # Direct invocation must use this checkout even with an unrelated editable
    # installation in the interpreter and with Python path variables ignored.
    script = Path(__file__).resolve().parents[1] / "scripts/judgment_receipt.py"
    result = subprocess.run([sys.executable, "-I", str(script), "bind-group", str(tmp_path)],
        cwd=tmp_path, text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == bind_configuration_group(tmp_path)

    if fault == "asset":
        image = tmp_path / doc.image_records(manifests["execution"])[0]["path"]
        image.write_bytes(image.read_bytes() + b"changed")
    else:
        path = tmp_path / "judge_outputs/execution.md.llm-result.json"
        metadata = json.loads(path.read_text())
        metadata["image_inputs"].reverse()
        path.write_text(json.dumps(metadata))
    before = {p: p.read_bytes() for p in (tmp_path / "judge_outputs").glob("*.json")}
    with pytest.raises(ReceiptError):
        bind_configuration_group(tmp_path)
    assert all(p.read_bytes() == data for p, data in before.items())
