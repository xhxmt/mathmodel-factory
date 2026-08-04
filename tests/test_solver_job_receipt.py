import json
import sys
from pathlib import Path

from scripts.solver_job_receipt import (
    bind_event_stream,
    build_completion_receipt,
    build_evidence,
    build_submission_receipt,
    write_receipt,
)


def fixture_paths(tmp_path: Path):
    script = tmp_path / "models" / "solve.py"
    input_path = tmp_path / "data" / "input.json"
    output = tmp_path / "results" / "p1" / "values.json"
    script.parent.mkdir(parents=True)
    input_path.parent.mkdir(parents=True)
    output.parent.mkdir(parents=True)
    script.write_text("print('solve')\n", encoding="utf-8")
    input_path.write_text('{"x": 1}\n', encoding="utf-8")
    return script, input_path, output


def submission(tmp_path: Path):
    script, input_path, output = fixture_paths(tmp_path)
    receipt = build_submission_receipt(
        project_dir=tmp_path,
        job_id="local_python_1",
        backend="local",
        runtime="python",
        script=script,
        workdir=script.parent,
        argv=("--mode", "full"),
        max_time_seconds=600,
        requested_at=100,
        input_paths=(input_path,),
        output_paths=(output,),
        seeds=("42",),
    )
    path = tmp_path / ".factory/solver_receipts/local_python_1.submitted.json"
    write_receipt(path, receipt)
    return script, input_path, output, receipt, path


def test_two_stage_receipt_binds_code_inputs_environment_seed_and_outputs(tmp_path):
    script, input_path, output, submitted, submitted_path = submission(tmp_path)
    output.write_text('{"objective": 9}\n', encoding="utf-8")

    completed = build_completion_receipt(
        project_dir=tmp_path,
        submission_path=submitted_path,
        status="COMPLETED",
        finished_at=200,
        result_refs={},
    )
    completion_path = submitted_path.with_name("local_python_1.completed.json")
    write_receipt(completion_path, completed)
    evidence = build_evidence(tmp_path, submitted_path, completion_path)

    assert submitted["schema"] == "solver-job-submission-receipt-v1"
    assert submitted["script"]["path"] == "models/solve.py"
    assert submitted["inputs"][0]["path"] == "data/input.json"
    assert submitted["environment"]["runtime_executable"] == str(Path(sys.executable).resolve())
    assert submitted["seeds"] == ["42"]
    assert completed["schema"] == "solver-job-completion-receipt-v1"
    assert completed["submission_receipt_sha256"]
    assert completed["outputs"][0]["path"] == "results/p1/values.json"
    assert completed["successful_outputs"] is True
    assert evidence["schema"] == "solver-job-evidence-v2"
    assert evidence["receipt_ready"] is True


def test_completion_fails_closed_when_input_changes_or_output_is_missing(tmp_path):
    _script, input_path, _output, _submitted, submitted_path = submission(tmp_path)
    input_path.write_text('{"x": 2}\n', encoding="utf-8")

    completed = build_completion_receipt(
        project_dir=tmp_path,
        submission_path=submitted_path,
        status="COMPLETED",
        finished_at=200,
        result_refs={},
    )

    assert completed["inputs_unchanged"] is False
    assert completed["outputs_complete"] is False
    assert completed["successful_outputs"] is False


def test_evidence_detects_output_drift_after_completion(tmp_path):
    _script, _input_path, output, _submitted, submitted_path = submission(tmp_path)
    output.write_text('{"objective": 9}\n', encoding="utf-8")
    completion = build_completion_receipt(
        project_dir=tmp_path,
        submission_path=submitted_path,
        status="COMPLETED",
        finished_at=200,
        result_refs={},
    )
    completion_path = submitted_path.with_name("local_python_1.completed.json")
    write_receipt(completion_path, completion)
    output.write_text('{"objective": 10}\n', encoding="utf-8")

    evidence = build_evidence(tmp_path, submitted_path, completion_path)

    assert evidence["receipt_ready"] is False
    assert evidence["current_outputs_match_completion"] is False


def test_native_event_stream_must_bind_both_receipt_hashes(tmp_path):
    _script, _input_path, output, _submitted, submitted_path = submission(tmp_path)
    output.write_text('{"objective": 9}\n', encoding="utf-8")
    completion = build_completion_receipt(
        project_dir=tmp_path,
        submission_path=submitted_path,
        status="COMPLETED",
        finished_at=200,
        result_refs={},
    )
    completion_path = submitted_path.with_name("local_python_1.completed.json")
    write_receipt(completion_path, completion)
    evidence = build_evidence(tmp_path, submitted_path, completion_path)
    events = [
        {
            "type": "SOLVER_JOB_RECEIPT_SUBMITTED",
            "payload": {
                "job_id": evidence["job_id"],
                "stage": "submitted",
                "receipt_sha256": evidence["submission_receipt_sha256"],
                "content_sha256": evidence["submission"]["content_sha256"],
                "request_sha256": evidence["submission"]["request_sha256"],
            },
        },
        {
            "type": "SOLVER_JOB_RECEIPT_COMPLETED",
            "payload": {
                "job_id": evidence["job_id"],
                "stage": "completed",
                "receipt_sha256": "0" * 64,
                "content_sha256": evidence["completion"]["content_sha256"],
                "request_sha256": evidence["completion"]["request_sha256"],
            },
        },
    ]

    bound = bind_event_stream(evidence, events)

    assert bound["event_stream_bound"] is False
    assert bound["receipt_ready"] is False
    assert "COMPLETED_RECEIPT_EVENT_HASH_MISMATCH" in bound["errors"]
