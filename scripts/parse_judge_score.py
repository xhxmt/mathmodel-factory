#!/usr/bin/env python3
"""Parse schema-valid independent-judge aggregate reports.

Current reports contain a ``judge-aggregate-v3`` JSON block.  A schema-valid
score can be exposed as an uncalibrated diagnostic, but raw in-loop reports are
never comparison-ready.  External calibration enrichment owns that promotion.
Legacy aggregate envelopes and Markdown scorecards are diagnostic-only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import statistics
import sys
from pathlib import Path
from typing import Any


# Keep this value in lockstep with scripts.aggregate_judges.  The v3 envelope
# binds the three role states to evidence-grounding reports; accepting an older
# envelope as current would silently bypass that binding.
AGGREGATE_SCHEMA_VERSION = "judge-aggregate-v3"
LEGACY_AGGREGATE_SCHEMA_VERSIONS = frozenset({"judge-aggregate-v1", "judge-aggregate-v2"})
EVIDENCE_GROUNDING_SCHEMA_VERSION = "evidence-grounding-v1"
SCORE_SEMANTICS = "UNCALIBRATED_DIAGNOSTIC"
AGGREGATE_JSON_BEGIN = "<!-- JUDGE_AGGREGATE_JSON_BEGIN -->"
AGGREGATE_JSON_END = "<!-- JUDGE_AGGREGATE_JSON_END -->"

DIMENSION_SPECS: tuple[tuple[str, str, int], ...] = (
    ("model_presentation", "模型呈现", 20),
    ("solution_narrative", "求解叙事", 20),
    ("innovation", "创新性", 20),
    ("writing_clarity", "写作清晰度", 15),
    ("result_persuasiveness", "结果说服力", 15),
    ("sensitivity_limitations", "敏感性与局限", 10),
)
DIMENSION_MAX = {key: maximum for key, _, maximum in DIMENSION_SPECS}

# Legacy-only diagnostics.  These fields are deliberately not comparable with
# judge-aggregate-v3 because the old format lacks hard-auditor state and strict
# evidence binding.
LEGACY_DIMENSION_MAX = {
    "模型合理性": 20,
    "求解正确性": 20,
    "创新性": 20,
    "写作清晰度": 15,
    "结果说服力": 15,
    "灵敏度分析": 10,
}
VERDICT_RE = re.compile(r"^\s*VERDICT:\s*(\S+)", re.M)
TOTAL_LINE_RE = re.compile(r"整体得分[:：]\s*\**([\d.]+)\**\s*/\s*100")
TITLE_BASE_RE = re.compile(r"^#.*`([^`\n]+)`\s*$", re.M)
BOLD_RE = re.compile(r"\*\*\s*([^*]+?)\s*\*\*")
NUM_RE = re.compile(r"^-?\d+(?:\.\d+)?$")


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


def _validate_string_list(value: object, where: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{where} must be an array of strings")
    return value


def _validate_dimensions(value: object) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict):
        raise ValueError("dimensions must be an object")
    _require_exact_keys(value, set(DIMENSION_MAX), "dimensions")
    parsed: dict[str, dict[str, Any]] = {}
    for key, label, maximum in DIMENSION_SPECS:
        item = value[key]
        if not isinstance(item, dict):
            raise ValueError(f"dimensions.{key} must be an object")
        _require_exact_keys(item, {"label", "max", "score", "evidence"}, f"dimensions.{key}")
        if item["label"] != label or item["max"] != maximum:
            raise ValueError(f"dimensions.{key} label/max does not match schema")
        score = item["score"]
        if not _is_number(score) or not 0 <= float(score) <= maximum:
            raise ValueError(f"dimensions.{key}.score must be within 0..{maximum}")
        evidence = item["evidence"]
        if not isinstance(evidence, list) or not evidence:
            raise ValueError(f"dimensions.{key}.evidence must be non-empty")
        for index, evidence_item in enumerate(evidence):
            if not isinstance(evidence_item, dict):
                raise ValueError(f"dimensions.{key}.evidence[{index}] must be an object")
            _require_exact_keys(
                evidence_item,
                {"ref_id", "chunk_id", "quote", "quote_sha256", "finding"},
                f"dimensions.{key}.evidence[{index}]",
            )
            if any(
                not isinstance(evidence_item[field], str) or not evidence_item[field].strip()
                for field in ("ref_id", "chunk_id", "quote", "quote_sha256", "finding")
            ):
                raise ValueError(f"dimensions.{key}.evidence[{index}] fields must be non-empty")
            if (
                len(evidence_item["chunk_id"]) != 64
                or not re.fullmatch(r"[0-9a-f]{64}", evidence_item["chunk_id"])
                or len(evidence_item["quote_sha256"]) != 64
                or not re.fullmatch(r"[0-9a-f]{64}", evidence_item["quote_sha256"])
            ):
                raise ValueError(
                    f"dimensions.{key}.evidence[{index}] chunk/quote hashes must be SHA-256"
                )
            computed_quote_hash = hashlib.sha256(
                evidence_item["quote"].encode("utf-8")
            ).hexdigest()
            if evidence_item["quote_sha256"] != computed_quote_hash:
                raise ValueError(
                    f"dimensions.{key}.evidence[{index}] quote_sha256 does not match quote"
                )
        parsed[key] = {
            "label": label,
            "weight": maximum,
            "weighted_mean": float(score),
            "max": maximum,
            "evidence": evidence,
        }
    return parsed


def _extract_aggregate_payload(text: str) -> dict[str, Any]:
    if text.count(AGGREGATE_JSON_BEGIN) != 1 or text.count(AGGREGATE_JSON_END) != 1:
        raise ValueError("aggregate JSON markers are missing or duplicated")
    start = text.index(AGGREGATE_JSON_BEGIN) + len(AGGREGATE_JSON_BEGIN)
    end = text.index(AGGREGATE_JSON_END, start)
    raw = text[start:end].strip()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"aggregate JSON is invalid: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise ValueError("aggregate JSON must be an object")
    return payload


def _validate_evidence_grounding(value: object) -> dict[str, dict[str, Any]]:
    """Validate the compact grounding summaries embedded in an aggregate.

    The full quote/chunk verification is performed by ``aggregate_judges``.
    The parser still checks the summary envelope and role alignment so a caller
    cannot remove or rewrite the grounding result after aggregation.
    """

    if not isinstance(value, dict):
        raise ValueError("evidence_grounding must be an object")
    _require_exact_keys(value, {"math", "execution", "paper"}, "evidence_grounding")
    parsed: dict[str, dict[str, Any]] = {}
    for role in ("math", "execution", "paper"):
        summary = value[role]
        if not isinstance(summary, dict):
            raise ValueError(f"evidence_grounding.{role} must be an object")
        if summary.get("schema_version") != EVIDENCE_GROUNDING_SCHEMA_VERSION:
            raise ValueError(f"evidence_grounding.{role} has an unsupported schema")
        if summary.get("role") != role:
            raise ValueError(f"evidence_grounding.{role}.role does not match key")
        if not isinstance(summary.get("enforced"), bool):
            raise ValueError(f"evidence_grounding.{role}.enforced must be boolean")
        valid = summary.get("valid")
        if valid is not None and not isinstance(valid, bool):
            raise ValueError(f"evidence_grounding.{role}.valid must be boolean or null")
        refs = summary.get("refs")
        errors = summary.get("errors")
        if not isinstance(refs, list) or not isinstance(errors, list):
            raise ValueError(f"evidence_grounding.{role} refs/errors must be arrays")
        # A report that was actually enforced must be valid before a role can
        # contribute to a current aggregate.  Invalid reports are expected to
        # have been converted to INDETERMINATE by the aggregator; this check
        # catches post-hoc tampering in the Markdown machine block.
        parsed[role] = summary
    return parsed


def _legacy_aggregate_result(text: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Return a non-comparable diagnostic view of a v1/v2 aggregate.

    Older reports are useful for historical inspection, but their score and
    verdict were produced without the current grounding contract.  Preserve a
    small amount of metadata while making every comparison-facing field inert.
    """

    verdict = payload.get("verdict") if isinstance(payload.get("verdict"), str) else None
    raw_total = payload.get("overall_score")
    raw_paper = payload.get("paper_score")
    legacy_total = float(raw_total) if _is_number(raw_total) else None
    legacy_paper = float(raw_paper) if _is_number(raw_paper) else None
    return {
        "schema_version": payload.get("schema_version"),
        "schema_valid": False,
        "legacy": True,
        "verdict": verdict,
        "status": "LEGACY_UNVERIFIED",
        "score_available": False,
        "score_semantics": "LEGACY_UNVERIFIED",
        "comparison_ready": False,
        "total": None,
        "total_adjusted": None,
        "total_recomputed": None,
        "paper_score": None,
        "legacy_total": legacy_total,
        "legacy_paper_score": legacy_paper,
        "legacy_recomputed": None,
        "overflow_clamped": 0,
        "grade": None,
        "dims": {},
        "vetoes": [],
        "indeterminate_roles": [],
        "role_statuses": {},
        "evidence_grounding": {},
        "parse_error": (
            f"legacy {payload.get('schema_version', 'aggregate')} lacks current "
            f"{AGGREGATE_SCHEMA_VERSION} evidence-grounding contract"
        ),
    }


