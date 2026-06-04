#!/usr/bin/env python3
"""Compare ablation-variant scores against a baseline.

Reads the aggregate eval JSONs produced by evaluation/run_evaluation.sh
(evaluation/results/<base>_eval.json) and prints a table of the cross-comparison
metric `median_recomputed` plus the 6 per-dimension medians, with the delta of
each variant vs the baseline. The dimension with the largest drop per variant is
flagged — that's the capability the ablated component was protecting.

If an aggregate JSON is missing but per-run <base>_eval_run*.md files exist, it
is rebuilt via scripts/parse_judge_score.py --aggregate (no judge re-run).

Usage:
    compare_ablations.py --baseline <base> --variant <base> [--variant <base> ...]
    compare_ablations.py --baseline cumcm2024b_baseline_rep1 \\
        --variant cumcm2024b_no_judge_rep1 --variant cumcm2024b_no_innov_rep1
    compare_ablations.py --baseline A --variant B --json     # machine-readable

A <base> may also be a path to an _eval.json directly.
"""
from __future__ import annotations

import argparse
import glob
import json
import statistics
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "evaluation" / "results"
PARSER = ROOT / "scripts" / "parse_judge_score.py"

# 6 CUMCM rubric dimensions, in canonical order (parse_judge_score.py:44).
DIMS = ("模型合理性", "求解正确性", "创新性", "写作清晰度", "结果说服力", "灵敏度分析")


def resolve_json(base: str) -> Path | None:
    """Locate the aggregate JSON for a base name (or accept a direct path)."""
    p = Path(base)
    if p.is_file() and p.suffix == ".json":
        return p
    cand = RESULTS / f"{base}_eval.json"
    if cand.is_file():
        return cand
    # Try to rebuild from per-run markdown if present.
    runs = sorted(glob.glob(str(RESULTS / f"{base}_eval_run*.md")))
    if runs:
        try:
            out = subprocess.run(
                ["python3", str(PARSER), "--aggregate", "--base", base, *runs],
                capture_output=True, text=True, check=False,
            )
            if out.returncode == 0 and out.stdout.strip():
                cand.write_text(out.stdout, encoding="utf-8")
                return cand
        except Exception:  # noqa: BLE001
            pass
    return None


def dim_medians(agg: dict) -> dict[str, float]:
    """Median per-dimension weighted_mean across the runs in an aggregate."""
    buckets: dict[str, list[float]] = {d: [] for d in DIMS}
    for run in agg.get("runs", []):
        for d, info in (run.get("dims") or {}).items():
            wm = info.get("weighted_mean")
            if d in buckets and isinstance(wm, (int, float)):
                buckets[d].append(float(wm))
    return {d: round(statistics.median(v), 2) for d, v in buckets.items() if v}


def load(base: str) -> dict | None:
    jp = resolve_json(base)
    if not jp:
        print(f"WARN: no eval JSON for '{base}' (looked in {RESULTS})", file=sys.stderr)
        return None
    agg = json.loads(jp.read_text(encoding="utf-8"))
    return {
        "base": base,
        "total": agg.get("median_recomputed"),
        "spread": (agg.get("min_recomputed"), agg.get("max_recomputed")),
        "dims": dim_medians(agg),
        "verdicts": agg.get("verdicts", []),
        "json": str(jp),
    }


def fmt(v) -> str:
    return f"{v:.1f}" if isinstance(v, (int, float)) else "  - "


def fmt_delta(v) -> str:
    if not isinstance(v, (int, float)):
        return "  - "
    return f"{v:+.1f}"


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--baseline", required=True, help="Baseline base name or _eval.json path.")
    ap.add_argument("--variant", action="append", default=[], required=True,
                    help="Ablation-variant base name (repeatable).")
    ap.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    args = ap.parse_args()

    base = load(args.baseline)
    if not base or base["total"] is None:
        print(f"ERROR: baseline '{args.baseline}' has no usable score.", file=sys.stderr)
        return 1
    variants = [v for v in (load(b) for b in args.variant) if v]
    if not variants:
        print("ERROR: no usable variant scores.", file=sys.stderr)
        return 1

    rows = [base, *variants]
    if args.json:
        out = {
            "baseline": base,
            "variants": [
                {
                    **v,
                    "total_delta": (round(v["total"] - base["total"], 2)
                                    if isinstance(v["total"], (int, float)) else None),
                    "dim_deltas": {d: round(v["dims"].get(d, 0) - base["dims"].get(d, 0), 2)
                                   for d in DIMS if d in v["dims"] and d in base["dims"]},
                }
                for v in variants
            ],
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    # Text table.
    short = {d: d[:4] for d in DIMS}
    hdr = f"{'project':<34} {'total':>6}  " + "  ".join(f"{short[d]:>5}" for d in DIMS)
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        cells = "  ".join(f"{fmt(r['dims'].get(d)):>5}" for d in DIMS)
        tag = "  (baseline)" if r is base else ""
        print(f"{r['base']:<34} {fmt(r['total']):>6}  {cells}{tag}")

    print("\nΔ vs baseline (negative = ablation hurt quality):")
    print("-" * len(hdr))
    for v in variants:
        cells = "  ".join(
            f"{fmt_delta(v['dims'].get(d, 0) - base['dims'].get(d)) if d in v['dims'] and d in base['dims'] else '  - ':>5}"
            for d in DIMS)
        tdelta = (v["total"] - base["total"]) if isinstance(v["total"], (int, float)) else None
        # Largest-drop dimension.
        drops = {d: v["dims"][d] - base["dims"][d]
                 for d in DIMS if d in v["dims"] and d in base["dims"]}
        worst = min(drops, key=drops.get) if drops else None
        worst_s = f"  ↓most: {worst} ({drops[worst]:+.1f})" if worst is not None else ""
        print(f"{v['base']:<34} {fmt_delta(tdelta):>6}  {cells}{worst_s}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
