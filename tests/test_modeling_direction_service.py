from pathlib import Path

from web.backend.modeling_direction_service import (
    build_modeling_directions,
    write_modeling_direction_selection,
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _seed_method_index(factory_root: Path) -> None:
    _write(
        factory_root / "method_library" / "index.json",
        """
[
  {
    "domain": "optimization",
    "subdomain": "mathematical programming",
    "method": "MILP",
    "name_zh": "混合整数线性规划",
    "path": "method_library/optimization/milp.md",
    "keywords": ["整数规划", "生产计划", "资源分配"],
    "applicable_problem_types": ["离散决策", "多阶段决策"],
    "required_data": ["目标函数系数", "容量或资源约束"],
    "solver_stack": ["scipy.optimize.milp", "ortools"],
    "failure_modes": ["大 M 过松", "非线性关系未线性化"]
  },
  {
    "domain": "simulation",
    "subdomain": "monte carlo",
    "method": "MonteCarlo",
    "name_zh": "蒙特卡洛仿真",
    "path": "method_library/simulation/monte_carlo.md",
    "keywords": ["随机", "抽样", "不确定性"],
    "applicable_problem_types": ["风险评估", "随机情景"],
    "required_data": ["参数分布", "随机变量范围"],
    "solver_stack": ["numpy"],
    "failure_modes": ["样本量不足"]
  },
  {
    "domain": "evaluation",
    "subdomain": "multi-criteria ranking",
    "method": "TOPSIS",
    "name_zh": "优劣解距离法",
    "path": "method_library/evaluation/topsis.md",
    "keywords": ["多指标", "排序", "权重"],
    "applicable_problem_types": ["综合评价", "方案优选"],
    "required_data": ["方案-指标矩阵", "指标权重"],
    "solver_stack": ["numpy", "pandas"],
    "failure_modes": ["量纲未标准化"]
  }
]
""".strip()
        + "\n",
    )


def test_build_modeling_directions_returns_ranked_two_to_three_options(tmp_path):
    factory_root = tmp_path / "factory"
    project = factory_root / "ongoing" / "demo"
    _seed_method_index(factory_root)
    _write(
        project / "problem" / "method_retrieval.md",
        """
# Method Retrieval Results

| Rank | Score | Method | Domain | Path | Matched terms |
|---:|---:|---|---|---|---|
| 1 | 6.000 | 混合整数线性规划 (MILP) | optimization / mathematical programming | method_library/optimization/milp.md | 生产计划, 资源分配 |
| 2 | 4.500 | 蒙特卡洛仿真 (MonteCarlo) | simulation / monte carlo | method_library/simulation/monte_carlo.md | 随机, 抽样 |
| 3 | 3.000 | 优劣解距离法 (TOPSIS) | evaluation / multi-criteria ranking | method_library/evaluation/topsis.md | 多指标, 排序 |
""".strip()
        + "\n",
    )
    _write(
        project / "problem" / "data_inventory.md",
        "题目给出目标函数系数、容量或资源约束、随机变量范围和指标权重。\n",
    )

    payload = build_modeling_directions(project, factory_root)

    assert payload["available"] is True
    assert [item["rank"] for item in payload["directions"]] == [1, 2, 3]
    assert 2 <= len(payload["directions"]) <= 3
    assert payload["directions"][0]["id"] == "milp"
    assert payload["directions"][0]["correctness_score"] >= payload["directions"][1]["correctness_score"]
    assert payload["directions"][0]["feasibility_score"] >= payload["directions"][1]["feasibility_score"]


def test_write_modeling_direction_selection_records_step1_guidance(tmp_path):
    project = tmp_path / "project"
    direction = {
        "id": "milp",
        "title": "混合整数线性规划",
        "method": "MILP",
        "method_path": "method_library/optimization/milp.md",
        "correctness_score": 92,
        "feasibility_score": 88,
        "rationale": "适合离散决策和多阶段约束。",
        "risks": ["大 M 过松"],
    }

    write_modeling_direction_selection(
        project,
        direction,
        timestamp="2026-07-01 12:00:00",
    )

    text = (project / "human_review.md").read_text(encoding="utf-8")
    assert "## Step 1 modeling directions:" in text
    assert "STATUS: READY" in text
    assert "Selected direction id: milp" in text
    assert "method_library/optimization/milp.md" in text
    assert "请 Step 1 将该方向作为高优先级候选流" in text
