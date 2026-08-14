from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "problem-plan-v1"
ALLOWED_PHASES = ("understand", "data", "model", "solve", "validate", "report")
ALLOWED_EDGE_TYPES = ("depends_on", "provides_data", "provides_parameters", "validates")
NODE_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
METHOD_PATH_RE = re.compile(r"^method_library/[A-Za-z0-9_./-]+\.md$")
TOP_LEVEL_FIELDS = {"schema_version", "title", "problem_identity", "nodes", "edges", "topological_order"}
NODE_FIELDS = {
    "id",
    "title",
    "objective",
    "phase",
    "subproblem",
    "source_refs",
    "inputs",
    "outputs",
    "method_candidates",
}
EDGE_FIELDS = {"from", "to", "type", "rationale"}


class ProblemPlanError(ValueError):
    """Raised when a project-specific problem plan violates its contract."""


def _reject_unknown_fields(value: dict[str, Any], allowed: set[str], field: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ProblemPlanError(f"{field} contains unknown fields: {', '.join(unknown)}")


def _require_string(value: Any, field: str, *, maximum: int = 2_000) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProblemPlanError(f"{field} must be a non-empty string")
    normalized = value.strip()
    if len(normalized) > maximum:
        raise ProblemPlanError(f"{field} exceeds {maximum} characters")
    return normalized


def _safe_relative_path(value: Any, field: str) -> str:
    text = _require_string(value, field, maximum=512)
    path = Path(text)
    if path.is_absolute() or ".." in path.parts:
        raise ProblemPlanError(f"{field} must be a contained project-relative path")
    return path.as_posix()


def _string_list(value: Any, field: str, *, paths: bool = False) -> list[str]:
    if not isinstance(value, list):
        raise ProblemPlanError(f"{field} must be a list")
    if len(value) > 100:
        raise ProblemPlanError(f"{field} has too many items")
    result: list[str] = []
    for index, item in enumerate(value):
        normalized = (
            _safe_relative_path(item, f"{field}[{index}]")
            if paths
            else _require_string(item, f"{field}[{index}]", maximum=512)
        )
        if normalized not in result:
            result.append(normalized)
    return result


def validate_problem_plan(data: Any) -> dict[str, Any]:
    """Validate and normalize the persistent per-project dependency DAG.

    The fixed Stage/Step lifecycle remains authoritative. This DAG describes the
    problem-specific scientific dependencies that run *inside* that lifecycle.
    """

    if not isinstance(data, dict):
        raise ProblemPlanError("problem plan must be a JSON object")
    _reject_unknown_fields(data, TOP_LEVEL_FIELDS, "problem plan")
    if data.get("schema_version") != SCHEMA_VERSION:
        raise ProblemPlanError(f"schema_version must be {SCHEMA_VERSION}")

    title = _require_string(data.get("title"), "title", maximum=300)
    nodes_raw = data.get("nodes")
    edges_raw = data.get("edges")
    if not isinstance(nodes_raw, list) or not nodes_raw:
        raise ProblemPlanError("nodes must be a non-empty list")
    if len(nodes_raw) > 100:
        raise ProblemPlanError("nodes exceeds the 100-node safety limit")
    if not isinstance(edges_raw, list):
        raise ProblemPlanError("edges must be a list")
    if len(edges_raw) > 300:
        raise ProblemPlanError("edges exceeds the 300-edge safety limit")

    nodes: list[dict[str, Any]] = []
    node_ids: set[str] = set()
    for index, raw in enumerate(nodes_raw):
        if not isinstance(raw, dict):
            raise ProblemPlanError(f"nodes[{index}] must be an object")
        _reject_unknown_fields(raw, NODE_FIELDS, f"nodes[{index}]")
        node_id = _require_string(raw.get("id"), f"nodes[{index}].id", maximum=64)
        if not NODE_ID_RE.fullmatch(node_id):
            raise ProblemPlanError(
                f"nodes[{index}].id must match {NODE_ID_RE.pattern}"
            )
        if node_id in node_ids:
            raise ProblemPlanError(f"duplicate node id: {node_id}")
        node_ids.add(node_id)
        phase = _require_string(raw.get("phase"), f"nodes[{index}].phase", maximum=32)
        if phase not in ALLOWED_PHASES:
            raise ProblemPlanError(
                f"nodes[{index}].phase must be one of {', '.join(ALLOWED_PHASES)}"
            )
        methods = _string_list(
            raw.get("method_candidates", []),
            f"nodes[{index}].method_candidates",
        )
        for method in methods:
            if not METHOD_PATH_RE.fullmatch(method):
                raise ProblemPlanError(
                    f"nodes[{index}].method_candidates contains an invalid method path: {method}"
                )
        source_refs = _string_list(
            raw.get("source_refs", []), f"nodes[{index}].source_refs", paths=True
        )
        if not source_refs:
            raise ProblemPlanError(f"nodes[{index}].source_refs must not be empty")
        nodes.append(
            {
                "id": node_id,
                "title": _require_string(raw.get("title"), f"nodes[{index}].title", maximum=200),
                "objective": _require_string(
                    raw.get("objective"), f"nodes[{index}].objective", maximum=2_000
                ),
                "phase": phase,
                "subproblem": str(raw.get("subproblem") or "").strip()[:200],
                "source_refs": source_refs,
                "inputs": _string_list(raw.get("inputs", []), f"nodes[{index}].inputs"),
                "outputs": _string_list(raw.get("outputs", []), f"nodes[{index}].outputs"),
                "method_candidates": methods,
            }
        )

    edges: list[dict[str, Any]] = []
    edge_keys: set[tuple[str, str, str]] = set()
    adjacency = {node_id: [] for node_id in node_ids}
    indegree = {node_id: 0 for node_id in node_ids}
    node_phases = {node["id"]: node["phase"] for node in nodes}
    phase_order = {phase: index for index, phase in enumerate(ALLOWED_PHASES)}
    for index, raw in enumerate(edges_raw):
        if not isinstance(raw, dict):
            raise ProblemPlanError(f"edges[{index}] must be an object")
        _reject_unknown_fields(raw, EDGE_FIELDS, f"edges[{index}]")
        source = _require_string(raw.get("from"), f"edges[{index}].from", maximum=64)
        target = _require_string(raw.get("to"), f"edges[{index}].to", maximum=64)
        edge_type = _require_string(raw.get("type"), f"edges[{index}].type", maximum=32)
        if source not in node_ids or target not in node_ids:
            raise ProblemPlanError(f"edges[{index}] references an unknown node")
        if source == target:
            raise ProblemPlanError(f"edges[{index}] cannot be a self-loop")
        if edge_type not in ALLOWED_EDGE_TYPES:
            raise ProblemPlanError(
                f"edges[{index}].type must be one of {', '.join(ALLOWED_EDGE_TYPES)}"
            )
        if phase_order[node_phases[source]] > phase_order[node_phases[target]]:
            raise ProblemPlanError(
                f"edges[{index}] moves backward from {node_phases[source]} to {node_phases[target]}"
            )
        key = (source, target, edge_type)
        if key in edge_keys:
            raise ProblemPlanError(f"duplicate edge: {source} -> {target} ({edge_type})")
        edge_keys.add(key)
        adjacency[source].append(target)
        indegree[target] += 1
        edges.append(
            {
                "from": source,
                "to": target,
                "type": edge_type,
                "rationale": str(raw.get("rationale") or "").strip()[:1_000],
            }
        )

    ready = sorted(node_id for node_id, degree in indegree.items() if degree == 0)
    topological_order: list[str] = []
    while ready:
        node_id = ready.pop(0)
        topological_order.append(node_id)
        for target in sorted(adjacency[node_id]):
            indegree[target] -= 1
            if indegree[target] == 0:
                ready.append(target)
                ready.sort()
    if len(topological_order) != len(nodes):
        cyclic = sorted(node_id for node_id, degree in indegree.items() if degree > 0)
        raise ProblemPlanError(f"problem plan must be acyclic; cycle includes: {', '.join(cyclic)}")
    declared_order = data.get("topological_order")
    if declared_order is not None and declared_order != topological_order:
        raise ProblemPlanError("declared topological_order does not match the graph")

    normalized = {
        "schema_version": SCHEMA_VERSION,
        "title": title,
        "problem_identity": str(data.get("problem_identity") or "").strip()[:128],
        "nodes": nodes,
        "edges": edges,
        "topological_order": topological_order,
    }
    return normalized


def load_problem_plan(project_path: Path) -> dict[str, Any]:
    path = project_path / "problem" / "problem_plan.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ProblemPlanError("problem/problem_plan.json does not exist") from exc
    except json.JSONDecodeError as exc:
        raise ProblemPlanError(f"problem plan is not valid JSON: {exc}") from exc
    plan = validate_problem_plan(data)
    project_root = project_path.resolve()
    for node in plan["nodes"]:
        for relative in node["source_refs"]:
            candidate = project_path / relative
            try:
                contained = candidate.resolve(strict=True).is_relative_to(project_root)
            except OSError:
                contained = False
            if not contained or candidate.is_symlink() or not candidate.is_file():
                raise ProblemPlanError(
                    f"{node['id']} source_refs points to a missing or uncontained file: {relative}"
                )
    return plan


def problem_plan_fingerprint(plan: dict[str, Any]) -> str:
    normalized = validate_problem_plan(plan)
    encoded = json.dumps(
        normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
