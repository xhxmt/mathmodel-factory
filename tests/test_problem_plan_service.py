import json
from pathlib import Path

from web.backend.problem_plan_service import build_problem_plan


def test_problem_plan_service_returns_typed_dag_blocks(tmp_path: Path):
    path = tmp_path / "problem" / "problem_plan.json"
    path.parent.mkdir(parents=True)
    (tmp_path / "problem" / "problem_brief.md").write_text("# brief\n", encoding="utf-8")
    path.write_text(
        json.dumps(
            {
                "schema_version": "problem-plan-v1",
                "title": "测试任务图",
                "nodes": [
                    {
                        "id": "analyze",
                        "title": "分析",
                        "objective": "理解题意",
                        "phase": "understand",
                        "source_refs": ["problem/problem_brief.md"],
                        "inputs": [],
                        "outputs": ["定义"],
                        "method_candidates": [],
                    }
                ],
                "edges": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    payload = build_problem_plan(tmp_path)

    assert payload["available"] is True
    assert payload["schema_version"] == "node-output-v1"
    assert [block["render_type"] for block in payload["blocks"]] == [
        "key_value",
        "dag",
        "artifact_link",
    ]
    assert payload["blocks"][1]["content"]["nodes"][0]["phase_label"] == "题意分析"


def test_problem_plan_service_returns_structured_waiting_notice(tmp_path: Path):
    payload = build_problem_plan(tmp_path)

    assert payload["available"] is False
    assert payload["blocks"][0]["render_type"] == "notice"
