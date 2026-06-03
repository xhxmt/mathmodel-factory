#!/usr/bin/env python3
"""
Perturbation evaluation harness.

For each reference paper, generate targeted degraded variants, run the
external judge on each, and report whether the judge correctly scores
degraded < original on the targeted dimension.

Usage:
    python3 scripts/perturbation_harness.py [--samples K] [--model MODEL]

Output: evaluation/results/perturbation_report.md
"""
import argparse, json, os, pathlib, re, subprocess, sys, tempfile, time

ROOT = pathlib.Path(__file__).parent.parent
RESULTS = ROOT / "evaluation/results"
JUDGE_PROMPT = ROOT / "evaluation/llm_judge_prompt.txt"
PARSE_SCRIPT = ROOT / "scripts/parse_judge_score.py"

PAPERS = {
    "provincial_1": {
        "label": "省一（Henryers 24B）",
        "md": ROOT / "reference_papers/provincial_1/ocr/full.md",
    },
    "provincial_3": {
        "label": "省三（Cherzing 24B）",
        "md": ROOT / "reference_papers/provincial_3/ocr/full.md",
    },
}

# ── perturbation functions ────────────────────────────────────────────────────

def remove_section(text: str, keywords: list[str]) -> tuple[str, bool]:
    """Remove markdown sections whose heading contains any keyword."""
    lines = text.splitlines(keepends=True)
    out, inside, removed = [], False, False
    current_level = 0
    for line in lines:
        m = re.match(r'^(#{1,4})\s', line)
        if m:
            level = len(m.group(1))
            heading = line.strip().lstrip('#').strip()
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


PERTURBATIONS = [
    {
        "id": "no_sensitivity",
        "label": "删除灵敏度分析",
        "target_dim": "灵敏度分析",
        "fn": lambda t: remove_section(t, ["灵敏", "敏感性", "sensitivity", "Sensitivity"]),
    },
    {
        "id": "no_symbols",
        "label": "删除符号说明",
        "target_dim": "模型合理性",
        "fn": lambda t: remove_section(t, ["符号说明", "符号表", "变量说明", "notation"]),
    },
    {
        "id": "no_sensitivity_no_symbols",
        "label": "删除灵敏度+符号说明",
        "target_dim": "灵敏度分析",
        "fn": lambda t: (
            lambda r1: remove_section(r1[0], ["符号说明", "符号表", "变量说明", "notation"])
        )(remove_section(t, ["灵敏", "敏感性", "sensitivity", "Sensitivity"])),
    },
]

# ── judge call ────────────────────────────────────────────────────────────────

def run_judge(paper_text: str, model: str, timeout: int = 360) -> dict | None:
    """Call claude -p with the judge prompt and paper text; return parsed score dict."""
    prompt_template = JUDGE_PROMPT.read_text()
    full_prompt = prompt_template + "\n\n---\n\n## 待评论文（全文 Markdown）\n\n" + paper_text

    cmd = [
        "claude", "-p",
        "--dangerously-skip-permissions", "--strict-mcp-config",
        "--model", model,
    ]
    try:
        result = subprocess.run(
            cmd, input=full_prompt, capture_output=True, text=True, timeout=timeout
        )
        output = result.stdout
    except subprocess.TimeoutExpired:
        return None

    # parse via parse_judge_score.py logic (inline subset)
    return _parse_score(output)


def _parse_score(text: str) -> dict | None:
    """Extract VERDICT + 整体得分 from judge output."""
    verdict_m = re.search(r'VERDICT:\s*(PASS|REOPEN_\w+)', text)
    score_m   = re.search(r'整体得分[：:]\s*(\d+(?:\.\d+)?)\s*/\s*100', text)
    if not score_m:
        score_m = re.search(r'总分[：:]\s*(\d+(?:\.\d+)?)', text)
    dims = {}
    for m in re.finditer(r'([^\n｜|]{2,10})[：:]\s*(\d+(?:\.\d+)?)\s*/\s*\d+', text):
        dims[m.group(1).strip()] = float(m.group(2))
    if not score_m:
        return None
    return {
        "total": float(score_m.group(1)),
        "verdict": verdict_m.group(1) if verdict_m else "UNKNOWN",
        "dims": dims,
        "raw": text[:200],
    }

