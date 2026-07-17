#!/usr/bin/env python3
"""Parse schema-valid independent-judge aggregate reports.

Current reports contain a ``judge-aggregate-v1`` JSON block.  Only reports for
which every correctness auditor passed expose a comparable total.  Legacy
Markdown scorecards are detected for diagnostics but are never promoted onto
the current comparison axis.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import statistics
import sys
from pathlib import Path
from typing import Any


AGGREGATE_SCHEMA_VERSION = "judge-aggregate-v1"
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
# judge-aggregate-v1 because the old format lacks hard-auditor state and strict
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
                evidence_item, {"location", "finding"}, f"dimensions.{key}.evidence[{index}]"
            )
            if any(
                not isinstance(evidence_item[field], str) or not evidence_item[field].strip()
                for field in ("location", "finding")
            ):
                raise ValueError(f"dimensions.{key}.evidence[{index}] fields must be non-empty")
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


def _parse_current(text: str) -> dict[str, Any]:
    payload = _extract_aggregate_payload(text)
    expected = {
        "schema_version",
        "verdict",
        "status",
        "comparison_ready",
        "overall_score",
        "paper_score",
        "vetoes",
        "indeterminate_roles",
        "dimensions",
        "role_statuses",
    }
    _require_exact_keys(payload, expected, "aggregate")
    if payload["schema_version"] != AGGREGATE_SCHEMA_VERSION:
        raise ValueError("unsupported aggregate schema_version")
    if payload["verdict"] not in {"PASS", "REOPEN_REVISION_MODEL", "REOPEN_REVISION_TEXT"}:
        raise ValueError("aggregate verdict is invalid")
    first_line = text.splitlines()[0] if text.splitlines() else ""
    if first_line != f"VERDICT: {payload['verdict']}":
        raise ValueError("first-line verdict does not match aggregate JSON")
    if payload["status"] not in {"PASS", "FAIL", "INDETERMINATE", "REVISE"}:
        raise ValueError("aggregate status is invalid")
    if not isinstance(payload["comparison_ready"], bool):
        raise ValueError("comparison_ready must be boolean")
    vetoes = _validate_string_list(payload["vetoes"], "vetoes")
    indeterminate = _validate_string_list(payload["indeterminate_roles"], "indeterminate_roles")
    role_statuses = payload["role_statuses"]
    if not isinstance(role_statuses, dict):
        raise ValueError("role_statuses must be an object")
    _require_exact_keys(role_statuses, {"math", "execution", "paper"}, "role_statuses")
    if any(
        status not in {"PASS", "FAIL", "INDETERMINATE", "REVISE"}
        for status in role_statuses.values()
    ):
        raise ValueError("role_statuses contains an invalid status")
    expected_vetoes = [
        role for role in ("math", "execution") if role_statuses[role] == "FAIL"
    ]
    expected_indeterminate = [
        role for role in ("math", "execution", "paper")
        if role_statuses[role] == "INDETERMINATE"
    ]
    if vetoes != expected_vetoes:
        raise ValueError("vetoes do not match hard-role statuses")
    if indeterminate != expected_indeterminate:
        raise ValueError("indeterminate_roles do not match role statuses")

    if expected_vetoes:
        expected_status, expected_verdict = "FAIL", "REOPEN_REVISION_MODEL"
    elif expected_indeterminate:
        expected_status, expected_verdict = "INDETERMINATE", "REOPEN_REVISION_MODEL"
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
    else:
        if paper_score is None or dimensions is None:
            raise ValueError("valid paper role requires score and all six dimensions")
        paper_recomputed = round(
            sum(item["weighted_mean"] for item in dimensions.values()), 2
        )
        if not math.isclose(float(paper_score), paper_recomputed, abs_tol=0.01):
            raise ValueError("paper_score does not equal six-dimension sum")

    comparison_ready = payload["comparison_ready"]
    overall_score = payload["overall_score"]
    hard_pass = role_statuses["math"] == "PASS" and role_statuses["execution"] == "PASS"
    paper_valid = role_statuses["paper"] in {"PASS", "REVISE"}
    if comparison_ready:
        if not hard_pass or not paper_valid or vetoes or indeterminate:
            raise ValueError("comparison_ready conflicts with role states")
        if payload["status"] not in {"PASS", "REVISE"}:
            raise ValueError("comparison_ready requires PASS or REVISE aggregate status")
        if not _is_number(overall_score) or not 0 <= float(overall_score) <= 100:
            raise ValueError("comparable overall_score must be within 0..100")
        if dimensions is None:
            raise ValueError("comparable score requires all six dimensions")
        recomputed = round(sum(item["weighted_mean"] for item in dimensions.values()), 2)
        if not math.isclose(float(overall_score), recomputed, abs_tol=0.01):
            raise ValueError("overall_score does not equal six-dimension sum")
        if paper_score is None or not math.isclose(
            float(overall_score), float(paper_score), abs_tol=0.01
        ):
            raise ValueError("overall_score must equal paper_score when comparable")
    else:
        if overall_score is not None:
            raise ValueError("non-comparable aggregate must set overall_score to null")
        recomputed = None
        if payload["status"] in {"PASS", "REVISE"}:
            raise ValueError("PASS/REVISE aggregate must be comparison_ready")

    return {
        "schema_version": AGGREGATE_SCHEMA_VERSION,
        "schema_valid": True,
        "legacy": False,
        "verdict": payload["verdict"],
        "status": payload["status"],
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
        "parse_error": "legacy scorecard lacks judge-aggregate-v1 evidence and hard-gate state",
    }


def parse_file(path: Path, base: str | None = None) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8", errors="replace")
    if AGGREGATE_JSON_BEGIN in text or AGGREGATE_JSON_END in text:
        try:
            result = _parse_current(text)
        except ValueError as exc:
            result = {
                "schema_version": None,
                "schema_valid": False,
                "legacy": False,
                "verdict": None,
                "status": "INDETERMINATE",
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
    ready_runs = [run for run in runs if run.get("comparison_ready")]
    all_ready = bool(runs) and len(ready_runs) == len(runs)
    totals = [float(run["total"]) for run in ready_runs]
    recomputed = [float(run["total_recomputed"]) for run in ready_runs]

    # A partial median would silently discard a hard FAIL or INDETERMINATE run.
    # Keep it diagnostic-only and expose headline comparison fields only when
    # every requested run is current-schema and correctness-valid.
    median_total = round(statistics.median(totals), 2) if all_ready else None
    median_recomputed = round(statistics.median(recomputed), 2) if all_ready else None
    return {
        "base": base or next((run["base"] for run in runs if run.get("base")), None),
        "schema_version": AGGREGATE_SCHEMA_VERSION,
        "comparison_ready": all_ready,
        "n": len(runs),
        "n_scored": len(ready_runs),
        "median_total": median_total,
        "min_total": min(totals) if all_ready else None,
        "max_total": max(totals) if all_ready else None,
        "median_total_raw": median_total,
        "median_recomputed": median_recomputed,
        "min_recomputed": min(recomputed) if all_ready else None,
        "max_recomputed": max(recomputed) if all_ready else None,
        "diagnostic_median_valid": (
            round(statistics.median(recomputed), 2) if ready_runs else None
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
        valid = bool(output.get("comparison_ready"))
    else:
        if len(paths) != 1:
            print("ERROR: single-file mode takes exactly one file", file=sys.stderr)
            return 2
        output = parse_file(paths[0], base=args.base)
        valid = bool(output.get("comparison_ready"))

    print(json.dumps(output, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
