#!/usr/bin/env python3
"""check_sensitivity_coverage.py — Step 6 灵敏度覆盖自检。

把 step6 prompt §5「灵敏度分析最小覆盖自检（硬门禁）」里可机检的部分自动化，
供 Step 6 agent 在收尾时自查（角色同 verify_symbols.py 之于 Step 10），减少
「灵敏度做得不够」被评委扣分（消融实验里 no_judge 案例曾在灵敏度维度 -1.0 分）。

读取项目目录下：
  - assumption_ledger.md   → 假设总数 / OPEN 数 / PROTECTED 数 / 升级率
  - sensitivity_report.md  → 行数 / sweep 数 / 是否含 tornado / 是否含 scenario
  - figures/sensitivity_*.{pdf,png} → 图数量 / tornado 图 / scenario 图

判定：
  FAIL   —— 缺 tornado 或缺 scenario 图（STEPS.md 强制两者都要）
  WARNING—— OPEN 假设未清零 / 升级率 < 阈值 / sweep 数不足 / 报告过短 / 图不足
  PASS   —— 以上皆满足

退出码：0=PASS，1=WARNING，2=FAIL。这是 agent 自检工具，不被 runner 当步骤门禁。

用法：
  check_sensitivity_coverage.py <project_dir> [--min-sweeps N] [--min-report-lines N]
                                              [--min-upgrade-rate F] [--min-figures N]
"""
import argparse
import glob
import os
import re
import sys

STATES = ("INHERITED", "OPEN", "CONFIRMED", "RELAXED", "RESOLVED")
UPGRADED = ("CONFIRMED", "RELAXED", "RESOLVED")
_ID_RE = re.compile(r"^[A-Za-z]{1,4}\d+[a-z]?$")


def _parse_ledger(path):
    """解析 assumption_ledger.md 的 markdown 表格。

    返回 (total, open_count, protected_count, upgraded_count, open_ids) 或 None。
    用表头定位「状态」「标签」列；定位不到则回退到倒数第 2 / 倒数第 1 列。
    只统计第一列形如 id（A1 / H2 / AB3a）的真实假设行，避免把依赖说明等误计。
    """
    if not os.path.isfile(path):
        return None
    status_idx = tag_idx = None
    rows = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            s = line.strip()
            if not s.startswith("|"):
                continue
            cells = [c.strip() for c in s.strip("|").split("|")]
            if status_idx is None and any("状态" in c for c in cells):
                for i, c in enumerate(cells):
                    if "状态" in c:
                        status_idx = i
                    if "标签" in c or "标记" in c:
                        tag_idx = i
                continue
            if set(s) <= set("|-: "):  # 分隔行 |---|---|
                continue
            rows.append(cells)

    total = open_c = protected = upgraded = 0
    open_ids = []
    for cells in rows:
        if not cells or not _ID_RE.match(cells[0]):
            continue
        si = status_idx if (status_idx is not None and status_idx < len(cells)) else -2
        ti = tag_idx if (tag_idx is not None and tag_idx < len(cells)) else -1
        status_cell = cells[si].upper() if -len(cells) <= si < len(cells) else ""
        tag_cell = cells[ti].upper() if -len(cells) <= ti < len(cells) else ""
        status = next((w for w in STATES if w in status_cell), status_cell)
        total += 1
        if status == "OPEN":
            open_c += 1
            open_ids.append(cells[0])
        if status in UPGRADED:
            upgraded += 1
        if "PROTECTED" in tag_cell:
            protected += 1
    return total, open_c, protected, upgraded, open_ids


def _parse_report(path):
    """返回 (line_count, has_tornado, has_scenario, sweep_count) 或 None。"""
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8", errors="replace") as f:
        text = f.read()
    lines = text.count("\n") + 1
    low = text.lower()
    has_tornado = "tornado" in low
    has_scenario = any(k in text for k in ("scenario", "场景", "对比")) or "compare" in low
    sweeps = len(re.findall(r"results/sensitivity/\S+\.json", text))
    if sweeps == 0:
        sweeps = len(re.findall(r"(?m)^\|\s*s\d+", text))
    return lines, has_tornado, has_scenario, sweeps


