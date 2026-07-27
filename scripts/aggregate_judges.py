#!/usr/bin/env python3
"""Validate and aggregate isolated judge outputs.

Each role file is a strict envelope: the first line is the runner-compatible
``VERDICT: ...`` header and the remaining content is one JSON object. Current
roles cite immutable packet chunks and exact quoted text; the citations are
recomputed before a verdict can affect routing. A malformed or ungrounded role
is INDETERMINATE; it is never repaired or partially scored.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

try:
    from scripts.evidence_grounding import atomic_write_report, validate_grounding
except ModuleNotFoundError:  # Direct execution from scripts/.
    from evidence_grounding import atomic_write_report, validate_grounding  # type: ignore


LEGACY_ROLE_SCHEMA_VERSION = "judge-role-v1"
LEGACY_PAPER_SCHEMA_VERSION = "judge-paper-role-v2"
ROLE_SCHEMA_VERSION = "judge-hard-role-v2"
PAPER_ROLE_SCHEMA_VERSION = "judge-paper-role-v3"
AGGREGATE_SCHEMA_VERSION = "judge-aggregate-v3"
SCORE_SEMANTICS = "UNCALIBRATED_DIAGNOSTIC"
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
    score_available: bool
    score_semantics: str
    comparison_ready: bool
    vetoes: tuple[str, ...]
    indeterminate_roles: tuple[str, ...]
    dimensions: dict[str, dict[str, Any]] | None
    packet_completeness: dict[str, dict[str, Any]]
    evidence_grounding: dict[str, dict[str, Any]]
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
    required = {
        "ref_id",
        "claim",
        "chunk_id",
        "quote",
        "finding",
        "severity",
    }
    allowed_severity = {"support", "fatal", "risk"}
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValueError(f"evidence[{index}] must be an object")
        _require_exact_keys(item, required | ({"quote_sha256"} if "quote_sha256" in item else set()), f"evidence[{index}]")
        evidence = {
            field: _require_string(item[field], f"evidence[{index}].{field}")
            for field in required
        }
        if len(evidence["chunk_id"]) != 64:
            raise ValueError(f"evidence[{index}].chunk_id must be a SHA-256")
        computed_quote_hash = hashlib.sha256(evidence["quote"].encode("utf-8")).hexdigest()
        declared_quote_hash = item.get("quote_sha256")
        if declared_quote_hash is not None and declared_quote_hash != computed_quote_hash:
            raise ValueError(f"evidence[{index}].quote_sha256 does not match quote")
        evidence["quote_sha256"] = computed_quote_hash
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
        required = {"ref_id", "chunk_id", "quote", "finding"}
        expected = required | ({"quote_sha256"} if "quote_sha256" in item else set())
        _require_exact_keys(item, expected, f"{where}[{index}]")
        evidence = {
            field: _require_string(item[field], f"{where}[{index}].{field}")
            for field in required
        }
        if len(evidence["chunk_id"]) != 64:
            raise ValueError(f"{where}[{index}].chunk_id must be a SHA-256")
        computed_quote_hash = hashlib.sha256(evidence["quote"].encode("utf-8")).hexdigest()
        declared_quote_hash = item.get("quote_sha256")
        if declared_quote_hash is not None and declared_quote_hash != computed_quote_hash:
            raise ValueError(f"{where}[{index}].quote_sha256 does not match quote")
        evidence["quote_sha256"] = computed_quote_hash
        validated.append(evidence)
    return validated


def _validate_paper_issues(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise ValueError("issues must be an array")
    validated: list[dict[str, str]] = []
    required = {
        "ref_id",
        "severity",
        "chunk_id",
        "quote",
        "finding",
        "recommendation",
    }
    allowed_severity = {"blocking", "major", "minor"}
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValueError(f"issues[{index}] must be an object")
        expected = required | ({"quote_sha256"} if "quote_sha256" in item else set())
        _require_exact_keys(item, expected, f"issues[{index}]")
        issue = {
            field: _require_string(item[field], f"issues[{index}].{field}")
            for field in required
        }
        if issue["severity"] not in allowed_severity:
            raise ValueError(f"issues[{index}].severity is invalid")
        if len(issue["chunk_id"]) != 64:
            raise ValueError(f"issues[{index}].chunk_id must be a SHA-256")
        computed_quote_hash = hashlib.sha256(issue["quote"].encode("utf-8")).hexdigest()
        declared_quote_hash = item.get("quote_sha256")
        if declared_quote_hash is not None and declared_quote_hash != computed_quote_hash:
            raise ValueError(f"issues[{index}].quote_sha256 does not match quote")
        issue["quote_sha256"] = computed_quote_hash
        validated.append(issue)
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
        if payload["schema_version"] == LEGACY_ROLE_SCHEMA_VERSION:
            return RoleResult(
                role=role,
                status="LEGACY_UNVERIFIED",
                verdict=payload.get("verdict") if isinstance(payload.get("verdict"), str) else None,
                fatal_flaws=payload.get("fatal_flaws") if isinstance(payload.get("fatal_flaws"), int) else None,
                score=None,
                dimensions=None,
                evidence_count=len(payload.get("evidence", [])) if isinstance(payload.get("evidence"), list) else 0,
                source=str(path),
                text=text,
                error=f"legacy hard-role schema {LEGACY_ROLE_SCHEMA_VERSION} is diagnostic-only",
            )
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
        legacy_expected = {
            "schema_version",
            "role",
            "verdict",
            "dimensions",
            "overall_score",
            "limitations",
            "recommendations",
            "conclusion",
        }
        expected = legacy_expected | {"issues"}
        schema_version = payload.get("schema_version")
        if schema_version in {LEGACY_ROLE_SCHEMA_VERSION, LEGACY_PAPER_SCHEMA_VERSION}:
            _require_exact_keys(
                payload,
                expected if schema_version == LEGACY_PAPER_SCHEMA_VERSION else legacy_expected,
                "payload",
            )
            score_value = payload.get("overall_score")
            score = round(float(score_value), 2) if _is_number(score_value) else None
            return RoleResult(
                role=role,
                status="LEGACY_UNVERIFIED",
                verdict=payload.get("verdict") if isinstance(payload.get("verdict"), str) else None,
                fatal_flaws=None,
                score=score,
                dimensions=None,
                evidence_count=0,
                source=str(path),
                text=text,
                error=f"legacy paper schema {schema_version} is diagnostic-only",
            )
        elif schema_version == PAPER_ROLE_SCHEMA_VERSION:
            _require_exact_keys(payload, expected, "payload")
        else:
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
        issues = _validate_paper_issues(payload["issues"])

        if verdict == "INDETERMINATE":
            if payload["dimensions"] is not None or payload["overall_score"] is not None:
                raise ValueError("INDETERMINATE paper review cannot contain scores")
            if not limitations:
                raise ValueError("INDETERMINATE requires at least one limitation")
            if issues:
                raise ValueError("INDETERMINATE paper review cannot assert issues")
            if recommendations:
                raise ValueError("INDETERMINATE paper review cannot contain recommendations")
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
            blocking_issues = [item for item in issues if item["severity"] == "blocking"]
            if verdict == "REVISE" and not blocking_issues:
                raise ValueError("REVISE requires at least one blocking issue")
            if verdict == "PASS" and blocking_issues:
                raise ValueError("PASS cannot contain a blocking issue")
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


def _enforce_evidence_grounding(
    result: RoleResult, manifest_path: Path | None
) -> tuple[RoleResult, dict[str, Any]]:
    if manifest_path is None:
        return result, {
            "schema_version": "evidence-grounding-v1",
            "role": result.role,
            "enforced": False,
            "valid": None,
            "refs": [],
            "errors": [],
        }
    report = validate_grounding(
        Path(result.source),
        manifest_path,
        manifest_path.with_name("context.txt"),
        role=result.role,
    )
    report["enforced"] = True
    atomic_write_report(
        Path(result.source).with_name(f"{result.role}.grounding.json"), report
    )
    if report.get("valid") is True:
        return result, report
    messages = "; ".join(
        str(item.get("message"))
        for item in report.get("errors", [])
        if isinstance(item, dict) and item.get("message")
    ) or "evidence grounding failed"
    existing = f"{result.error}; " if result.error else ""
    return (
        replace(
            result,
            status="INDETERMINATE",
            verdict="INDETERMINATE",
            fatal_flaws=None,
            score=None,
            dimensions=None,
            evidence_count=0,
            error=existing + messages,
        ),
        report,
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
    grounded = tuple(
        _enforce_evidence_grounding(result, manifest)
        for result, (_, manifest) in zip((item[0] for item in enforced), parsed)
    )
    roles = tuple(item[0] for item in grounded)
    packet_completeness = {
        result.role: summary for (result, _), (_, summary) in zip(parsed, enforced)
    }
    evidence_grounding = {result.role: summary for result, summary in grounded}
    hard_roles = roles[:2]
    vetoes = tuple(item.role for item in hard_roles if item.status == "FAIL")
    indeterminate = tuple(
        item.role
        for item in roles
        if item.status in {"INDETERMINATE", "LEGACY_UNVERIFIED"}
    )
    paper = roles[2]

    if vetoes:
        status = "FAIL"
        verdict = "REOPEN_REVISION_MODEL"
    elif indeterminate:
        status = "INDETERMINATE"
        verdict = "INDETERMINATE_REVIEW"
    elif paper.status == "REVISE":
        status = "REVISE"
        verdict = "REOPEN_REVISION_TEXT"
    else:
        status = "PASS"
        verdict = "PASS"

    score_available = not vetoes and not indeterminate and paper.score is not None
    overall_score = paper.score if score_available else None
    return AggregateResult(
        verdict=verdict,
        status=status,
        paper_score=paper.score,
        overall_score=overall_score,
        score_available=score_available,
        score_semantics=SCORE_SEMANTICS,
        comparison_ready=False,
        vetoes=vetoes,
        indeterminate_roles=indeterminate,
        dimensions=paper.dimensions,
        packet_completeness=packet_completeness,
        evidence_grounding=evidence_grounding,
        roles=roles,
    )


def _machine_payload(result: AggregateResult) -> dict[str, Any]:
    return {
        "schema_version": AGGREGATE_SCHEMA_VERSION,
        "verdict": result.verdict,
        "status": result.status,
        "score_available": result.score_available,
        "score_semantics": result.score_semantics,
        "comparison_ready": result.comparison_ready,
        "overall_score": result.overall_score,
        "paper_score": result.paper_score,
        "vetoes": list(result.vetoes),
        "indeterminate_roles": list(result.indeterminate_roles),
        "dimensions": result.dimensions,
        "role_statuses": {role.role: role.status for role in result.roles},
        "evidence_grounding": result.evidence_grounding,
    }


def _artifact_payload(result: AggregateResult) -> dict[str, Any]:
    """Return the full JSON artifact used by routing, receipts and diagnostics.

    The Markdown envelope intentionally stays compact because
    ``parse_judge_score`` validates an exact public schema.  The sidecar JSON
    additionally carries role records and packet completeness so downstream
    control code can independently recheck how the verdict was obtained.
    """

    payload = _machine_payload(result)
    payload["packet_completeness"] = result.packet_completeness
    payload["roles"] = [asdict(role) for role in result.roles]
    return payload


def write_aggregate_report(result: AggregateResult, output: Path, base_name: str) -> None:
    veto_text = ", ".join(result.vetoes) if result.vetoes else "none"
    indeterminate_text = (
        ", ".join(result.indeterminate_roles) if result.indeterminate_roles else "none"
    )
    diagnostic_score = (
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
        f"SCORE_AVAILABLE: {'true' if result.score_available else 'false'}",
        f"COMPARISON_READY: {'true' if result.comparison_ready else 'false'}",
        f"整体诊断得分（未校准）: {diagnostic_score}",
        f"SCORE_SEMANTICS: {result.score_semantics}",
        f"Paper diagnostic score: {result.paper_score if result.paper_score is not None else 'N/A'}",
        f"Correctness vetoes: {veto_text}",
        f"Indeterminate roles: {indeterminate_text}",
        "",
        "## Aggregation Rule",
        "",
        "Math and execution failures are hard vetoes.",
        "A schema-valid score may be available after both hard auditors pass, but it remains an uncalibrated diagnostic.",
        "Raw in-loop aggregates are never comparison-ready; external calibration enrichment may promote readiness.",
        "Missing, malformed, legacy, or evidence-incomplete role output is INDETERMINATE and cannot pass.",
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
    lines.extend(["", "## Evidence Grounding", ""])
    for role in ("math", "execution", "paper"):
        grounding = result.evidence_grounding[role]
        if grounding.get("enforced") is not True:
            lines.append(f"- {role}: not enforced by this caller")
            continue
        lines.append(
            f"- {role}: {'VALID' if grounding.get('valid') is True else 'INVALID'}; "
            f"resolved refs={len(grounding.get('refs') or [])}; "
            f"errors={len(grounding.get('errors') or [])}"
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
        data = _artifact_payload(result)
        Path(args.json).write_text(
            json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
            encoding="utf-8",
        )
    print(f"{result.status}: {result.verdict}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
