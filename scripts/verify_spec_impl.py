#!/usr/bin/env python3
"""verify_spec_impl.py — 规格–实现对账门禁 (Gate 1 第五道机械门禁)。

背景 (blind 2025A, 第三次同类事故): `model.md §8.1` 承诺预算阶梯
`particles×iterations = 40×80, 80×160, 120×240`、"3 个随机种子复核"、
"正式优化粗扫不大于 0.05 s"，代码实际 `pso_particles=6`、单种子 20260708、
预算阶梯内 `time_step_sec=0.08` —— 承诺打两折且无任何门禁对照。此前
rerun_0706 的 N=150 声明 vs N=51 实跑 (step5-canonical-demo-fallback) 同构。
verify_numbers/invariants/provenance 都不查"承诺 vs 实现"，本脚本补这一层:

  S1  预算承诺:  §8.1 声明的预算阶梯最高档 (particles×iterations 之积) 是
      canonical 求解的预算基准; 全局搜索子问题实际 n_eval < 基准 × ratio
      (默认 0.5) -> BLOCKING。
  S2  种子承诺:  §8.1 声明 "N 个随机种子" 时, canonical 求解必须留下 ≥N 个
      不同种子的证据 (values.json 的 random_seed 各异, 或 provenance.seeds
      数组) -> 不足则 BLOCKING。Step 6 低预算诊断的多种子不算 (那是灵敏度,
      不是 canonical 复核)。
  S3  时间步承诺: model.md 声明 "(正式)优化...不大于 X s" 的 Δt 上限时,
      models/**/*.py 中出现的 time_step/dt 常量 > X -> BLOCKING。
  S4  上界/gap 承诺: §8.1 声明松弛上界/gap 时, values.json 必须带非空
      upper_bound 且 (gap_pct 或 mip_gap 或 gap) -> 缺失则 BLOCKING。

SKIP 条件 (legacy 安全): 无 model.md、model.md 无 §8.1 结构分析节、或无
results/p*/values.json 树, 一律 SKIP (exit 0)。

Usage:
    python3 verify_spec_impl.py <project_dir>

Exit code: 0 = PASS 或 SKIP; 1 = 存在 BLOCKING。
"""

from __future__ import annotations

import glob
import json
import os
import re
import sys

# S1: 实际预算低于承诺最高档的这个比例即 BLOCKING。
_PROMISE_RATIO = float(os.environ.get('SPEC_IMPL_BUDGET_RATIO', '0.5'))

# 全局搜索求解器提示 (与 verify_provenance 一致, 决定 S1 是否适用于该子问题)。
_GLOBAL_SEARCH_HINTS = re.compile(
    r'\b(de|differential[_\s-]?evolution|pso|particle[_\s-]?swarm|ga|'
    r'genetic|simulated[_\s-]?annealing|sa|basin|multistart|multi[_\s-]?start)\b',
    re.IGNORECASE,
)


def _load_json(path):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


def _read_text(path):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return f.read()
    except Exception:
        return None


def _section_8_1(model_text):
    """截取 §8.1 结构分析节 (到下一个 ## 或 ### 标题为止)。找不到返回 None。"""
    m = re.search(r'^#{2,3}\s*8\.1\s.*$', model_text, re.MULTILINE)
    if not m:
        return None
    start = m.end()
    nxt = re.search(r'^#{2,3}\s', model_text[start:], re.MULTILINE)
    return model_text[start:start + nxt.start()] if nxt else model_text[start:]


def promised_budget(sec):
    """S1: 从 §8.1 抽预算阶梯承诺, 返回最高档 evals 基准 (int) 或 None。

    识别 `particles×iterations = 40×80, 80×160, 120×240` 风格 (× 或 x/*)。"""
    pairs = re.findall(r'(\d+)\s*[×x\*]\s*(\d+)', sec)
    products = [int(a) * int(b) for a, b in pairs if int(a) * int(b) >= 100]
    return max(products) if products else None


def promised_problem_evals(sec):
    """Parse explicit per-problem actual evaluation budgets from §8.1.

    Preferred current format: ``问题 2 n_eval=544``. Unlike the legacy
    particles×iterations promise, these values are exact provenance claims and
    are reconciled one-to-one with results/problemN/values.json.
    """
    pairs = re.findall(
        r'问题\s*([1-9]\d*).{0,48}?\bn_eval\s*[=:：]\s*(\d+)',
        sec,
        re.IGNORECASE,
    )
    return {int(problem): int(evals) for problem, evals in pairs}