def _parse_current(text: str) -> dict[str, Any]:
    payload = _extract_aggregate_payload(text)
    expected = {
        "schema_version",
        "verdict",
        "status",
        "score_available",
        "score_semantics",
        "comparison_ready",
        "overall_score",
        "paper_score",
        "vetoes",
        "indeterminate_roles",
        "dimensions",
        "role_statuses",
        "evidence_grounding",
    }
    _require_exact_keys(payload, expected, "aggregate")
    if payload["schema_version"] != AGGREGATE_SCHEMA_VERSION:
        raise ValueError("unsupported aggregate schema_version")
    if payload["verdict"] not in {
        "PASS",
        "REOPEN_REVISION_MODEL",
        "REOPEN_REVISION_TEXT",
        "INDETERMINATE_REVIEW",
    }:
        raise ValueError("aggregate verdict is invalid")
    first_line = text.splitlines()[0] if text.splitlines() else ""
    if first_line != f"VERDICT: {payload['verdict']}":
        raise ValueError("first-line verdict does not match aggregate JSON")
    if payload["status"] not in {"PASS", "FAIL", "INDETERMINATE", "REVISE"}:
        raise ValueError("aggregate status is invalid")
    if not isinstance(payload["score_available"], bool):
        raise ValueError("score_available must be boolean")
    if payload["score_semantics"] != SCORE_SEMANTICS:
        raise ValueError("score_semantics must be UNCALIBRATED_DIAGNOSTIC")
    if not isinstance(payload["comparison_ready"], bool):
        raise ValueError("comparison_ready must be boolean")
    if payload["comparison_ready"]:
        raise ValueError("raw judge-aggregate-v3 cannot be comparison_ready")
    vetoes = _validate_string_list(payload["vetoes"], "vetoes")
    indeterminate = _validate_string_list(payload["indeterminate_roles"], "indeterminate_roles")
    role_statuses = payload["role_statuses"]
    if not isinstance(role_statuses, dict):
        raise ValueError("role_statuses must be an object")
    _require_exact_keys(role_statuses, {"math", "execution", "paper"}, "role_statuses")
    if any(
        status not in {"PASS", "FAIL", "INDETERMINATE", "REVISE", "LEGACY_UNVERIFIED"}
        for status in role_statuses.values()
    ):
        raise ValueError("role_statuses contains an invalid status")
    if any(
        role_statuses[role] not in {"PASS", "FAIL", "INDETERMINATE", "LEGACY_UNVERIFIED"}
        for role in ("math", "execution")
    ):
        raise ValueError("hard role has an invalid status")
    if role_statuses["paper"] not in {
        "PASS",
        "REVISE",
        "INDETERMINATE",
        "LEGACY_UNVERIFIED",
    }:
        raise ValueError("paper role has an invalid status")
    expected_vetoes = [
        role for role in ("math", "execution") if role_statuses[role] == "FAIL"
    ]
    expected_indeterminate = [
        role for role in ("math", "execution", "paper")
        if role_statuses[role] in {"INDETERMINATE", "LEGACY_UNVERIFIED"}
    ]
    if vetoes != expected_vetoes:
        raise ValueError("vetoes do not match hard-role statuses")
    if indeterminate != expected_indeterminate:
        raise ValueError("indeterminate_roles do not match role statuses")

    evidence_grounding = _validate_evidence_grounding(payload["evidence_grounding"])
    for role, status in role_statuses.items():
        summary = evidence_grounding[role]
        if (
            summary["enforced"]
            and summary["valid"] is not True
            and status not in {"INDETERMINATE", "LEGACY_UNVERIFIED"}
        ):
            raise ValueError(
                f"evidence_grounding.{role} is invalid for role status {status}"
            )

    if expected_vetoes:
        expected_status, expected_verdict = "FAIL", "REOPEN_REVISION_MODEL"
    elif expected_indeterminate:
        expected_status, expected_verdict = "INDETERMINATE", "INDETERMINATE_REVIEW"
    elif role_statuses["paper"] == "REVISE":
        expected_status, expected_verdict = "REVISE", "REOPEN_REVISION_TEXT"
    elif all(role_statuses[role] == "PASS" for role in ("math", "execution", "paper")):
        expected_status, expected_verdict = "PASS", "PASS"
    else:
        raise ValueError("role statuses do not form a valid aggregate state")
    if payload["status"] != expected_status or payload["verdict"] != expected_verdict:
        raise ValueError("aggregate status/verdict conflicts with role statuses")

    paper_score = payload["paper_score"]
    if paper_score is not None and (
        not _is_number(paper_score) or not 0 <= float(paper_score) <= 100
    ):
        raise ValueError("paper_score must be null or within 0..100")

    dimensions = None
    if payload["dimensions"] is not None:
        dimensions = _validate_dimensions(payload["dimensions"])
    if role_statuses["paper"] == "INDETERMINATE":
        if paper_score is not None or dimensions is not None:
            raise ValueError("indeterminate paper role cannot expose a score")
    elif role_statuses["paper"] == "LEGACY_UNVERIFIED":
        if (paper_score is None) != (dimensions is None):
            raise ValueError("legacy paper diagnostics must expose both score and dimensions or neither")
        if paper_score is not None:
            paper_recomputed = round(
                sum(item["weighted_mean"] for item in dimensions.values()), 2
            )
            if not math.isclose(float(paper_score), paper_recomputed, abs_tol=0.01):
                raise ValueError("legacy paper_score does not equal six-dimension sum")
    else:
        if paper_score is None or dimensions is None:
            raise ValueError("valid paper role requires score and all six dimensions")
        paper_recomputed = round(
            sum(item["weighted_mean"] for item in dimensions.values()), 2
        )
        if not math.isclose(float(paper_score), paper_recomputed, abs_tol=0.01):
            raise ValueError("paper_score does not equal six-dimension sum")

    score_available = payload["score_available"]
    comparison_ready = payload["comparison_ready"]
    overall_score = payload["overall_score"]
    hard_pass = role_statuses["math"] == "PASS" and role_statuses["execution"] == "PASS"
    paper_valid = role_statuses["paper"] in {"PASS", "REVISE"}
    structurally_available = hard_pass and paper_valid and not vetoes and not indeterminate
    if score_available != structurally_available:
        raise ValueError("score_available conflicts with role states")
    if score_available:
        if not hard_pass or not paper_valid or vetoes or indeterminate:
            raise ValueError("score_available conflicts with role states")
        if payload["status"] not in {"PASS", "REVISE"}:
            raise ValueError("score_available requires PASS or REVISE aggregate status")
        if not _is_number(overall_score) or not 0 <= float(overall_score) <= 100:
            raise ValueError("diagnostic overall_score must be within 0..100")
        if dimensions is None:
            raise ValueError("available score requires all six dimensions")
        recomputed = round(sum(item["weighted_mean"] for item in dimensions.values()), 2)
        if not math.isclose(float(overall_score), recomputed, abs_tol=0.01):
            raise ValueError("overall_score does not equal six-dimension sum")
        if paper_score is None or not math.isclose(
            float(overall_score), float(paper_score), abs_tol=0.01
        ):
            raise ValueError("overall_score must equal paper_score when available")
    else:
        if overall_score is not None:
            raise ValueError("score-unavailable aggregate must set overall_score to null")
        recomputed = None

    return {
        "schema_version": AGGREGATE_SCHEMA_VERSION,
        "schema_valid": True,
        "legacy": False,
        "verdict": payload["verdict"],
        "status": payload["status"],
        "score_available": score_available,
        "score_semantics": SCORE_SEMANTICS,
        "comparison_ready": comparison_ready,
        "total": float(overall_score) if overall_score is not None else None,
        "total_adjusted": float(overall_score) if overall_score is not None else None,
        "total_recomputed": recomputed,
        "paper_score": float(paper_score) if paper_score is not None else None,
        "overflow_clamped": 0,
        "grade": None,
        "dims": dimensions or {},
        "vetoes": vetoes,
        "indeterminate_roles": indeterminate,
        "role_statuses": role_statuses,
        "evidence_grounding": evidence_grounding,
        "parse_error": None,
    }


