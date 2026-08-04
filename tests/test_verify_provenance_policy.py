import json
from pathlib import Path

from scripts.verify_provenance import check_values
from scripts.solver_job_receipt import (
    build_completion_receipt,
    build_submission_receipt,
    write_receipt,
)


def test_generic_budget_floor_is_advisory_without_project_contract(tmp_path):
    values = tmp_path / "results" / "p1" / "values.json"
    values.parent.mkdir(parents=True)
    values.write_text(
        json.dumps(
            {
                "solver": "differential_evolution",
                "status": "FEASIBLE",
                "n_vars": 12,
                "provenance": {
                    "solver": "differential_evolution",
                    "job_id": "job-1",
                    "repair": False,
                    "budget": {"maxiter": 3, "n_eval": 100},
                },
            }
        ),
        encoding="utf-8",
    )
    meta = tmp_path / "run_state" / "solver_jobs" / "job-1.meta"
    meta.parent.mkdir(parents=True)
    meta.write_text("status=COMPLETED\n", encoding="utf-8")

    findings, count = check_values(str(tmp_path))

    assert count == 1
    assert any(kind == "WARN" and "预算" in message for kind, _, message in findings)
    assert not any(kind == "BUDGET_LIMITED" for kind, _, _ in findings)


def test_cross_project_result_and_unstructured_solver_log_are_hard_failures(tmp_path):
    result_dir = tmp_path / "results" / "problem_constants"
    result_dir.mkdir(parents=True)
    (result_dir / "values.json").write_text(
        json.dumps(
            {
                "project": "different_project",
                "status": "TRACE_ANCHOR_FOR_PAPER_CONSTANTS",
                "provenance": {
                    "solver": "problem_statement_constants",
                    "job_id": "job-constants",
                    "repair": False,
                    "budget": {},
                },
            }
        ),
        encoding="utf-8",
    )
    (result_dir / "jobid.txt").write_text("job-constants\n", encoding="utf-8")
    (result_dir / "solver.log").write_text(
        '{"status":"COMPLETED","summary":{"problem4":{"objective":4.45}}}\n'
        "--- stderr ---\nold run\n",
        encoding="utf-8",
    )
    meta = tmp_path / "run_state" / "solver_jobs" / "job-constants.meta"
    meta.parent.mkdir(parents=True)
    meta.write_text("status=COMPLETED\n", encoding="utf-8")

    findings, count = check_values(str(tmp_path))

    assert count == 1
    messages = [message for kind, _, message in findings if kind == "HARD_FAIL"]
    assert any("跨项目" in message for message in messages)
    assert any("solver.log" in message and "结构化" in message for message in messages)


def test_canonical_results_must_match_selected_method_and_source(tmp_path):
    (tmp_path / "chosen_method.md").write_text(
        "PRIMARY: m1 family=test\n", encoding="utf-8"
    )
    values = tmp_path / "results/problem1/values.json"
    values.parent.mkdir(parents=True)
    values.write_text(
        json.dumps(
            {
                "project": tmp_path.name,
                "primary_method": "m1",
                "solver": "solver-a",
                "status": "FEASIBLE",
                "provenance": {
                    "solver": "solver-a",
                    "repair": False,
                    "budget": {},
                },
            }
        ),
        encoding="utf-8",
    )
    canonical = tmp_path / "results/canonical_results.json"
    canonical.write_text(
        json.dumps(
            {
                "project": tmp_path.name,
                "primary_method": "m3",
                "p1": {
                    "primary_method": "m3",
                    "source": "results/problem1/values.json",
                    "status": "FEASIBLE",
                    "objective": 1.0,
                },
            }
        ),
        encoding="utf-8",
    )

    findings, _count = check_values(str(tmp_path))

    hard_messages = [message for kind, _, message in findings if kind == "HARD_FAIL"]
    assert any("chosen_method" in message and "m1" in message and "m3" in message for message in hard_messages)
    assert any("source primary_method=m1" in message for message in hard_messages)