def promised_seeds(sec):
    """S2: 抽 "N 个随机种子" 承诺, 返回 N 或 None。"""
    m = re.search(r'(\d+)\s*个随机种子', sec)
    return int(m.group(1)) if m else None


def promised_dt_cap(model_text):
    """S3: 抽 "(正式)优化...不大于 X s" 的 Δt 上限承诺。全文找, 返回 float 或 None。

    只认与时间步语境同句出现的上限 (Δt / 时间步 / 粗扫), 避免误抓其他不等式。"""
    for line in model_text.splitlines():
        if not re.search(r'\\Delta\s*t|时间步|粗扫', line):
            continue
        m = re.search(r'不大于\s*\$?\s*([\d.]+)', line)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                continue
    return None


def promises_gap(sec):
    """S4: §8.1 是否承诺了松弛上界 / gap 自证。"""
    return bool(re.search(r'松弛上界|LP\s*松弛|\bgap\b', sec, re.IGNORECASE))


def _values_files(project_dir):
    results = os.path.join(project_dir, 'results')
    out = []
    if os.path.isdir(results):
        out.extend(glob.glob(os.path.join(results, 'p*', 'values.json')))
        out.extend(glob.glob(os.path.join(results, 'problem*', 'values.json')))
    return sorted(set(out))


def _is_global_search(v):
    hay = ' '.join(str(v.get(k, '')) for k in ('solver', 'status'))
    hay += ' ' + str((v.get('provenance') or {}).get('solver', ''))
    return bool(_GLOBAL_SEARCH_HINTS.search(hay))


def _actual_evals(v):
    budget = (v.get('provenance') or {}).get('budget') or {}
    for k in ('n_eval', 'nfev', 'evaluations', 'func_evals'):
        if k in budget and budget[k] is not None:
            return budget[k]
    return None