def _cells(line: str) -> list[str]:
    parts = line.split("|")
    if parts and parts[0].strip() == "":
        parts = parts[1:]
    if parts and parts[-1].strip() == "":
        parts = parts[:-1]
    return [cell.strip() for cell in parts]


def _first_number(text: str) -> float | None:
    for token in BOLD_RE.findall(text):
        if NUM_RE.match(token):
            return float(token)
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    return float(match.group(0)) if match else None


def _parse_legacy(text: str) -> dict[str, Any]:
    verdict_match = VERDICT_RE.search(text)
    total_match = TOTAL_LINE_RE.search(text)
    dims: dict[str, dict[str, Any]] = {}
    invalid_dimension = False
    for line in text.splitlines():
        if "|" not in line:
            continue
        cells = _cells(line)
        if not cells:
            continue
        name = cells[0].replace("*", "").strip()
        if name not in LEGACY_DIMENSION_MAX or len(cells) < 6:
            continue
        score = _first_number(cells[-2])
        maximum = LEGACY_DIMENSION_MAX[name]
        if score is None or not 0 <= score <= maximum:
            invalid_dimension = True
        dims[name] = {"weight": maximum, "weighted_mean": score, "max": maximum}
    complete = len(dims) == len(LEGACY_DIMENSION_MAX) and not invalid_dimension
    legacy_recomputed = (
        round(sum(float(item["weighted_mean"]) for item in dims.values()), 2)
        if complete
        else None
    )
    return {
        "schema_version": None,
        "schema_valid": False,
        "legacy": True,
        "verdict": verdict_match.group(1) if verdict_match else None,
        "status": "LEGACY_UNVERIFIED",
        "score_available": False,
        "score_semantics": "LEGACY_UNVERIFIED",
        "comparison_ready": False,
        "total": None,
        "total_adjusted": None,
        "total_recomputed": None,
        "paper_score": None,
        "legacy_total": float(total_match.group(1)) if total_match else None,
        "legacy_recomputed": legacy_recomputed,
        "overflow_clamped": 0,
        "grade": None,
        "dims": dims,
        "vetoes": [],
        "indeterminate_roles": [],
        "role_statuses": {},
        "evidence_grounding": {},
        "parse_error": f"legacy scorecard lacks {AGGREGATE_SCHEMA_VERSION} evidence and hard-gate state",
    }


