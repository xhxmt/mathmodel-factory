"""Declared inputs, initial I/O snapshots and fresh intermediate reads."""
import hashlib
import json

import pytest

from scripts.solver_job_receipt import build_evidence, receipt_paths
from tests.support.normal_run import controlled_service
from tests.test_normal_run_packet_chain import write


def run_producer(tmp_path, body, *, initial=None, inputs=()):
    service, project, backend = controlled_service(tmp_path)
    (project / "results").mkdir(exist_ok=True)
    if initial is not None:
        write(project, "results/value.json", initial)
    script = write(project, "models/producer.py", "from pathlib import Path\nimport json\n"
        "output = Path('../results/value.json')\n" + body)
    job = service.submit_solver(project, runtime="python", script=script, max_time_seconds=10,
        input_paths=inputs, output_paths=("results/value.json",))
    job = service.solver_status(project, job["job_id"])
    submitted, completed = receipt_paths(project / ".factory/solver_receipts", job["job_id"])
    evidence = build_evidence(project, submitted, completed)
    closure = json.loads((project / job["result_refs"]["input_closure"]).read_text())
    return project, job, evidence, closure, backend


@pytest.mark.parametrize("body", [
    "value = json.loads(output.read_text())\noutput.write_text(json.dumps(value + 1))\n",
    "with output.open('a') as handle:\n    handle.write('3')\n",
])
def test_preexisting_read_or_append_requires_explicit_input(tmp_path, body):
    _, job, evidence, closure, backend = run_producer(tmp_path, body, initial="2")
    assert job["status"] == "failed"
    assert evidence["receipt_ready"] is False
    assert closure["undeclared_inputs"] == ["results/value.json"]
    assert "UNDECLARED_INITIAL_OUTPUT_INPUT" in backend.result.stderr.decode()
    assert evidence["completion"]["input_closure_valid"] is False


def test_declared_read_rewrite_binds_initial_snapshot_and_final_output(tmp_path):
    project, job, evidence, closure, _ = run_producer(tmp_path,
        "value = json.loads(output.read_text())\noutput.write_text(json.dumps(value + 1))\n",
        initial="2", inputs=("results/value.json",))
    assert job["status"] == "completed"
    assert evidence["receipt_ready"] is True
    assert evidence["completion"]["input_closure_valid"] is True
    assert closure["input_output_paths"] == ["results/value.json"]
    assert "results/value.json" in closure["observed_inputs"]
    snapshot = evidence["submission"]["input_output_snapshots"][0]["snapshot"]
    assert (project / snapshot["path"]).read_text() == "2"
    assert snapshot["sha256"] == hashlib.sha256(b"2").hexdigest()
    assert evidence["completion"]["initial_inputs"] == [snapshot]
    assert evidence["completion"]["outputs"][0]["sha256"] == hashlib.sha256(b"3").hexdigest()
    assert evidence["completion"]["inputs_unchanged"]
    from factory_core.finalization import build_final_input_manifest
    from tests.test_normal_run_numeric_bindings import approve_content
    write(project, f"{project.name}_paper.tex", "Controlled result.")
    approve_content(project)
    final = build_final_input_manifest(project)
    assert snapshot["path"] in {item["path"] for item in final.manifest["files"]}


@pytest.mark.parametrize("initial", [None, "old result"])
@pytest.mark.parametrize("write_mode", ["direct", "atomic"])
def test_current_job_output_readback_is_a_fresh_intermediate(tmp_path, initial, write_mode):
    body = "output.write_text('3')\n"
    if write_mode == "atomic":
        body = "temporary = output.with_suffix('.tmp')\ntemporary.write_text('3')\ntemporary.replace(output)\n"
    body += "value = int(output.read_text())\noutput.write_text(str(value + 1))\n"
    _, job, evidence, closure, backend = run_producer(tmp_path, body, initial=initial)
    assert job["status"] == "completed", backend.result.stderr
    assert evidence["receipt_ready"]
    assert closure["generated_output_reads"] == ["results/value.json"]
    assert "results/value.json" not in closure["observed_inputs"]
    assert evidence["completion"]["initial_inputs"] == []


@pytest.mark.parametrize("changed", ["snapshot", "closure"])
def test_initial_input_or_closure_evidence_change_invalidates_completion(tmp_path, changed):
    project, job, evidence, _, _ = run_producer(tmp_path,
        "value = int(output.read_text())\noutput.write_text(str(value + 1))\n",
        initial="2", inputs=("results/value.json",))
    assert evidence["receipt_ready"]
    path = (evidence["submission"]["input_output_snapshots"][0]["snapshot"]["path"]
            if changed == "snapshot" else job["result_refs"]["input_closure"])
    (project / path).write_text("changed evidence")
    submitted, completed = receipt_paths(project / ".factory/solver_receipts", job["job_id"])
    assert build_evidence(project, submitted, completed)["receipt_ready"] is False


def test_submission_to_execution_initial_input_race_is_explicit_failure(tmp_path, monkeypatch):
    service, project, backend = controlled_service(tmp_path)
    write(project, "results/value.json", "2")
    script = write(project, "models/producer.py", "from pathlib import Path\n"
        "value = int(Path('../results/value.json').read_text())\n"
        "Path('../results/value.json').write_text(str(value + 1))\n")
    original = backend.submit

    def changed_before_start(request):
        (project / "results/value.json").write_text("7")
        return original(request)

    monkeypatch.setattr(backend, "submit", changed_before_start)
    job = service.submit_solver(project, runtime="python", script=script, max_time_seconds=10,
        input_paths=("results/value.json",), output_paths=("results/value.json",))
    job = service.solver_status(project, job["job_id"])
    assert job["status"] == "failed"
    closure = json.loads((project / job["result_refs"]["input_closure"]).read_text())
    assert closure["preflight_errors"] == ["INITIAL_INPUT_OR_OUTPUT_CHANGED_BEFORE_EXECUTION"]
    assert (project / "results/value.json").read_text() == "7"


def test_numpy_producer_uses_normal_serialization_and_receipt_chain(tmp_path):
    _, job, evidence, _, backend = run_producer(tmp_path,
        "import numpy as np\nfrom factory_core.json_values import dumps\n"
        "output.write_text(dumps({'value': np.int64(2), 'valid': np.bool_(True), "
        "'array': np.array([1.0, 2.0])}))\n")
    assert job["status"] == "completed", backend.result.stderr
    assert evidence["receipt_ready"]
