from __future__ import annotations

from pathlib import Path
from typing import Any

from factory_core.problem_plan import (
    ProblemPlanError,
    load_problem_plan,
    problem_plan_fingerprint,
)

from .content_blocks import ContentBlock, NodeOutput


PHASE_LABELS = {
    "understand": "题意分析",
    "data": "数据准备",
    "model": "模型建立",
    "solve": "计算求解",
    "validate": "结果验证",
    "report": "论文交付",
}


def build_problem_plan(project_path: Path) -> dict[str, Any]:
    try:
        plan = load_problem_plan(project_path)
    except ProblemPlanError as exc:
        output = NodeOutput(
            node_id="problem-plan",
            title="问题任务图",
            blocks=[
                ContentBlock(
                    id="problem-plan.status",
                    type="status",
                    label="任务图状态",
                    render_type="notice",
                    content={"level": "waiting", "message": str(exc)},
                )
            ],
            metadata={"available": False},
        )
        return {"available": False, "message": str(exc), **output.to_ui_dict()}

    node_by_id = {node["id"]: node for node in plan["nodes"]}
    graph_nodes = [
        {
            **node,
            "phase_label": PHASE_LABELS.get(node["phase"], node["phase"]),
            "order": plan["topological_order"].index(node["id"]) + 1,
        }
        for node in plan["nodes"]
    ]
    graph_edges = [
        {
            **edge,
            "from_title": node_by_id[edge["from"]]["title"],
            "to_title": node_by_id[edge["to"]]["title"],
        }
        for edge in plan["edges"]
    ]
    phase_counts: dict[str, int] = {}
    for node in plan["nodes"]:
        label = PHASE_LABELS.get(node["phase"], node["phase"])
        phase_counts[label] = phase_counts.get(label, 0) + 1

    output = NodeOutput(
        node_id="problem-plan",
        title=plan["title"],
        blocks=[
            ContentBlock(
                id="problem-plan.summary",
                type="summary",
                label="任务图摘要",
                render_type="key_value",
                content={
                    "节点": len(graph_nodes),
                    "依赖": len(graph_edges),
                    "阶段分布": " · ".join(f"{key} {value}" for key, value in phase_counts.items()),
                    "校验": "无环且引用完整",
                },
            ),
            ContentBlock(
                id="problem-plan.graph",
                type="graph",
                label="问题专属 DAG",
                render_type="dag",
                content={
                    "nodes": graph_nodes,
                    "edges": graph_edges,
                    "topological_order": plan["topological_order"],
                },
            ),
            ContentBlock(
                id="problem-plan.artifact",
                type="document",
                label="业务真相产物",
                render_type="artifact_link",
                content={
                    "path": "problem/problem_plan.json",
                    "name": "problem_plan.json",
                    "description": "Step 0 生成并经确定性 DAG 校验的题目任务计划",
                },
            ),
        ],
        metadata={
            "available": True,
            "fingerprint": problem_plan_fingerprint(plan),
            "schema_version": plan["schema_version"],
        },
    )
    return {"available": True, "plan": plan, **output.to_ui_dict()}