def _count_figures(figdir):
    """返回 (n_sensitivity_figs, has_tornado_fig, has_scenario_fig)。"""
    figs = glob.glob(os.path.join(figdir, "sensitivity_*.pdf")) + \
        glob.glob(os.path.join(figdir, "sensitivity_*.png"))
    names = " ".join(os.path.basename(p).lower() for p in figs)
    return len(figs), "tornado" in names, ("scenario" in names or "compare" in names)


def main():
    ap = argparse.ArgumentParser(description="Step 6 灵敏度覆盖自检")
    ap.add_argument("project_dir")
    ap.add_argument("--min-sweeps", type=int, default=3)
    ap.add_argument("--min-report-lines", type=int, default=150)
    ap.add_argument("--min-upgrade-rate", type=float, default=0.6)
    ap.add_argument("--min-figures", type=int, default=2)
    a = ap.parse_args()
    P = a.project_dir

    if not os.path.isdir(P):
        print(f"ERROR: {P} not found", file=sys.stderr)
        sys.exit(3)

    fail, warn = [], []

    ledger = _parse_ledger(os.path.join(P, "assumption_ledger.md"))
    if ledger is None:
        fail.append("assumption_ledger.md 缺失")
        total = open_c = prot = upg = 0
        open_ids = []
    else:
        total, open_c, prot, upg, open_ids = ledger
    rate = (upg / total) if total else 0.0

    report = _parse_report(os.path.join(P, "sensitivity_report.md"))
    if report is None:
        fail.append("sensitivity_report.md 缺失")
        rlines = sweeps = 0
        r_tor = r_scn = False
    else:
        rlines, r_tor, r_scn, sweeps = report

    nfig, fig_tor, fig_scn = _count_figures(os.path.join(P, "figures"))
    has_tornado = r_tor or fig_tor
    has_scenario = r_scn or fig_scn

    print("=== Sensitivity Coverage Self-Check ===")
    print(f"Project: {P}")
    print(f"ASSUMPTIONS_TOTAL     = {total}")
    print(f"ASSUMPTIONS_OPEN      = {open_c}")
    print(f"ASSUMPTIONS_PROTECTED = {prot}")
    print(f"UPGRADE_RATE          = {rate:.2f}")
    print(f"SWEEP_COUNT           = {sweeps}")
    print(f"HAS_TORNADO           = {'yes' if has_tornado else 'no'}")
    print(f"HAS_SCENARIO          = {'yes' if has_scenario else 'no'}")
    print(f"SENS_FIGURES          = {nfig}")
    print(f"REPORT_LINES          = {rlines}")
    print()

    if not has_tornado:
        fail.append("缺 tornado / OAT 图（STEPS.md 强制）")
    if not has_scenario:
        fail.append("缺 scenario-comparison 图（STEPS.md 强制）")
    if open_c > 0:
        warn.append(f"仍有 {open_c} 条 OPEN 假设未处理（应升 CONFIRMED/RELAXED 或说明）: {', '.join(open_ids)}")
    if total and rate < a.min_upgrade_rate:
        warn.append(f"假设升级率 {rate:.2f} < {a.min_upgrade_rate}")
    if sweeps < a.min_sweeps:
        warn.append(f"sweep 数 {sweeps} < {a.min_sweeps}")
    if rlines < a.min_report_lines:
        warn.append(f"sensitivity_report.md {rlines} 行 < {a.min_report_lines}")
    if nfig < a.min_figures:
        warn.append(f"sensitivity 图 {nfig} 张 < {a.min_figures}")

    for i in fail:
        print(f"  ✗ FAIL: {i}")
    for i in warn:
        print(f"  ⚠ WARN: {i}")
    if not fail and not warn:
        print("  ✓ 覆盖完整")
    print()

    if fail:
        print("VERDICT: FAIL")
        sys.exit(2)
    if warn:
        print("VERDICT: WARNING")
        sys.exit(1)
    print("VERDICT: PASS")
    sys.exit(0)


if __name__ == "__main__":
    main()