# ── main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--samples", type=int, default=1)
    ap.add_argument("--model", default=os.environ.get("CLAUDE_MODEL", "haiku[1m]"))
    args = ap.parse_args()

    RESULTS.mkdir(parents=True, exist_ok=True)
    report_lines = [
        "# 扰动对照评估报告",
        f"\n模型: `{args.model}`  samples={args.samples}",
        f"\n生成时间: {time.strftime('%Y-%m-%d %H:%M')}",
        "\n---\n",
    ]

    all_results = []

    for paper_id, paper_info in PAPERS.items():
        md_path = paper_info["md"]
        if not md_path.exists():
            print(f"SKIP {paper_id}: {md_path} not found")
            continue
        original_text = md_path.read_text()
        print(f"\n{'='*60}")
        print(f"Paper: {paper_info['label']}  ({len(original_text)} chars)")

        report_lines.append(f"## {paper_info['label']}\n")

        # Judge original
        print("  Running original…")
        orig_scores = [run_judge(original_text, args.model) for _ in range(args.samples)]
        orig_scores = [s for s in orig_scores if s]
        if not orig_scores:
            print("  ERROR: original judge returned no parseable score")
            report_lines.append("⚠ 原版评委无法解析\n")
            continue
        orig_median = sorted(s["total"] for s in orig_scores)[len(orig_scores)//2]
        print(f"  original median={orig_median}")
        report_lines.append(f"### 原版\n- 中位数得分: **{orig_median}**\n")

        # Each perturbation
        for p in PERTURBATIONS:
            degraded_text, was_removed = p["fn"](original_text)
            if not was_removed:
                print(f"  SKIP {p['id']}: section not found in this paper")
                report_lines.append(f"### {p['label']}\n- ⚠ 目标章节未找到，跳过\n")
                continue

            removed_chars = len(original_text) - len(degraded_text)
            print(f"  Perturbation: {p['label']} (removed {removed_chars} chars)")

            deg_scores = [run_judge(degraded_text, args.model) for _ in range(args.samples)]
            deg_scores = [s for s in deg_scores if s]
            if not deg_scores:
                report_lines.append(f"### {p['label']}\n- ⚠ 评委无法解析\n")
                continue

            deg_median = sorted(s["total"] for s in deg_scores)[len(deg_scores)//2]
            correct = deg_median < orig_median
            delta = orig_median - deg_median
            status = "✅ 正确扣分" if correct else "❌ 未扣分"
            print(f"  {status}: orig={orig_median} deg={deg_median} Δ={delta:+.1f}")

            all_results.append({
                "paper": paper_id, "perturbation": p["id"],
                "orig": orig_median, "degraded": deg_median,
                "delta": delta, "correct": correct,
            })

            report_lines += [
                f"### {p['label']}",
                f"- 删除字符数: {removed_chars}",
                f"- 目标维度: {p['target_dim']}",
                f"- 原版中位数: {orig_median}  →  劣化版中位数: {deg_median}  (Δ={delta:+.1f})",
                f"- 判断: **{status}**\n",
            ]

    # Summary table
    if all_results:
        n_correct = sum(1 for r in all_results if r["correct"])
        n_total = len(all_results)
        accuracy = n_correct / n_total * 100
        report_lines += [
            "---\n## 汇总",
            f"\n定向扣分正确率: **{n_correct}/{n_total} = {accuracy:.0f}%**\n",
            "| 论文 | 扰动 | 原版 | 劣化版 | Δ | 正确? |",
            "|-----|------|------|--------|---|-------|",
        ]
        for r in all_results:
            tick = "✅" if r["correct"] else "❌"
            report_lines.append(
                f"| {r['paper']} | {r['perturbation']} | {r['orig']} | {r['degraded']} | {r['delta']:+.1f} | {tick} |"
            )

    out_path = RESULTS / "perturbation_report.md"
    out_path.write_text("\n".join(report_lines) + "\n")
    print(f"\nReport saved to {out_path}")

    # Also save raw JSON
    (RESULTS / "perturbation_raw.json").write_text(json.dumps(all_results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
