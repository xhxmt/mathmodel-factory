import json
from pathlib import Path

from scripts.verify_provenance import check_values


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
