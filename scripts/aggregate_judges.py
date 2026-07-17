#!/usr/bin/env python3
"""Validate and aggregate isolated judge outputs.

Each role file is a strict envelope: the first line is the runner-compatible
``VERDICT: ...`` header and the remaining content is one JSON object conforming
to ``judge-role-v1``.  A malformed role is INDETERMINATE; it is never repaired
or partially scored.
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any


ROLE_SCHEMA_VERSION = "judge-role-v1"
AGGREGATE_SCHEMA_VERSION = "judge-aggregate-v1"
AGGREGATE_JSON_BEGIN = "<!-- JUDGE_AGGREGATE_JSON_BEGIN -->"
AGGREGATE_JSON_END = "<!-- JUDGE_AGGREGATE_JSON_END -->"
PACKET_COMPLETENESS_VERSION = "judge-packet-completeness-v1"

DIMENSION_SPECS: tuple[tuple[str, str, int], ...] = (
    ("model_presentation", "模型呈现", 20),
    ("solution_narrative", "求解叙事", 20),
    ("innovation", "创新性", 20),
    ("writing_clarity", "写作清晰度", 15),
    ("result_persuasiveness", "结果说服力", 15),
    ("sensitivity_limitations", "敏感性与局限", 10),
)
DIMENSION_MAX = {key: maximum for key, _, maximum in DIMENSION_SPECS}


@dataclass(frozen=True)
class RoleResult:
    role: str
    status: str
    verdict: str | None
    fatal_flaws: int | None
    score: float | None
    dimensions: dict[str, dict[str, Any]] | None
    evidence_count: int
    source: str
    text: str
    error: str | None


@dataclass(frozen=True)
class AggregateResult:
    verdict: str
    status: str
    paper_score: float | None
    overall_score: float | None
    comparison_ready: bool
    vetoes: tuple[str, ...]
    indeterminate_roles: tuple[str, ...]
    dimensions: dict[str, dict[str, Any]] | None
    packet_completeness: dict[str, dict[str, Any]]
    roles: tuple[RoleResult, ...]


def _invalid(role: str, path: Path, text: str, error: str) -> RoleResult:
    return RoleResult(
        role=role,
        status="INDETERMINATE",
        verdict=None,
        fatal_flaws=None,
        score=None,
        dimensions=None,
        evidence_count=0,
        source=str(path),
        text=text,
        error=error,
    )


def _is_number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _require_exact_keys(data: dict[str, Any], expected: set[str], where: str) -> None:
    missing = sorted(expected - set(data))
    extra = sorted(set(data) - expected)
    if missing or extra:
        details = []
        if missing:
            details.append(f"missing={','.join(missing)}")
        if extra:
            details.append(f"extra={','.join(extra)}")
        raise ValueError(f"{where} keys invalid ({'; '.join(details)})")


def _require_string(value: object, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{where} must be a non-empty string")
    return value.strip()


def _validate_string_list(value: object, where: str) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"{where} must be an array")
    return [_require_string(item, f"{where}[{index}]") for index, item in enumerate(value)]


def _validate_hard_evidence(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise ValueError("evidence must be an array")
    validated: list[dict[str, str]] = []
    expected = {"claim", "location", "finding", "severity"}
    allowed_severity = {"support", "fatal", "risk"}
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValueError(f"evidence[{index}] must be an object")
        _require_exact_keys(item, expected, f"evidence[{index}]")
        evidence = {
            "claim": _require_string(item["claim"], f"evidence[{index}].claim"),
            "location": _require_string(item["location"], f"evidence[{index}].location"),
            "finding": _require_string(item["finding"], f"evidence[{index}].finding"),
            "severity": _require_string(item["severity"], f"evidence[{index}].severity"),
        }
        if evidence["severity"] not in allowed_severity:
            raise ValueError(f"evidence[{index}].severity is invalid")
        validated.append(evidence)
    return validated


def _validate_paper_evidence(value: object, where: str) -> list[dict[str, str]]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{where} must be a non-empty array")
    validated: list[dict[str, str]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValueError(f"{where}[{index}] must be an object")
        _require_exact_keys(item, {"location", "finding"}, f"{where}[{index}]")
        validated.append(
            {
                "location": _require_string(item["location"], f"{where}[{index}].location"),
                "finding": _require_string(item["finding"], f"{where}[{index}].finding"),
            }
        )
    return validated


def _load_envelope(path: Path, role: str) -> tuple[str, dict[str, Any], str]:
    if not path.is_file():
        raise ValueError("role output is missing")
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    if not lines or not lines[0].startswith("VERDICT: "):
        raise ValueError("first line must be VERDICT: <value>")
    header_verdict = lines[0][len("VERDICT: ") :].strip()
    body = "\n".join(lines[1:]).strip()
    if not body:
        raise ValueError("JSON payload is missing")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON payload: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise ValueError("JSON payload must be an object")
    return header_verdict, payload, text


def _read_hard_role(path: Path, role: str) -> RoleResult:
    text = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
    try:
        header_verdict, payload, text = _load_envelope(path, role)
        expected = {
            "schema_version",
            "role",
            "verdict",
            "fatal_flaws",
            "evidence",
            "limitations",
            "conclusion",
        }
        _require_exact_keys(payload, expected, "payload")
        if payload["schema_version"] != ROLE_SCHEMA_VERSION:
            raise ValueError("unsupported schema_version")
        if payload["role"] != role:
            raise ValueError("role does not match requested auditor")
        verdict = payload["verdict"]
        if verdict not in {"PASS", "FAIL", "INDETERMINATE"}:
            raise ValueError("invalid verdict")
        if header_verdict != verdict:
            raise ValueError("header verdict does not match JSON verdict")
        fatal = payload["fatal_flaws"]
        if not isinstance(fatal, int) or isinstance(fatal, bool) or fatal < 0:
            raise ValueError("fatal_flaws must be a non-negative integer")
        evidence = _validate_hard_evidence(payload["evidence"])
        limitations = _validate_string_list(payload["limitations"], "limitations")
        _require_string(payload["conclusion"], "conclusion")
        fatal_evidence = sum(item["severity"] == "fatal" for item in evidence)

        if verdict == "PASS":
            if fatal != 0 or fatal_evidence != 0:
                raise ValueError("PASS requires zero fatal flaws")
            if not any(item["severity"] == "support" for item in evidence):
                raise ValueError("PASS requires at least one supporting evidence item")
            status = "PASS"
        elif verdict == "FAIL":
            if fatal < 1 or fatal_evidence != fatal:
                raise ValueError("FAIL requires one fatal evidence item per fatal flaw")
            status = "FAIL"
        else:
            if fatal != 0 or fatal_evidence != 0:
                raise ValueError("INDETERMINATE cannot assert a fatal flaw")
            if not limitations:
                raise ValueError("INDETERMINATE requires at least one limitation")
            status = "INDETERMINATE"

        return RoleResult(
            role=role,
            status=status,
            verdict=verdict,
            fatal_flaws=fatal,
            score=None,
            dimensions=None,
            evidence_count=len(evidence),
            source=str(path),
            text=text,
            error=None,
        )
    except ValueError as exc:
        return _invalid(role, path, text, str(exc))


def _validate_dimensions(value: object) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict):
        raise ValueError("dimensions must be an object")
    _require_exact_keys(value, set(DIMENSION_MAX), "dimensions")
    validated: dict[str, dict[str, Any]] = {}
    for key, label, maximum in DIMENSION_SPECS:
        item = value[key]
        if not isinstance(item, dict):
            raise ValueError(f"dimensions.{key} must be an object")
        _require_exact_keys(item, {"score", "evidence"}, f"dimensions.{key}")
        score = item["score"]
        if not _is_number(score) or not 0 <= float(score) <= maximum:
            raise ValueError(f"dimensions.{key}.score must be within 0..{maximum}")
        validated[key] = {
            "label": label,
            "max": maximum,
            "score": float(score),
            "evidence": _validate_paper_evidence(
                item["evidence"], f"dimensions.{key}.evidence"
            ),
        }
    return validated


def _read_paper_role(path: Path) -> RoleResult:
    role = "paper"
    text = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
    try:
        header_verdict, payload, text = _load_envelope(path, role)
        expected = {
            "schema_version",
            "role",
            "verdict",
            "dimensions",
            "overall_score",
            "limitations",
            "recommendations",
            "conclusion",
        }
        _require_exact_keys(payload, expected, "payload")
        if payload["schema_version"] != ROLE_SCHEMA_VERSION:
            raise ValueError("unsupported schema_version")
        if payload["role"] != role:
            raise ValueError("role must be paper")
        verdict = payload["verdict"]
        if verdict not in {"PASS", "REVISE", "INDETERMINATE"}:
            raise ValueError("invalid verdict")
        if header_verdict != verdict:
            raise ValueError("header verdict does not match JSON verdict")
        limitations = _validate_string_list(payload["limitations"], "limitations")
        recommendations = _validate_string_list(payload["recommendations"], "recommendations")
        _require_string(payload["conclusion"], "conclusion")

        if verdict == "INDETERMINATE":
            if payload["dimensions"] is not None or payload["overall_score"] is not None:
                raise ValueError("INDETERMINATE paper review cannot contain scores")
            if not limitations:
                raise ValueError("INDETERMINATE requires at least one limitation")
            dimensions = None
            score = None
            status = "INDETERMINATE"
            evidence_count = 0
        else:
            dimensions = _validate_dimensions(payload["dimensions"])
            score_value = payload["overall_score"]
            if not _is_number(score_value) or not 0 <= float(score_value) <= 100:
                raise ValueError("overall_score must be within 0..100")
            score = round(float(score_value), 2)
            recomputed = round(sum(item["score"] for item in dimensions.values()), 2)
            if not math.isclose(score, recomputed, abs_tol=0.01):
                raise ValueError("overall_score must equal the sum of six dimension scores")
            if len(recommendations) != 3:
                raise ValueError("paper review requires exactly three recommendations")
            if verdict == "PASS" and score < 70:
                raise ValueError("PASS requires overall_score >= 70")
            if verdict == "REVISE" and score >= 70:
                raise ValueError("REVISE requires overall_score < 70")
            status = verdict
            evidence_count = sum(len(item["evidence"]) for item in dimensions.values())

        return RoleResult(
            role=role,
            status=status,
            verdict=verdict,
            fatal_flaws=None,
            score=score,
            dimensions=dimensions,
            evidence_count=evidence_count,
            source=str(path),
            text=text,
            error=None,
        )
    except ValueError as exc:
        return _invalid(role, path, text, str(exc))


def _read_role(path: Path, role: str) -> RoleResult:
    if role in {"math", "execution"}:
        return _read_hard_role(path, role)
    return _read_paper_role(path)


def _packet_completeness(
    manifest_path: Path | None, role: str
) -> tuple[dict[str, Any], str | None]:
    if manifest_path is None:
        return {
            "enforced": False,
            "manifest": None,
            "status": None,
            "eligible": None,
            "unmet_requirements": [],
            "limitations": [],
            "error": None,
        }, None
    summary: dict[str, Any] = {
        "enforced": True,
        "manifest": str(manifest_path),
        "status": "INCOMPLETE",
        "eligible": False,
        "unmet_requirements": [],
        "limitations": [],
        "error": None,
    }
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("manifest must be a JSON object")
        if payload.get("role") != role:
            raise ValueError("manifest role mismatch")
        completeness = payload.get("completeness")
        if not isinstance(completeness, dict):
            raise ValueError("manifest completeness is missing")
        if completeness.get("contract_version") != PACKET_COMPLETENESS_VERSION:
            raise ValueError("unsupported packet completeness contract")
        files = payload.get("files")
        if not isinstance(files, list):
            raise ValueError("manifest files must be an array")
        by_path: dict[str, dict[str, Any]] = {}
        for index, file_item in enumerate(files):
            if not isinstance(file_item, dict) or not isinstance(file_item.get("path"), str):
                raise ValueError(f"invalid manifest file at index {index}")
            by_path[file_item["path"]] = file_item
        requirements = completeness.get("requirements")
        if not isinstance(requirements, list) or not requirements:
            raise ValueError("packet completeness requirements are missing")
        unmet = []
        for index, requirement in enumerate(requirements):
            if not isinstance(requirement, dict) or not isinstance(requirement.get("id"), str):
                raise ValueError(f"invalid completeness requirement at index {index}")
            paths = requirement.get("paths")
            if not isinstance(paths, list) or any(not isinstance(path, str) for path in paths):
                raise ValueError(f"invalid completeness paths at index {index}")
            actual_satisfied = [
                path for path in paths if by_path.get(path, {}).get("status") == "included"
            ]
            actual_complete = bool(paths) and len(actual_satisfied) == len(paths)
            if requirement.get("satisfied_paths") != actual_satisfied:
                raise ValueError(f"completeness requirement {requirement['id']} paths conflict")
            if requirement.get("satisfied") is not actual_complete:
                raise ValueError(f"completeness requirement {requirement['id']} state conflicts")
            if not actual_complete:
                unmet.append(requirement["id"])
        limitations = completeness.get("limitations")
        if not isinstance(limitations, list):
            raise ValueError("packet completeness limitations must be an array")
        disclosed = {
            (item.get("path"), item.get("status"))
            for item in limitations
            if isinstance(item, dict)
        }
        undisclosed = [
            str(item["path"])
            for item in files
            if item.get("status") in {"truncated", "omitted"}
            and (item.get("path"), item.get("status")) not in disclosed
        ]
        if undisclosed:
            raise ValueError(
                "packet limitations omit truncated/omitted files: " + ", ".join(undisclosed)
            )
        declared_complete = (
            completeness.get("status") == "COMPLETE"
            and completeness.get("eligible") is True
        )
        actually_complete = not unmet
        if declared_complete != actually_complete:
            raise ValueError("packet completeness declaration is internally inconsistent")
        summary.update(
            {
                "status": completeness.get("status"),
                "eligible": declared_complete,
                "unmet_requirements": unmet,
                "limitations": limitations,
            }
        )
        if not declared_complete:
            detail = ", ".join(unmet) if unmet else "undeclared requirement"
            return summary, f"packet completeness gate: INCOMPLETE ({detail})"
        return summary, None
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        summary["error"] = str(exc)
        return summary, f"packet completeness gate: {exc}"


def _enforce_packet_completeness(
    result: RoleResult, manifest_path: Path | None
) -> tuple[RoleResult, dict[str, Any]]:
    summary, error = _packet_completeness(manifest_path, result.role)
    if error is None:
        return result, summary
    return (
        replace(
            result,
            status="INDETERMINATE",
            verdict="INDETERMINATE",
            fatal_flaws=None,
            score=None,
            dimensions=None,
            evidence_count=0,
            error=error,
        ),
        summary,
    )


def aggregate_outputs(
    *,
    math_path: Path,
    execution_path: Path,
    paper_path: Path,
    math_manifest: Path | None = None,
    execution_manifest: Path | None = None,
    paper_manifest: Path | None = None,
) -> AggregateResult:
    parsed = (
        (_read_role(math_path, "math"), math_manifest),
        (_read_role(execution_path, "execution"), execution_manifest),
        (_read_role(paper_path, "paper"), paper_manifest),
    )
    enforced = tuple(_enforce_packet_completeness(result, manifest) for result, manifest in parsed)
    roles = tuple(item[0] for item in enforced)
    packet_completeness = {
        result.role: summary for (result, _), (_, summary) in zip(parsed, enforced)
    }
    hard_roles = roles[:2]
    vetoes = tuple(item.role for item in hard_roles if item.status == "FAIL")
    indeterminate = tuple(item.role for item in roles if item.status == "INDETERMINATE")
    paper = roles[2]

    if vetoes:
        status = "FAIL"
        verdict = "REOPEN_REVISION_MODEL"
    elif indeterminate:
        status = "INDETERMINATE"
        verdict = "REOPEN_REVISION_MODEL"
    elif paper.status == "REVISE":
        status = "REVISE"
        verdict = "REOPEN_REVISION_TEXT"
    else:
        status = "PASS"
        verdict = "PASS"

    comparison_ready = not vetoes and not indeterminate and paper.score is not None
    overall_score = paper.score if comparison_ready else None
    return AggregateResult(
        verdict=verdict,
        status=status,
        paper_score=paper.score,
        overall_score=overall_score,
        comparison_ready=comparison_ready,
        vetoes=vetoes,
        indeterminate_roles=indeterminate,
        dimensions=paper.dimensions,
        packet_completeness=packet_completeness,
        roles=roles,
    )


def _machine_payload(result: AggregateResult) -> dict[str, Any]:
    return {
        "schema_version": AGGREGATE_SCHEMA_VERSION,
        "verdict": result.verdict,
        "status": result.status,
        "comparison_ready": result.comparison_ready,
        "overall_score": result.overall_score,
        "paper_score": result.paper_score,
        "vetoes": list(result.vetoes),
        "indeterminate_roles": list(result.indeterminate_roles),
        "dimensions": result.dimensions,
        "role_statuses": {role.role: role.status for role in result.roles},
    }


def write_aggregate_report(result: AggregateResult, output: Path, base_name: str) -> None:
    veto_text = ", ".join(result.vetoes) if result.vetoes else "none"
    indeterminate_text = (
        ", ".join(result.indeterminate_roles) if result.indeterminate_roles else "none"
    )
    displayed_score = (
        f"{result.overall_score:g}/100" if result.overall_score is not None else "N/A"
    )
    machine_json = json.dumps(
        _machine_payload(result), ensure_ascii=False, indent=2, allow_nan=False
    )
    lines = [
        f"VERDICT: {result.verdict}",
        AGGREGATE_JSON_BEGIN,
        machine_json,
        AGGREGATE_JSON_END,
        "",
        f"# Step 13 Independent Judge Aggregate - `{base_name}`",
        "",
        f"AGGREGATE_STATUS: {result.status}",
        f"COMPARISON_READY: {'true' if result.comparison_ready else 'false'}",
        f"整体得分: {displayed_score}",
        f"Paper diagnostic score: {result.paper_score if result.paper_score is not None else 'N/A'}",
        f"Correctness vetoes: {veto_text}",
        f"Indeterminate roles: {indeterminate_text}",
        "",
        "## Aggregation Rule",
        "",
        "Math and execution failures are hard vetoes.",
        "A comparable overall score exists only when both hard auditors pass and the paper review is schema-valid.",
        "Missing, malformed, or evidence-incomplete role output is INDETERMINATE and cannot pass.",
        "A role whose packet completeness contract is INCOMPLETE is deterministically INDETERMINATE, regardless of model output.",
    ]
    lines.extend(["", "## Packet Completeness", ""])
    for role in ("math", "execution", "paper"):
        summary = result.packet_completeness[role]
        if not summary.get("enforced"):
            lines.append(f"- {role}: not enforced by this caller")
            continue
        unmet = ", ".join(summary.get("unmet_requirements") or []) or "none"
        limitations = len(summary.get("limitations") or [])
        lines.append(
            f"- {role}: {summary.get('status', 'INCOMPLETE')}; "
            f"unmet={unmet}; disclosed limitations={limitations}"
        )
    if result.dimensions:
        lines.extend(["", "## Paper Quality Dimensions", "", "| Dimension | Score | Maximum |", "|---|---:|---:|"])
        for key, label, maximum in DIMENSION_SPECS:
            lines.append(f"| {label} | {result.dimensions[key]['score']:g} | {maximum} |")
    for role in result.roles:
        lines.extend(
            [
                "",
                f"## {role.role.title()} Auditor",
                "",
                f"Parsed status: {role.status}",
                f"Parsed verdict: {role.verdict or 'MISSING'}",
                f"Fatal flaws: {role.fatal_flaws if role.fatal_flaws is not None else 'N/A'}",
                f"Score: {role.score if role.score is not None else 'N/A'}",
                f"Evidence items: {role.evidence_count}",
                f"Schema error: {role.error or 'none'}",
                f"Source: `{role.source}`",
                "",
                role.text.strip() or "(missing output)",
            ]
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--math", required=True)
    parser.add_argument("--execution", required=True)
    parser.add_argument("--paper", required=True)
    parser.add_argument("--math-manifest")
    parser.add_argument("--execution-manifest")
    parser.add_argument("--paper-manifest")
    parser.add_argument("--output", required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--json")
    args = parser.parse_args()
    result = aggregate_outputs(
        math_path=Path(args.math),
        execution_path=Path(args.execution),
        paper_path=Path(args.paper),
        math_manifest=Path(args.math_manifest) if args.math_manifest else None,
        execution_manifest=Path(args.execution_manifest) if args.execution_manifest else None,
        paper_manifest=Path(args.paper_manifest) if args.paper_manifest else None,
    )
    write_aggregate_report(result, Path(args.output), args.base)
    if args.json:
        data = asdict(result)
        Path(args.json).write_text(
            json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
            encoding="utf-8",
        )
    print(f"{result.status}: {result.verdict}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
