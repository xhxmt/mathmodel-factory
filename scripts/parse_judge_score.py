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

# The 6 CUMCM rubric dimensions (STEPS.md:144, step13_gate2_judge.txt:5-11).
# Keyed by name so we never mis-parse the header/separator/total rows.
DIMENSIONS = (
    "模型合理性",
    "求解正确性",
    "创新性",
    "写作清晰度",
    "结果说服力",
    "灵敏度分析",
)

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
            grade = cells[-1].replace("*", "").strip() or None
            weight_m = re.search(r"(\d+)", cells[1]) if len(cells) > 1 else None
            dims[name] = {
                "weight": int(weight_m.group(1)) if weight_m else None,
                "weighted_mean": weighted,
                "grade": grade,
            }

    # The `整体得分:` line is the canonical total; fall back to the table row.
    total_m = TOTAL_LINE_RE.search(text)
    total = float(total_m.group(1)) if total_m else total_from_table

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
        "grade": table_grade,
        "dims": dims,
        "source_file": str(path),
    }


def aggregate(paths: list[Path], base: str | None = None) -> dict:
    runs = [parse_file(p, base=base) for p in paths]
    totals = [r["total"] for r in runs if isinstance(r["total"], (int, float))]
    agg = {
        "base": base or next((r["base"] for r in runs if r["base"]), None),
        "n": len(runs),
        "n_scored": len(totals),
        "median_total": round(statistics.median(totals), 2) if totals else None,
        "min_total": min(totals) if totals else None,
        "max_total": max(totals) if totals else None,
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
