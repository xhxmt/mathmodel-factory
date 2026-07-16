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
    writing = data.get("writing") if isinstance(data.get("writing"), dict) else {}
    llm = data.get("llm_score") if isinstance(data.get("llm_score"), dict) else {}
    for value in (
        writing.get("median_score"),
        llm.get("median_recomputed"),
        data.get("median_recomputed"),
        llm.get("median_total"),
        data.get("median_total"),
    ):
        if isinstance(value, (int, float)):
            return float(value)
    return None


def _correctness_score(data: dict[str, Any]) -> float | None:
    section = data.get("correctness") if isinstance(data.get("correctness"), dict) else {}
    value = section.get("median_score")
    return float(value) if isinstance(value, (int, float)) else None


def _writing_dimensions(data: dict[str, Any]) -> dict[str, float]:
    writing = data.get("writing") if isinstance(data.get("writing"), dict) else {}
    dimensions = writing.get("dimensions") if isinstance(writing.get("dimensions"), dict) else {}
    return {
        str(name): float(value)
        for name, value in dimensions.items()
        if isinstance(value, (int, float))
    }


def _pair_result(path: Path) -> dict[str, Any] | None:
    data = _load_result(path)
    if not data or data.get("kind") != "blind_pairwise":
        return None
    return data


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
    correctness = data.get("correctness") if isinstance(data.get("correctness"), dict) else {}
    fatal_rate = correctness.get("fatal_flaw_rate")
    if isinstance(fatal_rate, (int, float)):
        return fatal_rate >= 0.5
    verdicts = [value for value in _verdicts(data) if value]
    if not verdicts:
        return False
    return sum(value == MODEL_REOPEN for value in verdicts) * 2 >= len(verdicts)


