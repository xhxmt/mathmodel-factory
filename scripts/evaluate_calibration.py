#!/usr/bin/env python3
"""Evaluate offline judge calibration against labeled real-paper ordering."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


MODEL_REOPEN = "REOPEN_REVISION_MODEL"


def _load_result(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _score(data: dict[str, Any]) -> float | None:
    llm = data.get("llm_score") if isinstance(data.get("llm_score"), dict) else {}
    for value in (
        llm.get("median_recomputed"),
        data.get("median_recomputed"),
        llm.get("median_total"),
        data.get("median_total"),
    ):
        if isinstance(value, (int, float)):
            return float(value)
    return None


def _verdicts(data: dict[str, Any]) -> list[str | None]:
    values = data.get("verdicts")
    if not isinstance(values, list):
        llm = data.get("llm_score")
        if isinstance(llm, dict):
            values = llm.get("verdicts") or llm.get("verdict_distribution")
    if isinstance(values, dict):
        expanded: list[str] = []
        for verdict, count in values.items():
            if isinstance(count, int) and count > 0:
                expanded.extend([str(verdict)] * count)
        return expanded
    return [value if isinstance(value, str) else None for value in values] if isinstance(values, list) else []


def _fatal_detected(data: dict[str, Any]) -> bool:
    verdicts = [value for value in _verdicts(data) if value]
    if not verdicts:
        return False
    return sum(value == MODEL_REOPEN for value in verdicts) * 2 >= len(verdicts)


def evaluate_calibration(manifest: dict[str, Any], root: Path) -> dict[str, Any]:
    root = root.resolve()
    paper_rows: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    missing: list[str] = []
    malformed = 0
    total_runs = 0
    fatal_expected = 0
    fatal_detected = 0
    coverage: dict[str, dict[str, int]] = {}

    for item in manifest.get("papers", []):
        paper_id = str(item["id"])
        problem_id = str(item.get("problem_id") or "UNKNOWN")
        result_path = root / str(item.get("result_path") or "")
        data = _load_result(result_path)
        score = _score(data) if data else None
        available = data is not None and score is not None
        row = {
            "id": paper_id,
            "problem_id": problem_id,
            "award_tier": item.get("award_tier"),
            "category": item.get("category"),
            "paper_path": item.get("paper_path"),
            "result_path": item.get("result_path"),
            "status": "AVAILABLE" if available else "MISSING",
            "score": score,
            "fatal_flaw_detected": _fatal_detected(data) if data else False,
        }
        paper_rows.append(row)
        by_id[paper_id] = row
        stats = coverage.setdefault(problem_id, {"available": 0, "total": 0})
        stats["total"] += 1
        if available:
            stats["available"] += 1
        else:
            missing.append(paper_id)

        if data:
            n = data.get("n")
            n_scored = data.get("n_scored")
            if not isinstance(n, int):
                llm = data.get("llm_score")
                n = llm.get("n") if isinstance(llm, dict) else None
            if not isinstance(n_scored, int):
                llm = data.get("llm_score")
                n_scored = llm.get("n_scored") if isinstance(llm, dict) else None
            if isinstance(n, int) and n >= 0:
                total_runs += n
                malformed += max(0, n - (n_scored if isinstance(n_scored, int) else 0))

        if item.get("expected_fatal_flaw") is True and data:
            fatal_expected += 1
            if row["fatal_flaw_detected"]:
                fatal_detected += 1

    pair_rows: list[dict[str, Any]] = []
    correct_points = 0.0
    concordant = discordant = ties = 0
    for pair in manifest.get("pairs", []):
        higher_id = str(pair["higher"])
        lower_id = str(pair["lower"])
        higher = by_id.get(higher_id)
        lower = by_id.get(lower_id)
        if not higher or not lower or higher["score"] is None or lower["score"] is None:
            pair_rows.append(
                {"higher": higher_id, "lower": lower_id, "status": "MISSING", "credit": None}
            )
            continue
        delta = float(higher["score"]) - float(lower["score"])
        if abs(delta) <= 1e-9:
            credit = 0.5
            status = "TIE"
            ties += 1
        elif delta > 0:
            credit = 1.0
            status = "CORRECT"
            concordant += 1
        else:
            credit = 0.0
            status = "REVERSED"
            discordant += 1
        correct_points += credit
        pair_rows.append(
            {
                "higher": higher_id,
                "lower": lower_id,
                "status": status,
                "credit": credit,
                "higher_score": higher["score"],
                "lower_score": lower["score"],
            }
        )

    evaluated_pairs = sum(row["credit"] is not None for row in pair_rows)
    tau_denominator = concordant + discordant + ties
    coverage_out = {
        problem: {
            "available": values["available"],
            "total": values["total"],
            "coverage": values["available"] / values["total"] if values["total"] else None,
        }
        for problem, values in sorted(coverage.items())
    }
    return {
        "papers": paper_rows,
        "missing_results": sorted(missing),
        "pairs": pair_rows,
        "pairwise": {
            "evaluated": evaluated_pairs,
            "total": len(pair_rows),
            "correct_points": correct_points,
            "accuracy": correct_points / evaluated_pairs if evaluated_pairs else None,
        },
        "ordering": {
            "concordant": concordant,
            "discordant": discordant,
            "ties": ties,
            "kendall_style_tau": (
                (concordant - discordant) / tau_denominator if tau_denominator else None
            ),
        },
        "malformed_outputs": {
            "malformed": malformed,
            "total_runs": total_runs,
            "rate": malformed / total_runs if total_runs else None,
        },
        "fatal_flaw_detection": {
            "detected": fatal_detected,
            "expected": fatal_expected,
            "rate": fatal_detected / fatal_expected if fatal_expected else None,
        },
        "coverage_by_problem": coverage_out,
    }


def _fmt(value: Any) -> str:
    if value is None:
        return "N/A"
    if isinstance(value, float):
        return f"{value:.3f}".rstrip("0").rstrip(".")
    return str(value)


def write_reports(report: dict[str, Any], json_path: Path, md_path: Path) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    lines = [
        "# Evaluation Calibration Report",
        "",
        "## Paper Coverage",
        "",
        "| Paper | Problem | Award tier | Status | Score |",
        "|---|---|---|---|---:|",
    ]
    for row in report["papers"]:
        lines.append(
            f"| {row['id']} | {row.get('problem_id', '')} | {row.get('award_tier') or ''} | "
            f"{row['status']} | {_fmt(row['score'])} |"
        )
    pairwise = report["pairwise"]
    ordering = report["ordering"]
    malformed = report["malformed_outputs"]
    fatal = report["fatal_flaw_detection"]
    lines.extend(
        [
            "",
            "## Metrics",
            "",
            f"- Pairwise award-order accuracy: {_fmt(pairwise['accuracy'])} "
            f"({pairwise['evaluated']}/{pairwise['total']} pairs evaluated)",
            f"- Kendall-style ordering: {_fmt(ordering['kendall_style_tau'])}",
            f"- Malformed-output rate: {_fmt(malformed['rate'])} "
            f"({malformed['malformed']}/{malformed['total_runs']})",
            f"- Fatal-flaw detection rate: {_fmt(fatal['rate'])} "
            f"({fatal['detected']}/{fatal['expected']})",
            "",
            "## Missing Results",
            "",
        ]
    )
    if report["missing_results"]:
        lines.extend(f"- {paper_id}: MISSING" for paper_id in report["missing_results"])
    else:
        lines.append("- None")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest")
    parser.add_argument("--existing-results", action="store_true")
    parser.add_argument("--json-output")
    parser.add_argument("--markdown-output")
    args = parser.parse_args()
    manifest_path = Path(args.manifest).resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    root = (manifest_path.parent / str(manifest.get("path_root") or ".")).resolve()
    report = evaluate_calibration(manifest, root)
    json_path = Path(args.json_output) if args.json_output else manifest_path.parent / "calibration_report.json"
    md_path = Path(args.markdown_output) if args.markdown_output else manifest_path.parent / "calibration_report.md"
    write_reports(report, json_path, md_path)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
