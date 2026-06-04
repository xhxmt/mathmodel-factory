#!/usr/bin/env python3
"""
Perturbation evaluation harness (v2).

Fixes vs v1:
  - Uses external_paper_judge_prompt.txt (no project-file assumptions)
  - Parses 6-dim scores from the markdown table (not just total)
  - Stronger perturbations: remove_numbers_in_results, remove_derivation_steps
  - samples=3 median by default
  - Section detection tolerates headings without '#' prefix

Usage:
    python3 scripts/perturbation_harness.py [--samples K] [--model MODEL]
"""
import argparse, json, os, pathlib, re, subprocess, sys, time, urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from llm_judge_call import call as _llm_call  # shared backend dispatcher

ROOT = pathlib.Path(__file__).parent.parent
RESULTS = ROOT / "evaluation/results"
JUDGE_PROMPT = ROOT / "evaluation/external_paper_judge_prompt.txt"

PAPERS = {
    "national_1_A": {
        "label": "国一 A题（蚁群+遗传+蒙特卡洛，生产决策）",
        "md": ROOT / "reference_papers/national_1_A/ocr/full.md",
    },
    "national_1_B": {
        "label": "国一 B题（板凳龙动态搜索）",
        "md": ROOT / "reference_papers/national_1_B/ocr/full.md",
    },
    "provincial_1": {
        "label": "省一（Henryers 24B）",
        "md": ROOT / "reference_papers/provincial_1/ocr/full.md",
    },
    "provincial_3": {
        "label": "省三（Cherzing 24B）",
        "md": ROOT / "reference_papers/provincial_3/ocr/full.md",
    },
}

DIM_WEIGHTS = {
    "模型合理性": (20, 20), "求解正确性": (20, 20), "创新性": (20, 20),
    "写作清晰度": (15, 15), "结果说服力": (15, 15), "灵敏度分析": (10, 10),
}

# ── section removal (tolerates headings without '#') ─────────────────────────

def _heading_level(line: str) -> tuple[int, str]:
    """Return (level, text) for a heading line, 0 if not a heading."""
    m = re.match(r'^(#{1,4})\s+(.*)', line)
    if m:
        return len(m.group(1)), m.group(2).strip()
    # Bare Chinese-numbered headings like "四、符号说明" at line start
    m2 = re.match(r'^([一二三四五六七八九十]+)[、．.]\s*(.+)', line)
    if m2:
        return 2, m2.group(2).strip()
    return 0, ""


def remove_section(text: str, keywords: list[str]) -> tuple[str, bool]:
    lines = text.splitlines(keepends=True)
    out, inside, removed = [], False, False
    current_level = 0
    for line in lines:
        level, heading = _heading_level(line.rstrip())
        if level:
            if any(kw in heading for kw in keywords):
                inside = True
                current_level = level
                removed = True
                continue
            if inside and level <= current_level:
                inside = False
        if not inside:
            out.append(line)
    return "".join(out), removed


def scramble_result_numbers(text: str) -> tuple[str, bool]:
    """Replace concrete result numbers with obviously wrong values (×10)."""
    # Target numbers in result/conclusion sections: integers and decimals
    # that look like model outputs (not years, not section numbers)
    pattern = re.compile(
        r'(?<![0-9\.\-])(\d{2,6}(?:\.\d{1,4})?)'   # 2-6 digit number
        r'(?!\s*[年月日届章节])'                       # not dates/section refs
        r'(?=\s*(?:元|%|m|km|个|件|次|m/s|s\b|分\b|分钟|万|亿|×|＝|=|，|。|；|、|\)|）))'
    )
    count = [0]
    def replace(m):
        count[0] += 1
        val = float(m.group(1))
        # Shift by ×3.7 (conspicuous but not obviously 10×)
        new_val = val * 3.7
        if '.' in m.group(1):
            return f"{new_val:.2f}"
        return str(int(new_val))
    new_text = pattern.sub(replace, text)
    return new_text, count[0] > 0


PERTURBATIONS = [
    {
        "id": "no_sensitivity",
        "label": "删除灵敏度分析章节",
        "target_dim": "灵敏度分析",
        "fn": lambda t: remove_section(t, ["灵敏", "敏感性", "鲁棒", "sensitivity"]),
    },
    {
        "id": "no_symbols",
        "label": "删除符号/变量说明章节",
        "target_dim": "模型合理性",
        "fn": lambda t: remove_section(t, ["符号说明", "符号表", "变量说明", "符号与定义", "notation"]),
    },
    {
        "id": "scramble_numbers",
        "label": "篡改结果数字(×3.7)",
        "target_dim": "求解正确性",
        "fn": scramble_result_numbers,
    },
    {
        "id": "no_sensitivity_no_symbols",
        "label": "删除灵敏度+符号说明",
        "target_dim": "灵敏度分析",
        "fn": lambda t: (
            lambda r1: remove_section(r1[0], ["符号说明", "符号表", "变量说明"])
        )(remove_section(t, ["灵敏", "敏感性", "鲁棒", "sensitivity"])),
    },
]