def parse_file(path: Path, base: str | None = None) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8", errors="replace")
    if AGGREGATE_JSON_BEGIN in text or AGGREGATE_JSON_END in text:
        try:
            payload = _extract_aggregate_payload(text)
            schema = payload.get("schema_version")
            if schema in LEGACY_AGGREGATE_SCHEMA_VERSIONS:
                result = _legacy_aggregate_result(text, payload)
            else:
                result = _parse_current(text)
        except ValueError as exc:
            result = {
                "schema_version": None,
                "schema_valid": False,
                "legacy": False,
                "verdict": None,
                "status": "INDETERMINATE",
                "score_available": False,
                "score_semantics": None,
                "comparison_ready": False,
                "total": None,
                "total_adjusted": None,
                "total_recomputed": None,
                "paper_score": None,
                "overflow_clamped": 0,
                "grade": None,
                "dims": {},
                "vetoes": [],
                "indeterminate_roles": [],
                "role_statuses": {},
                "evidence_grounding": {},
                "parse_error": str(exc),
            }
    else:
        result = _parse_legacy(text)

    if base is None:
        title_match = TITLE_BASE_RE.search(text)
        if title_match:
            base = title_match.group(1).strip().strip("`")
        elif path.parent.name not in ("results", "complete", "ongoing", "."):
            base = path.parent.name
    result.update({"base": base, "source_file": str(path)})
    return result