def _actual_dt_constants(project_dir):
    """S3: 扫描 models/**/*.py 的 time_step/dt 数值常量。返回 {值: [文件:行]}。

    只抓明确命名的时间步赋值/实参, 不抓裸 dt (太泛)。"""
    hits = {}
    pat = re.compile(r'\b(time_step(?:_sec)?|dt_sec|time_grid_step)\s*[=:]\s*([\d.]+)')
    for path in glob.glob(os.path.join(project_dir, 'models', '**', '*.py'), recursive=True):
        text = _read_text(path)
        if text is None:
            continue
        rel = os.path.relpath(path, project_dir)
        for i, line in enumerate(text.splitlines(), 1):
            if 'demo' in rel or '校验' in line or 'P1' in line:
                pass  # 不豁免: 承诺针对"正式优化", 但 demo 文件仍可能被 canonical 调用
            for m in pat.finditer(line):
                try:
                    val = float(m.group(2))
                except ValueError:
                    continue
                hits.setdefault(val, []).append(f'{rel}:{i}')
    return hits


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    project_dir = sys.argv[1]
    if not os.path.isdir(project_dir):
        print(f'ERROR: {project_dir} not found')
        return 3

    print('=== Spec-Implementation Reconciliation Gate (S1-S4) ===')
    print(f'Project: {project_dir}')
    print()

    model_text = _read_text(os.path.join(project_dir, 'model.md'))
    if model_text is None:
        print('VERDICT: SKIP (no model.md)')
        return 0
    sec = _section_8_1(model_text)
    if sec is None:
        print('VERDICT: SKIP (model.md 无 §8.1 结构分析节 — legacy 项目)')
        return 0
    vfiles = _values_files(project_dir)
    if not vfiles:
        print('VERDICT: SKIP (无 results/p*/values.json — 求解未落盘)')
        return 0

    findings = []  # (severity, detail)

    # ---- S1: 预算承诺 ----
    problem_eval_promises = promised_problem_evals(sec)
    budget_cap = promised_budget(sec)
    if problem_eval_promises:
        print(f'S1 逐问题 n_eval 承诺: {problem_eval_promises}')
        seen_problems = set()
        for path in vfiles:
            v = _load_json(path)
            if v is None or not _is_global_search(v):
                continue
            match = re.search(r'problem(\d+)', os.path.basename(os.path.dirname(path)))
            if not match:
                continue
            problem = int(match.group(1))
            actual = _actual_evals(v)
            promised = problem_eval_promises.get(problem)
            if promised is None:
                findings.append(('WARN', f'S1: 问题 {problem} 为全局搜索但 §8.1 未声明 n_eval'))
                continue
            seen_problems.add(problem)
            if actual is None:
                findings.append(('BLOCKING', f'S1: {os.path.relpath(path, project_dir)} 无 n_eval 记录'))
            elif int(actual) != promised:
                findings.append((
                    'BLOCKING',
                    f'S1: 问题 {problem} §8.1 声明 n_eval={promised}, 实际 n_eval={actual}',
                ))
        unused = sorted(set(problem_eval_promises) - seen_problems)
        for problem in unused:
            findings.append(('WARN', f'S1: §8.1 声明问题 {problem} n_eval, 但未找到对应全局搜索结果'))
    elif budget_cap is None:
        findings.append(('WARN', 'S1: §8.1 未声明可解析的逐问题 n_eval 或预算阶梯 '
                         '(particles×iterations) — 无法对账'))
    else:
        floor = int(budget_cap * _PROMISE_RATIO)
        print(f'S1 预算承诺: 最高档 {budget_cap} evals, 对账下限 {floor} (ratio={_PROMISE_RATIO})')
        for path in vfiles:
            v = _load_json(path)
            if v is None or not _is_global_search(v):
                continue
            rel = os.path.relpath(path, project_dir)
            actual = _actual_evals(v)
            if actual is None:
                findings.append(('BLOCKING', f'S1: {rel} 全局搜索无 n_eval 记录, 无法对账承诺预算'))
            elif float(actual) < floor:
                findings.append(('BLOCKING',
                                 f'S1: {rel} 实际 n_eval={actual} < 承诺预算下限 {floor} '
                                 f'(§8.1 承诺最高档 {budget_cap}, 实现打了 '
                                 f'{float(actual) / budget_cap:.0%})'))

    # ---- S2: 种子承诺 ----
    n_seeds_promised = promised_seeds(sec)
    if n_seeds_promised:
        seeds = set()
        for path in vfiles:
            v = _load_json(path) or {}
            s = v.get('random_seed')
            if s is not None:
                seeds.add(s)
            for extra in (v.get('provenance') or {}).get('seeds') or []:
                seeds.add(extra)
        print(f'S2 种子承诺: {n_seeds_promised} 个; canonical 证据 {sorted(seeds)}')
        if len(seeds) < n_seeds_promised:
            findings.append(('BLOCKING',
                             f'S2: §8.1 承诺 {n_seeds_promised} 个随机种子复核, canonical 只有 '
                             f'{len(seeds)} 个 ({sorted(seeds)}); Step 6 低预算诊断不算 canonical 复核 '
                             f'(需 values.json 各异 random_seed 或 provenance.seeds 数组)'))

    # ---- S3: 时间步承诺 ----
    dt_cap = promised_dt_cap(model_text)
    if dt_cap is not None:
        hits = _actual_dt_constants(project_dir)
        over = {v: locs for v, locs in hits.items() if v > dt_cap + 1e-12}
        print(f'S3 时间步承诺: Δt ≤ {dt_cap}; 代码常量 {sorted(hits)}')
        for val, locs in sorted(over.items()):
            findings.append(('BLOCKING',
                             f'S3: 代码时间步 {val} > 承诺上限 {dt_cap} @ {", ".join(locs[:3])}'
                             + (f' 等 {len(locs)} 处' if len(locs) > 3 else '')))

    # ---- S4: 上界/gap 承诺 ----
    if promises_gap(sec):
        for path in vfiles:
            v = _load_json(path) or {}
            if not _is_global_search(v):
                continue
            rel = os.path.relpath(path, project_dir)
            has_ub = v.get('upper_bound') is not None
            has_gap = any(v.get(k) is not None for k in ('gap_pct', 'mip_gap', 'gap'))
            if not (has_ub and has_gap):
                findings.append(('BLOCKING',
                                 f'S4: {rel} 承诺了松弛上界/gap 自证但缺 '
                                 f'{"upper_bound " if not has_ub else ""}'
                                 f'{"gap 字段" if not has_gap else ""}'.strip()))

    # ---- 汇总 ----
    print()
    blocking = [d for s, d in findings if s == 'BLOCKING']
    warns = [d for s, d in findings if s == 'WARN']
    for d in blocking:
        print(f'[BLOCKING] {d}')
    for d in warns:
        print(f'[WARN] {d}')
    print()
    print(f'SPEC_IMPL_BLOCKING={len(blocking)}')
    print(f'SPEC_IMPL_WARN={len(warns)}')
    if blocking:
        print('VERDICT: FAIL (§8.1 承诺与实现不符 — 要么把实现补到承诺水平, '
              '要么在 §8.1 用证据下修承诺并让 critic 复核)')
        return 1
    print('VERDICT: PASS')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