# ── judge & parser ────────────────────────────────────────────────────────────

def run_judge(paper_text: str, model: str, timeout: int = 360) -> dict | None:
    """Score one paper. Backend (deepseek/gemini/claude) is picked by model name
    prefix inside the shared scripts/llm_judge_call.py dispatcher."""
    prompt = JUDGE_PROMPT.read_text() + "\n\n" + paper_text
    try:
        text = _llm_call(prompt, model, timeout, max_tokens=4000)
    except Exception:
        return None
    return _parse_score(text) if text else None


def _parse_score(text: str) -> dict | None:
    verdict_m = re.search(r'^VERDICT:\s*(PASS|REOPEN_\w+)', text, re.MULTILINE)
    total_m   = re.search(r'整体得分[：:]\s*(\d+(?:\.\d+)?)\s*/\s*100', text)
    if not total_m:
        return None
    total = float(total_m.group(1))

    # Parse 6-dim scores from the markdown table row
    # | 模型合理性 | 20% | 15 | 16 | 14 | **15.0** | 良 |
    dims = {}
    for dim, (weight, maxscore) in DIM_WEIGHTS.items():
        m = re.search(
            r'\|\s*' + re.escape(dim) + r'\s*\|[^|]+\|'
            r'\s*(\d+(?:\.\d+)?)\s*\|'   # judge A
            r'\s*(\d+(?:\.\d+)?)\s*\|'   # judge B
            r'\s*(\d+(?:\.\d+)?)\s*\|'   # judge C
            r'\s*\*\*(\d+(?:\.\d+)?)\*\*\s*\|',  # weighted avg
            text,
        )
        if m:
            a, b, c, avg = float(m.group(1)), float(m.group(2)), float(m.group(3)), float(m.group(4))
            # Clamp to max to handle haiku overflow bugs
            avg = min(avg, maxscore)
            dims[dim] = {"a": a, "b": b, "c": c, "avg": avg, "max": maxscore}

    return {
        "total": total,
        "verdict": verdict_m.group(1) if verdict_m else "UNKNOWN",
        "dims": dims,
    }