def evaluate_calibration(
    manifest: dict[str, Any], root: Path, results_dir_override: str | None = None
) -> dict[str, Any]:
    root = root.resolve()
    paper_rows: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    missing: list[str] = []
    malformed = 0
    total_runs = 0
    fatal_expected = 0
    fatal_detected = 0
    coverage: dict[str, dict[str, int]] = {}
    models: set[str] = set()

    for item in manifest.get("papers", []):
        paper_id = str(item["id"])
        problem_id = str(item.get("problem_id") or "UNKNOWN")
        result_ref = item.get("calibration_result_path") or item.get("result_path")
        if results_dir_override:
            result_ref = f"{results_dir_override.rstrip('/')}/paper_{paper_id}.json"
        if not result_ref and manifest.get("calibration_results_dir"):
            result_ref = f"{str(manifest['calibration_results_dir']).rstrip('/')}/paper_{paper_id}.json"
        result_ref = result_ref or ""
        result_path = root / str(result_ref)
        data = _load_result(result_path)
        if data and data.get("model"):
            models.add(str(data["model"]))
        score = _score(data) if data else None
        available = data is not None and score is not None
        row = {
            "id": paper_id,
            "problem_id": problem_id,
            "award_tier": item.get("award_tier"),
            "category": item.get("category"),
            "paper_path": item.get("paper_path"),
            "result_path": item.get("result_path"),
            "calibration_result_path": item.get("calibration_result_path"),
            "status": "AVAILABLE" if available else "MISSING",
            "score": score,
            "correctness_score": _correctness_score(data) if data else None,
            "writing_dimensions": _writing_dimensions(data) if data else {},
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
                n = data.get("samples_requested")
            if not isinstance(n_scored, int):
                n_scored = data.get("samples_scored")
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
    direct_evaluated = 0
    direct_complete = 0
    diagnostic_pairs = 0
    pair_total_runs = 0
    pair_malformed = 0
    axis_correct = {"overall": 0.0, "correctness": 0.0, "writing": 0.0}
    axis_evaluated = {"overall": 0, "correctness": 0, "writing": 0}
    for pair in manifest.get("pairs", []):
        higher_id = str(pair["higher"])
        lower_id = str(pair["lower"])
        readiness_eligible = pair.get("readiness_eligible") is not False
        higher = by_id.get(higher_id)
        lower = by_id.get(lower_id)
        direct_path_ref = pair.get("result_path")
        if results_dir_override:
            pair_id = str(pair.get("id") or f"{higher_id}__vs__{lower_id}")
            direct_path_ref = f"{results_dir_override.rstrip('/')}/pair_{pair_id}.json"
        direct = _pair_result(root / str(direct_path_ref)) if direct_path_ref else None
        if direct and direct.get("model"):
            models.add(str(direct["model"]))
        if direct and direct.get("overall_winner") in {higher_id, lower_id, "TIE"}:
            requested = direct.get("samples_requested")
            scored = direct.get("samples_scored")
            malformed_count = direct.get("malformed")
            if isinstance(requested, int) and requested >= 0:
                pair_total_runs += requested
            if isinstance(malformed_count, int) and malformed_count >= 0:
                pair_malformed += malformed_count
            adjudicator_recovered = bool(direct.get("adjudicated")) and any(
                isinstance(run, dict)
                and run.get("role") == "adjudicator"
                and run.get("status") != "MALFORMED"
                for run in direct.get("runs", [])
            )
            complete = adjudicator_recovered or (
                isinstance(requested, int)
                and requested > 0
                and isinstance(scored, int)
                and scored == requested
                and malformed_count == 0
            )
            expected_overall = str(pair.get("expected_overall_winner") or higher_id)
            winner = direct["overall_winner"]
            if winner == expected_overall:
                credit, status = 1.0, "CORRECT"
                if readiness_eligible:
                    concordant += 1
            elif winner == "TIE":
                credit, status = 0.5, "TIE"
                if readiness_eligible:
                    ties += 1
            else:
                credit, status = 0.0, "REVERSED"
                if readiness_eligible:
                    discordant += 1
            if readiness_eligible:
                direct_evaluated += 1
                direct_complete += int(complete)
                correct_points += credit
            else:
                diagnostic_pairs += 1
            for axis, result_key in (
                ("overall", "overall_winner"),
                ("correctness", "correctness_winner"),
                ("writing", "writing_winner"),
            ):
                expected = pair.get(f"expected_{axis}_winner")
                actual = direct.get(result_key)
                if readiness_eligible and expected in {higher_id, lower_id, "TIE"} and actual in {higher_id, lower_id, "TIE"}:
                    axis_evaluated[axis] += 1
                    axis_correct[axis] += 1.0 if actual == expected else 0.0
            pair_rows.append(
                {
                    "higher": higher_id,
                    "lower": lower_id,
                    "status": status,
                    "credit": credit,
                    "source": "BLIND_PAIRWISE",
                    "winner": winner,
                    "correctness_winner": direct.get("correctness_winner"),
                    "writing_winner": direct.get("writing_winner"),
                    "samples_scored": direct.get("samples_scored"),
                    "samples_requested": direct.get("samples_requested"),
                    "complete": complete,
                    "label_type": pair.get("label_type") or "AWARD_WEAK_PRIOR",
                    "expected_overall_winner": expected_overall,
                    "readiness_eligible": readiness_eligible,
                }
            )
            continue
        if not higher or not lower or higher["score"] is None or lower["score"] is None:
            pair_rows.append(
                {"higher": higher_id, "lower": lower_id, "status": "MISSING", "credit": None, "source": "NONE"}
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
        if readiness_eligible:
            correct_points += credit
        pair_rows.append(
            {
                "higher": higher_id,
                "lower": lower_id,
                "status": status,
                "credit": credit,
                "source": "ABSOLUTE_SCORE_FALLBACK",
                "higher_score": higher["score"],
                "lower_score": lower["score"],
                "readiness_eligible": readiness_eligible,
            }
        )

    evaluated_pairs = sum(
        row["credit"] is not None and row.get("readiness_eligible", True) for row in pair_rows
    )
    tau_denominator = concordant + discordant + ties
    coverage_out = {
        problem: {
            "available": values["available"],
            "total": values["total"],
            "coverage": values["available"] / values["total"] if values["total"] else None,
        }
        for problem, values in sorted(coverage.items())
    }
    malformed += pair_malformed
    total_runs += pair_total_runs
    pair_accuracy = correct_points / evaluated_pairs if evaluated_pairs else None
    malformed_rate = malformed / total_runs if total_runs else None
    fatal_rate = fatal_detected / fatal_expected if fatal_expected else None
    policy = manifest.get("readiness_policy") if isinstance(manifest.get("readiness_policy"), dict) else {}
    required_pair_accuracy = float(policy.get("min_pairwise_accuracy", 0.75))
    required_direct_coverage = float(policy.get("min_direct_pair_coverage", 1.0))
    max_malformed_rate = float(policy.get("max_malformed_rate", 0.1))
    min_fatal_rate = float(policy.get("min_fatal_flaw_detection_rate", 1.0))
    min_correctness_accuracy = float(policy.get("min_correctness_accuracy", required_pair_accuracy))
    min_writing_accuracy = float(policy.get("min_writing_accuracy", required_pair_accuracy))
    eligible_pair_count = sum(pair.get("readiness_eligible") is not False for pair in manifest.get("pairs", []))
    direct_coverage = direct_complete / eligible_pair_count if eligible_pair_count else None
    split_axis_coverage = (
        sum(
            row.get("correctness_score") is not None
            and row.get("score") is not None
            and len(row.get("writing_dimensions") or {}) >= 6
            for row in paper_rows
        ) / len(paper_rows)
        if paper_rows else None
    )
    readiness_checks = {
        "all_papers_scored": not missing,
        "split_axis_coverage": split_axis_coverage == 1.0,
        "direct_pair_coverage": direct_coverage is not None and direct_coverage >= required_direct_coverage,
        "pairwise_accuracy": pair_accuracy is not None and pair_accuracy >= required_pair_accuracy,
        "malformed_output_rate": malformed_rate is not None and malformed_rate <= max_malformed_rate,
        "fatal_flaw_detection": fatal_rate is not None and fatal_rate >= min_fatal_rate,
    }
    axis_accuracy = {
        axis: axis_correct[axis] / axis_evaluated[axis] if axis_evaluated[axis] else None
        for axis in axis_evaluated
    }
    axis_checks = {
        "correctness_pairwise_accuracy": (
            axis_accuracy["correctness"] is not None
            and axis_accuracy["correctness"] >= min_correctness_accuracy
        ),
        "writing_pairwise_accuracy": (
            axis_accuracy["writing"] is not None
            and axis_accuracy["writing"] >= min_writing_accuracy
        ),
    }
    proxy_ready = manifest.get("readiness_kind") == "proxy" and all(readiness_checks.values())
    axis_ready = proxy_ready and all(axis_checks.values())
    human_ready = manifest.get("readiness_kind") == "human" and all(readiness_checks.values())
    return {
        "models": sorted(models),
        "papers": paper_rows,
        "missing_results": sorted(missing),
        "pairs": pair_rows,
        "pairwise": {
            "evaluated": evaluated_pairs,
            "total": len(pair_rows),
            "readiness_total": eligible_pair_count,
            "diagnostic_pairs": diagnostic_pairs,
            "correct_points": correct_points,
            "accuracy": pair_accuracy,
            "direct_evaluated": direct_evaluated,
            "direct_complete": direct_complete,
            "direct_coverage": direct_coverage,
            "axis_accuracy": axis_accuracy,
            "axis_evaluated": axis_evaluated,
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
            "rate": malformed_rate,
        },
        "fatal_flaw_detection": {
            "detected": fatal_detected,
            "expected": fatal_expected,
            "rate": fatal_rate,
        },
        "coverage_by_problem": coverage_out,
        "split_axis_coverage": split_axis_coverage,
        "score_reliability": {
            "ready": human_ready,
            "checks": readiness_checks,
            "policy": {
                "min_pairwise_accuracy": required_pair_accuracy,
                "min_direct_pair_coverage": required_direct_coverage,
                "max_malformed_rate": max_malformed_rate,
                "min_fatal_flaw_detection_rate": min_fatal_rate,
                "min_correctness_accuracy": min_correctness_accuracy,
                "min_writing_accuracy": min_writing_accuracy,
            },
            "meaning": "Absolute scores and award prediction require independent human calibration",
        },
        "proxy_reliability": {
            "ready": proxy_ready,
            "checks": readiness_checks,
            "axis_ready": axis_ready,
            "axis_checks": axis_checks,
            "scope": ["overall_pairwise_ranking", "fatal_defect_detection"],
            "meaning": "Safe only for bounded overall A/B ranking against deterministic perturbations",
        },
        "axis_reliability": {
            "ready": axis_ready,
            "checks": axis_checks,
            "meaning": "Correctness and writing sub-axis labels require separate validation",
        },
        "human_calibration": {
            "ready": human_ready,
            "reason": None if human_ready else "no independent human ground truth",
        },
        "award_prediction_ready": human_ready,
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
        f"Models: {', '.join(report.get('models', [])) or 'N/A'}",
        "",
        "## Paper Coverage",
        "",
        "| Paper | Problem | Award tier | Status | Correctness | Writing |",
        "|---|---|---|---|---:|---:|",
    ]
    for row in report["papers"]:
        lines.append(
            f"| {row['id']} | {row.get('problem_id', '')} | {row.get('award_tier') or ''} | "
            f"{row['status']} | {_fmt(row.get('correctness_score'))} | {_fmt(row['score'])} |"
        )
    pairwise = report["pairwise"]
    ordering = report["ordering"]
    malformed = report["malformed_outputs"]
    fatal = report["fatal_flaw_detection"]
    reliability = report.get("score_reliability", {})
    proxy = report.get("proxy_reliability", {})
    axis = report.get("axis_reliability", {})
    lines.extend(
        [
            "",
            "## Metrics",
            "",
            f"- Pairwise award-order accuracy: {_fmt(pairwise['accuracy'])} "
            f"({pairwise['evaluated']}/{pairwise.get('readiness_total', pairwise['total'])} readiness pairs; "
            f"{pairwise.get('diagnostic_pairs', 0)} diagnostic pairs excluded)",
            f"- Kendall-style ordering: {_fmt(ordering['kendall_style_tau'])}",
            f"- Malformed-output rate: {_fmt(malformed['rate'])} "
            f"({malformed['malformed']}/{malformed['total_runs']})",
            f"- Fatal-flaw detection rate: {_fmt(fatal['rate'])} "
            f"({fatal['detected']}/{fatal['expected']})",
            f"- Direct blind-pair coverage: {_fmt(pairwise.get('direct_coverage'))}",
            f"- Split correctness/writing coverage: {_fmt(report.get('split_axis_coverage'))}",
            f"- Step 13 score reliability: {'READY' if reliability.get('ready') else 'NOT READY'}",
            f"- Proxy A/B reliability: {'READY' if proxy.get('ready') else 'NOT READY'}",
            f"- Correctness/writing axis reliability: {'READY' if axis.get('ready') else 'NOT READY'}",
            f"- Human calibration: {'READY' if report.get('human_calibration', {}).get('ready') else 'NOT READY'}",
            f"- Award prediction: {'READY' if report.get('award_prediction_ready') else 'NOT READY'}",
            "",
            "## Blind Pairwise Results",
            "",
            "| Expected higher | Expected lower | Result | Source | Complete |",
            "|---|---|---|---|---|",
        ]
    )
    for row in report.get("pairs", []):
        lines.append(
            f"| {row['higher']} | {row['lower']} | "
            f"{row['status'] if row.get('readiness_eligible', True) else 'DIAGNOSTIC_' + row['status']} | "
            f"{row.get('source', '')} | {row.get('complete', 'N/A')} |"
        )
    lines.extend(["", "## Reliability Checks", ""])
    for name, ok in (proxy.get("checks") or reliability.get("checks", {})).items():
        lines.append(f"- {'PASS' if ok else 'FAIL'}: {name}")
    for name, ok in axis.get("checks", {}).items():
        lines.append(f"- {'PASS' if ok else 'FAIL'}: {name}")
    lines.extend(["", "## Missing Results", ""])
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
    parser.add_argument("--require-ready", action="store_true", help="Exit 1 unless human score reliability is ready.")
    parser.add_argument("--require-proxy-ready", action="store_true", help="Exit 1 unless proxy A/B reliability is ready.")
    parser.add_argument("--results-dir", help="Override result directory referenced by the manifest.")
    args = parser.parse_args()
    manifest_path = Path(args.manifest).resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    root = (manifest_path.parent / str(manifest.get("path_root") or ".")).resolve()
    report = evaluate_calibration(manifest, root, args.results_dir)
    json_path = Path(args.json_output) if args.json_output else manifest_path.parent / "calibration_report.json"
    md_path = Path(args.markdown_output) if args.markdown_output else manifest_path.parent / "calibration_report.md"
    write_reports(report, json_path, md_path)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.require_ready and not report["score_reliability"]["ready"]:
        return 1
    if args.require_proxy_ready and not report["proxy_reliability"]["ready"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
