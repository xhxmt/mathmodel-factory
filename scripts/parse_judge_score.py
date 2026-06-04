#!/usr/bin/env python3
"""Extract scores from a judge_evaluation.md-format file into JSON.

The in-loop Step 13 judge (prompts/step13_gate2_judge.txt) and the external
evaluator (evaluation/run_evaluation.sh) both emit the SAME on-disk format:

    VERDICT: PASS
    ...
    整体得分: 86.4/100  (按 CUMCM 评分细则 6 维度加权)
    ...
    | 维度 | 权重 | 评委 A | 评委 B | 评委 C | 加权均分 | 评级 |
    | 模型合理性 | 20% | 18 | 17 | 18 | **17.7** | 优 |
    ...
    | **总分** | 100% | | | | **86.4** | **优** |

This script is the shared reader for both, so external and in-loop scores can
be compared on the same axis (and later aggregated for ablations).

Usage:
    parse_judge_score.py <judge_file.md> [--base NAME]
    parse_judge_score.py --aggregate <run1.md> <run2.md> ...   # median + spread

Single-file mode prints one JSON object. --aggregate prints
{base, n, median_total, min_total, max_total, runs:[...]} across K judge runs
(used by run_evaluation.sh to fold K stochastic claude -p samples).
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from pathlib import Path

# The 6 CUMCM rubric dimensions with their per-dimension max scores
# (STEPS.md:144, step13_gate2_judge.txt:5-11). Keyed by name so we never
# mis-parse the header/separator/total rows. The max is used to CLAMP the
# weighted-mean cell: judges (esp. deepseek/haiku) sometimes emit an
# out-of-range value like 灵敏度分析 13/10, which would otherwise inflate the
# total and corrupt cross-paper ordering. (Mirrors the clamp already in
# perturbation_harness.py:_parse_score so the two parsers don't drift.)
DIMENSION_MAX = {
    "模型合理性": 20,
    "求解正确性": 20,
    "创新性": 20,
    "写作清晰度": 15,
    "结果说服力": 15,
    "灵敏度分析": 10,
}
DIMENSIONS = tuple(DIMENSION_MAX)

VERDICT_RE = re.compile(r"^\s*VERDICT:\s*(\S+)", re.M)
TOTAL_LINE_RE = re.compile(r"整体得分[:：]\s*([\d.]+)\s*/\s*100")
# An H1 line ending in a backtick-quoted token — matches both the in-loop
# title ("… Judge Simulation — `base`") and the external one ("… External Judge — `base`").
TITLE_BASE_RE = re.compile(r"^#.*`([^`\n]+)`\s*$", re.M)
BOLD_RE = re.compile(r"\*\*\s*([^*]+?)\s*\*\*")
NUM_RE = re.compile(r"^-?\d+(?:\.\d+)?$")


def _cells(line: str) -> list[str]:
    """Split a markdown table row into stripped cell strings."""
    parts = line.split("|")
    # Drop the empty leading/trailing fragments produced by edge pipes.
    if parts and parts[0].strip() == "":
        parts = parts[1:]
    if parts and parts[-1].strip() == "":
        parts = parts[:-1]
    return [c.strip() for c in parts]


def _first_number(text: str) -> float | None:
    for tok in BOLD_RE.findall(text):
        if NUM_RE.match(tok):
            return float(tok)
    # Fall back to a bare number if the cell isn't bolded.
    m = re.search(r"-?\d+(?:\.\d+)?", text)
    return float(m.group(0)) if m else None


def parse_file(path: Path, base: str | None = None) -> dict:
    text = path.read_text(encoding="utf-8", errors="replace")

    verdict_m = VERDICT_RE.search(text)
    verdict = verdict_m.group(1) if verdict_m else None

    # Per-dimension weighted means + grades, keyed by the fixed dimension names.
    dims: dict[str, dict] = {}
    total_from_table: float | None = None
    table_grade: str | None = None
    overflow_sum = 0.0  # total points clamped away from out-of-range dimensions
    dim_weighted_sum = 0.0  # sum of clamped weighted means (recomputed total)
    n_dims_seen = 0

    for line in text.splitlines():
        if "|" not in line:
            continue
        cells = _cells(line)
        if not cells:
            continue
        name = cells[0].replace("*", "").strip()

        if name == "总分":
            bolds = BOLD_RE.findall(line)
            for tok in bolds:
                if NUM_RE.match(tok):
                    total_from_table = float(tok)
                elif tok != "总分":
                    table_grade = tok  # last non-numeric bold = overall grade
            continue

        if name in DIMENSIONS and len(cells) >= 6:
            weighted = _first_number(cells[-2])  # 加权均分 column
            # Clamp an out-of-range weighted mean to the dimension's max so a
            # judge typo like 灵敏度分析 13/10 can't corrupt downstream stats.
            dim_max = DIMENSION_MAX[name]
            clamped = weighted is not None and weighted > dim_max
            if clamped:
                overflow_sum += weighted - dim_max
                weighted = float(dim_max)
            if weighted is not None:
                dim_weighted_sum += weighted
                n_dims_seen += 1
            grade = cells[-1].replace("*", "").strip() or None
            weight_m = re.search(r"(\d+)", cells[1]) if len(cells) > 1 else None
            dims[name] = {
                "weight": int(weight_m.group(1)) if weight_m else None,
                "weighted_mean": weighted,
                "max": dim_max,
                "grade": grade,
            }
            if clamped:
                dims[name]["clamped"] = True

    # The `整体得分:` line is the canonical total; fall back to the table row.
    total_m = TOTAL_LINE_RE.search(text)
    total = float(total_m.group(1)) if total_m else total_from_table

    # Adjusted total: the judge computes 整体得分 from its own (possibly
    # out-of-range) dimension cells, so an overflow like 灵敏度 13/10 inflates it.
    # Subtract the clamped overflow to recover a comparable total. Clean runs
    # (overflow_sum == 0) keep the judge's value unchanged, so the validated
    # in-loop reads (80.2 / 86.4) are preserved.
    total_adjusted = (round(total - overflow_sum, 2)
                      if total is not None and overflow_sum else total)

    # Recomputed total = sum of the (clamped) per-dimension weighted means. The
    # six dimension maxes sum to 100, so this IS a 0-100 score. Use it for
    # cross-paper comparison: some judges (observed: deepseek-chat) anchor the
    # hand-written 整体得分 to a near-constant value regardless of their own
    # dimension scores, which destroys ordering; the dimension sum does not.
    # Validated against the two in-loop files: recomputed 81.8/88.1 vs the
    # judge's 80.2/86.4 — same ordering, constant ~1.6 rounding gap.
    total_recomputed = round(dim_weighted_sum, 2) if n_dims_seen == len(DIMENSION_MAX) else None

    if base is None:
        title_m = TITLE_BASE_RE.search(text)
        if title_m:
            base = title_m.group(1).strip().strip("`")
        elif path.parent.name not in ("results", "complete", "ongoing", "."):
            base = path.parent.name

    return {
        "base": base,
        "verdict": verdict,
        "total": total,
        "total_adjusted": total_adjusted,
        "total_recomputed": total_recomputed,
        "overflow_clamped": round(overflow_sum, 2) if overflow_sum else 0,
        "grade": table_grade,
        "dims": dims,
        "source_file": str(path),
    }


def aggregate(paths: list[Path], base: str | None = None) -> dict:
    runs = [parse_file(p, base=base) for p in paths]
    # Aggregate over the adjusted total (overflow-clamped) so an out-of-range
    # dimension in one run can't skew the cross-paper median. Falls back to the
    # raw total when no clamping happened (clean runs are unchanged).
    adj = [r.get("total_adjusted") for r in runs if isinstance(r.get("total_adjusted"), (int, float))]
    raw = [r["total"] for r in runs if isinstance(r["total"], (int, float))]
    recomp = [r.get("total_recomputed") for r in runs if isinstance(r.get("total_recomputed"), (int, float))]
    agg = {
        "base": base or next((r["base"] for r in runs if r["base"]), None),
        "n": len(runs),
        "n_scored": len(adj),
        "median_total": round(statistics.median(adj), 2) if adj else None,
        "min_total": min(adj) if adj else None,
        "max_total": max(adj) if adj else None,
        "median_total_raw": round(statistics.median(raw), 2) if raw else None,
        # Cross-comparison axis (robust to total-score anchoring): median of the
        # recomputed dimension sums. See parse_file() for why this beats `total`.
        "median_recomputed": round(statistics.median(recomp), 2) if recomp else None,
        "min_recomputed": min(recomp) if recomp else None,
        "max_recomputed": max(recomp) if recomp else None,
        "any_clamped": any(r.get("overflow_clamped") for r in runs),
        "verdicts": [r["verdict"] for r in runs],
        "runs": runs,
    }
    return agg


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("files", nargs="+", help="judge_evaluation.md-format file(s)")
    parser.add_argument("--base", help="Override the project base name.")
    parser.add_argument("--aggregate", action="store_true",
                        help="Fold multiple judge runs into median + spread.")
    args = parser.parse_args()

    paths = [Path(f) for f in args.files]
    missing = [str(p) for p in paths if not p.is_file()]
    if missing:
        print(f"ERROR: file(s) not found: {', '.join(missing)}", file=sys.stderr)
        return 2

    if args.aggregate:
        out = aggregate(paths, base=args.base)
    else:
        if len(paths) != 1:
            print("ERROR: single-file mode takes exactly one file (use --aggregate for many)",
                  file=sys.stderr)
            return 2
        out = parse_file(paths[0], base=args.base)

    print(json.dumps(out, ensure_ascii=False, indent=2))

    # Exit non-zero if we failed to extract a total — lets callers detect a
    # malformed judge output instead of silently scoring None.
    total = out.get("median_total") if args.aggregate else out.get("total")
    return 0 if total is not None else 1


if __name__ == "__main__":
    raise SystemExit(main())