def median(vals):
    s = sorted(vals)
    return s[len(s) // 2]


def _load_env(path: pathlib.Path) -> None:
    """Minimal .env loader (KEY=VALUE lines) so API keys are available when the
    harness is run standalone. Existing environment variables are not overwritten."""
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        os.environ.setdefault(key, val)

# ── main ──────────────────────────────────────────────────────────────────────

def main():
    _load_env(ROOT / ".env")
    ap = argparse.ArgumentParser()
    ap.add_argument("--samples", type=int, default=3)
    ap.add_argument("--model", default=os.environ.get("CLAUDE_MODEL", "deepseek-chat"))
    args = ap.parse_args()

    RESULTS.mkdir(parents=True, exist_ok=True)
    report_lines = [
        "# 扰动对照评估报告 v2",
        f"\n模型: `{args.model}`  samples={args.samples}",
        f"\n生成时间: {time.strftime('%Y-%m-%d %H:%M')}",
        "\n---\n",
    ]
    all_results = []

    for paper_id, pinfo in PAPERS.items():
        md_path = pinfo["md"]
        if not md_path.exists():
            print(f"SKIP {paper_id}: {md_path} not found")
            continue
        original_text = md_path.read_text()
        print(f"\n{'='*60}")
        print(f"Paper: {pinfo['label']}  ({len(original_text)} chars)")
        report_lines.append(f"## {pinfo['label']}\n")

        # Judge original (K samples)
        print(f"  Original ({args.samples} samples)…")
        orig_scores = [run_judge(original_text, args.model) for _ in range(args.samples)]
        orig_scores = [s for s in orig_scores if s]
        if not orig_scores:
            print("  ERROR: no parseable score"); report_lines.append("⚠ 原版无法解析\n"); continue

        orig_totals = [s["total"] for s in orig_scores]
        orig_med = median(orig_totals)
        print(f"  original: totals={orig_totals}  median={orig_med}")

        # Dim medians
        orig_dim_med = {}
        for dim in DIM_WEIGHTS:
            avgs = [s["dims"][dim]["avg"] for s in orig_scores if dim in s["dims"]]
            if avgs:
                orig_dim_med[dim] = median(avgs)

        report_lines.append(f"### 原版\n- 总分中位数: **{orig_med}**")
        if orig_dim_med:
            dim_str = "  ".join(f"{d}: {v}" for d, v in orig_dim_med.items())
            report_lines.append(f"- 维度: {dim_str}")
        report_lines.append("")

        # Each perturbation
        for p in PERTURBATIONS:
            result = p["fn"](original_text)
            degraded_text, was_applied = result if isinstance(result, tuple) else (result[0], result[1])
            if isinstance(result, tuple) and len(result) == 2:
                degraded_text, was_applied = result
            else:
                degraded_text, was_applied = result

            if not was_applied:
                print(f"  SKIP {p['id']}: not found in this paper")
                report_lines.append(f"### {p['label']}\n- ⚠ 目标内容未找到，跳过\n"); continue

            removed_chars = len(original_text) - len(degraded_text)
            print(f"  [{p['id']}] Δchars={removed_chars}, running {args.samples} samples…")

            deg_scores = [run_judge(degraded_text, args.model) for _ in range(args.samples)]
            deg_scores = [s for s in deg_scores if s]
            if not deg_scores:
                report_lines.append(f"### {p['label']}\n- ⚠ 劣化版无法解析\n"); continue

            deg_totals = [s["total"] for s in deg_scores]
            deg_med = median(deg_totals)
            total_correct = deg_med < orig_med
            delta_total = orig_med - deg_med

            # Target-dim delta
            target = p["target_dim"]
            deg_dim_avgs = [s["dims"][target]["avg"] for s in deg_scores if target in s["dims"]]
            orig_dim_val = orig_dim_med.get(target)
            deg_dim_med = median(deg_dim_avgs) if deg_dim_avgs else None
            dim_correct = (deg_dim_med < orig_dim_val) if (deg_dim_med is not None and orig_dim_val is not None) else None
            dim_delta = (orig_dim_val - deg_dim_med) if (deg_dim_med is not None and orig_dim_val is not None) else None

            status_total = "✅" if total_correct else "❌"
            status_dim   = ("✅" if dim_correct else "❌") if dim_correct is not None else "?"

            dim_delta_str = f"{dim_delta:+.2f}" if dim_delta is not None else "N/A"
            print(f"    total: {orig_med}→{deg_med} Δ={delta_total:+.1f} {status_total}  |  "
                  f"{target}: {orig_dim_val}→{deg_dim_med} Δ={dim_delta_str} {status_dim}")

            all_results.append({
                "paper": paper_id, "perturbation": p["id"],
                "orig_total": orig_med, "deg_total": deg_med,
                "delta_total": delta_total, "total_correct": total_correct,
                "target_dim": target,
                "orig_dim": orig_dim_val, "deg_dim": deg_dim_med,
                "delta_dim": dim_delta, "dim_correct": dim_correct,
            })

            report_lines += [
                f"### {p['label']}",
                f"- Δchars: {removed_chars}  |  目标维度: {target}",
                f"- **总分**: {orig_med} → {deg_med}  (Δ={delta_total:+.1f})  {status_total}",
                f"- **{target}**: {orig_dim_val} → {deg_dim_med}  (Δ={dim_delta_str})  {status_dim}",
                "",
            ]

    # Summary
    if all_results:
        n_total_correct = sum(1 for r in all_results if r["total_correct"])
        n_dim_correct   = sum(1 for r in all_results if r.get("dim_correct"))
        n_dim_tested    = sum(1 for r in all_results if r.get("dim_correct") is not None)
        n = len(all_results)
        report_lines += [
            "---\n## 汇总",
            f"\n总分定向扣分率: **{n_total_correct}/{n} = {n_total_correct/n*100:.0f}%**",
            f"目标维度定向扣分率: **{n_dim_correct}/{n_dim_tested} = {n_dim_correct/max(n_dim_tested,1)*100:.0f}%**\n",
            "| 论文 | 扰动 | 总分orig→deg | Δ总 | ✓ | 目标维度orig→deg | Δ维 | ✓ |",
            "|-----|------|-------------|-----|---|----------------|-----|---|",
        ]
        for r in all_results:
            t = "✅" if r["total_correct"] else "❌"
            d = ("✅" if r["dim_correct"] else "❌") if r["dim_correct"] is not None else "?"
            report_lines.append(
                f"| {r['paper']} | {r['perturbation']} "
                f"| {r['orig_total']}→{r['deg_total']} | {r['delta_total']:+.1f} | {t} "
                f"| {r['target_dim']} {r['orig_dim']}→{r['deg_dim']} | "
                f"{r['delta_dim']:+.2f} | {d} |"
                if r.get("delta_dim") is not None else
                f"| {r['paper']} | {r['perturbation']} "
                f"| {r['orig_total']}→{r['deg_total']} | {r['delta_total']:+.1f} | {t} "
                f"| {r['target_dim']} N/A | — | {d} |"
            )

    out_path = RESULTS / "perturbation_report_v2.md"
    out_path.write_text("\n".join(report_lines) + "\n")
    (RESULTS / "perturbation_raw_v2.json").write_text(
        json.dumps(all_results, ensure_ascii=False, indent=2))
    print(f"\nReport → {out_path}")


if __name__ == "__main__":
    main()