def test_current_canonical_results_require_explicit_source_path(tmp_path):
    values = tmp_path / "results/problem1/values.json"
    values.parent.mkdir(parents=True)
    values.write_text(
        json.dumps(
            {
                "project": tmp_path.name,
                "primary_method": "m1",
                "solver": "solver-a",
                "status": "FEASIBLE",
                "provenance": {
                    "solver": "solver-a",
                    "repair": False,
                    "budget": {},
                },
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "results/canonical_results.json").write_text(
        json.dumps(
            {
                "project": tmp_path.name,
                "primary_method": "m1",
                "p1": {"status": "FEASIBLE", "objective": 1.0},
            }
        ),
        encoding="utf-8",
    )

    findings, _count = check_values(str(tmp_path))

    assert any(
        kind == "HARD_FAIL" and "source/source_file" in message
        for kind, _, message in findings
    )


def _v4_contract(project: Path) -> None:
    (project / "quality_contract.json").write_text(
        json.dumps(
            {
                "version": 4,
                "claims": [],
                "anomaly_checks": [],
                "competitiveness_checks": [],
                "derived_artifacts": {"manifest": "results/derived_artifacts.json"},
            }
        ),
        encoding="utf-8",
    )


def _receipt_bound_values(project: Path, job_id: str = "local_python_v4") -> Path:
    script = project / "models/solve.py"
    values = project / "results/p1/values.json"
    script.parent.mkdir(parents=True)
    values.parent.mkdir(parents=True)
    script.write_text("print('solve')\n", encoding="utf-8")
    receipt_dir = project / ".factory/solver_receipts"
    submitted_path = receipt_dir / f"{job_id}.submitted.json"
    completed_path = receipt_dir / f"{job_id}.completed.json"
    submitted = build_submission_receipt(
        project_dir=project,
        job_id=job_id,
        backend="local",
        runtime="python",
        script=script,
        workdir=script.parent,
        argv=(),
        max_time_seconds=10,
        requested_at=100,
        output_paths=(values,),
        seeds=(42,),
    )
    write_receipt(submitted_path, submitted)
    values.write_text(
        json.dumps(
            {
                "project": project.name,
                "solver": "python",
                "status": "FEASIBLE",
                "provenance": {
                    "solver": "python",
                    "job_id": job_id,
                    "repair": False,
                    "budget": {},
                },
            }
        ),
        encoding="utf-8",
    )
    completed = build_completion_receipt(
        project_dir=project,
        submission_path=submitted_path,
        status="COMPLETED",
        finished_at=200,
        result_refs={},
    )
    write_receipt(completed_path, completed)
    return values


def test_v4_provenance_requires_two_stage_receipt(tmp_path):
    values = tmp_path / "results/p1/values.json"
    values.parent.mkdir(parents=True)
    values.write_text(
        json.dumps(
            {
                "solver": "python",
                "status": "FEASIBLE",
                "provenance": {
                    "solver": "python",
                    "job_id": "old-job",
                    "repair": False,
                    "budget": {},
                },
            }
        ),
        encoding="utf-8",
    )
    _v4_contract(tmp_path)

    findings, _count = check_values(str(tmp_path))

    assert any(
        kind == "HARD_FAIL" and "两阶段 receipt" in message
        for kind, _, message in findings
    )


def test_v4_provenance_accepts_receipt_bound_current_values(tmp_path):
    _v4_contract(tmp_path)
    _receipt_bound_values(tmp_path)

    findings, _count = check_values(str(tmp_path))

    assert not any(kind in {"HARD_FAIL", "REPAIR_FALLBACK"} for kind, _, _ in findings)


def test_v4_provenance_rejects_output_changed_after_completion(tmp_path):
    _v4_contract(tmp_path)
    values = _receipt_bound_values(tmp_path)
    payload = json.loads(values.read_text(encoding="utf-8"))
    payload["objective"] = 999
    values.write_text(json.dumps(payload), encoding="utf-8")

    findings, _count = check_values(str(tmp_path))

    assert any(
        kind == "HARD_FAIL" and "receipt_ready=false" in message
        for kind, _, message in findings
    )
