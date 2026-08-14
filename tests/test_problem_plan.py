import json
from pathlib import Path

import pytest

from factory_core.problem_plan import (
    ProblemPlanError,
    load_problem_plan,
    problem_plan_fingerprint,
    validate_problem_plan,
)
from factory_core.steps.validators import NativeArtifactValidator


def _plan() -> dict:
    return {
        "schema_version": "problem-plan-v1",
        "title": "测试题任务图",
        "problem_identity": "demo",
        "nodes": [
            {
                "id": "estimate",
                "title": "参数估计",
                "objective": "估计优化模型需要的参数",
                "phase": "data",
                "subproblem": "问题1",
                "source_refs": ["problem/problem_brief.md"],
                "inputs": ["原始数据"],
                "outputs": ["参数"],
                "method_candidates": ["method_library/statistics/bayesian_inference.md"],
            },
            {
                "id": "optimize",
                "title": "优化决策",
                "objective": "使用估计参数求最优决策",
                "phase": "solve",
                "subproblem": "问题2",
                "source_refs": ["problem/problem_brief.md"],
                "inputs": ["参数"],
                "outputs": ["最优决策"],
                "method_candidates": ["method_library/optimization/milp.md"],
            },
        ],
        "edges": [
            {
                "from": "estimate",
                "to": "optimize",
                "type": "provides_parameters",
                "rationale": "优化目标依赖估计参数",
            }
        ],
    }


def test_problem_plan_normalizes_and_orders_dag(tmp_path: Path):
    project = tmp_path / "demo"
    path = project / "problem" / "problem_plan.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(_plan(), ensure_ascii=False), encoding="utf-8")
    (project / "problem" / "problem_brief.md").write_text("# brief\n", encoding="utf-8")

    plan = load_problem_plan(project)

    assert plan["topological_order"] == ["estimate", "optimize"]
    assert len(problem_plan_fingerprint(plan)) == 64


def test_problem_plan_rejects_cycles():
    data = _plan()
    data["nodes"][1]["phase"] = "data"
    data["edges"].append(
        {
            "from": "optimize",
            "to": "estimate",
            "type": "depends_on",
            "rationale": "invalid",
        }
    )

    with pytest.raises(ProblemPlanError, match="acyclic"):
        validate_problem_plan(data)


def test_problem_plan_rejects_uncontained_source_reference():
    data = _plan()
    data["nodes"][0]["source_refs"] = ["../secret.txt"]

    with pytest.raises(ProblemPlanError, match="contained project-relative"):
        validate_problem_plan(data)


def test_native_step_zero_requires_valid_plan_and_registered_methods(tmp_path: Path):
    project = tmp_path / "demo"
    problem = project / "problem"
    problem.mkdir(parents=True)
    for name in (
        "problem_brief.md",
        "terminology_table.md",
        "data_inventory.md",
        "feasibility_constraints.md",
    ):
        (problem / name).write_text("# test\n", encoding="utf-8")
    (problem / "candidate_methods.md").write_text(
        "method_library/optimization/milp.md\n", encoding="utf-8"
    )
    (problem / "deliverables.json").write_text(
        '{"attachments":[],"strategy_tables":[]}\n', encoding="utf-8"
    )
    plan = _plan()
    plan["nodes"][0]["method_candidates"] = ["method_library/optimization/milp.md"]
    (problem / "problem_plan.json").write_text(
        json.dumps(plan, ensure_ascii=False), encoding="utf-8"
    )

    root = Path(__file__).resolve().parents[1]
    valid, _, evidence, _ = NativeArtifactValidator(root, 0)._step_0(project)

    assert valid is True
    assert "problem/problem_plan.json" in evidence