def aggregate(paths: list[Path], base: str | None = None) -> dict[str, Any]:
    runs = [parse_file(path, base=base) for path in paths]
    scored_runs = [run for run in runs if run.get("score_available")]
    all_scored = bool(runs) and len(scored_runs) == len(runs)
    totals = [float(run["total"]) for run in scored_runs]
    recomputed = [float(run["total_recomputed"]) for run in scored_runs]

    # A partial median would silently discard a hard FAIL or INDETERMINATE run.
    # Keep partial samples diagnostic-only.  Even a complete set remains
    # uncalibrated until enrich_evaluation_result applies an exact-runtime
    # calibration report.
    median_total = round(statistics.median(totals), 2) if all_scored else None
    median_recomputed = round(statistics.median(recomputed), 2) if all_scored else None
    return {
        "base": base or next((run["base"] for run in runs if run.get("base")), None),
        "schema_version": AGGREGATE_SCHEMA_VERSION,
        "score_available": all_scored,
        "score_semantics": SCORE_SEMANTICS,
        "comparison_ready": False,
        "n": len(runs),
        "n_scored": len(scored_runs),
        "median_total": median_total,
        "min_total": min(totals) if all_scored else None,
        "max_total": max(totals) if all_scored else None,
        "median_total_raw": median_total,
        "median_recomputed": median_recomputed,
        "min_recomputed": min(recomputed) if all_scored else None,
        "max_recomputed": max(recomputed) if all_scored else None,
        "diagnostic_median_valid": (
            round(statistics.median(recomputed), 2) if scored_runs else None
        ),
        "any_clamped": False,
        "all_schema_valid": all(bool(run.get("schema_valid")) for run in runs),
        "verdicts": [run.get("verdict") for run in runs],
        "statuses": [run.get("status") for run in runs],
        "runs": runs,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="+", help="judge aggregate report(s)")
    parser.add_argument("--base", help="Override the project base name.")
    parser.add_argument("--aggregate", action="store_true", help="Fold multiple current reports.")
    args = parser.parse_args()

    paths = [Path(filename) for filename in args.files]
    missing = [str(path) for path in paths if not path.is_file()]
    if missing:
        print(f"ERROR: file(s) not found: {', '.join(missing)}", file=sys.stderr)
        return 2

    if args.aggregate:
        output = aggregate(paths, base=args.base)
        valid = bool(output.get("score_available"))
    else:
        if len(paths) != 1:
            print("ERROR: single-file mode takes exactly one file", file=sys.stderr)
            return 2
        output = parse_file(paths[0], base=args.base)
        valid = bool(output.get("score_available"))

    print(json.dumps(output, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
