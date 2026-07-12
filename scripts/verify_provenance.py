#!/usr/bin/env python3
"""求解产物来源与真实性门禁 (B1/B2/B3) — Step 5 完成前判定。

动机 (rerun_0706 事后取证): Step 5 正规求解通道失败后, 一个兜底脚本以"过门禁"
为目标产出了格式合法但并非真求解的 canonical 结果 —— m3 MILP 目录永为 .stub,
P5 为零优化拼装, 初版预算 de-maxiter=3。既有的 verify_numbers / verify_invariants
只校验数字自洽与可追溯, 无法区分"搜索得到的 FEASIBLE"与"拼装得到的 FEASIBLE"。

本门禁从三个角度补这个缺口:

  B1  provenance 对账:  每个 results/p*/values.json 应带 provenance 块声明
      {solver, budget, job_id, repair}。repair=true 或 provenance 缺失/无法对账
      -> 记 REPAIR_FALLBACK (BLOCKING, 不阻断但必须在 Step 16 前清零)。

  B2  预算诊断:  全局搜索类求解器 (DE/PSO/GA...) 的迭代/评估预算低于通用经验线
      -> 记 WARN。固定预算线不能证明最优性；项目若要硬限制，必须在
         quality_contract.json 给出题目级依据和独立证据。

  B3  设计-实现一致性:  model.md 声明的 stream 目录若仍残留 .stub 文件, 说明
      承诺的求解器从未实现 -> BLOCKING FAIL (硬失败)。

用法:
    python3 verify_provenance.py <project_dir> [base_name]

退出码: 0=全通过, 1=存在 BLOCKING (含 stub / REPAIR_FALLBACK / BUDGET_LIMITED),
        2=无法判定 (缺 results 树)。

设计约定:
  - 门禁"软失败"(REPAIR_FALLBACK / BUDGET_LIMITED) 退出码仍为 1, 因为它们必须
    被 Step 16 硬验收拦住; 但输出里区分 BLOCKING vs HARD_FAIL, 便于上层决定是
    触发 consult 还是直接打回。
  - 兼容旧项目: 没有 provenance 块的历史项目会被标 REPAIR_FALLBACK 而非崩溃,
    这正是我们想要的信号 (旧结果来源不可对账)。legacy 项目应在调用层 SKIP。
"""

import json
import os
import re
import sys
import glob


# 需要全局搜索预算下限的求解器族 (名字里出现即触发 B2 检查)。
_GLOBAL_SEARCH_HINTS = re.compile(
    r'\b(de|differential[_\s-]?evolution|pso|particle[_\s-]?swarm|ga|'
    r'genetic|simulated[_\s-]?annealing|sa|basin|multistart|multi[_\s-]?start)\b',
    re.IGNORECASE,
)

# 预算字段可能的键名 (求解脚本作者习惯各异, 尽量宽松匹配)。
_ITER_KEYS = ('maxiter', 'de_maxiter', 'generations', 'gens', 'iters',
              'pso_iters', 'n_iter', 'iterations')
_EVAL_KEYS = ('n_eval', 'nfev', 'evaluations', 'func_evals')

# 通用经验线仅用于诊断，不作为真实性或最优性硬门禁。
_MIN_ITERS = int(os.environ.get('SOLVE_MIN_ITERS', '50'))
_MIN_EVALS = int(os.environ.get('SOLVE_MIN_EVALS', '50000'))

# A1 (blind 2025A): 评估预算下限必须随决策维度增长, 否则 4 维和 12 维用同一阈值,
# 高维子问题在低预算下静默陷局部峰 (blind P4 12 维 21744 evals -> FY3 死弹,
# P5 45+ 维 -> 7/15 死弹, 却因旧 _MIN_EVALS=5e4 从未真正触发: _budget_from_values
# 命中 iters 就 return, evals 分支是死代码).  维度感知下限:
#     floor(n_vars) = max(_MIN_EVALS_BASE, _EVALS_PER_DIM2 * n_vars^2)
# 经 blind 数据标定: 4 维 -> max(2000, 250*16)=4000 (P2 480 会被抓, 属实欠搜);
# 12 维 -> 250*144=36000 (P4 21744 触发); 45 维 -> 巨大 (P5 触发).
_MIN_EVALS_BASE = int(os.environ.get('SOLVE_MIN_EVALS_BASE', '2000'))
_EVALS_PER_DIM2 = int(os.environ.get('SOLVE_EVALS_PER_DIM2', '250'))


def _load_json(path):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


def _find_values_files(project_dir):
    results = os.path.join(project_dir, 'results')
    out = []
    if os.path.isdir(results):
        out.extend(sorted(glob.glob(os.path.join(results, 'p*', 'values.json'))))
        out.extend(sorted(glob.glob(os.path.join(results, 'problem*', 'values.json'))))
    return sorted(set(out))


def _declared_stream_dirs(project_dir):
    """从 model.md 粗略抽取声明用到的 stream 目录 (m<N>_<short>)。

    只用于给 stub 检测提供上下文; 找不到就退回到扫描整个 models/。"""
    model_md = os.path.join(project_dir, 'model.md')
    streams = set()
    if os.path.isfile(model_md):
        try:
            txt = open(model_md, 'r', encoding='utf-8').read()
            for m in re.findall(r'\bm(\d+)_[a-z0-9]+', txt):
                streams.add(m)
        except Exception:
            pass
    return streams


def check_stub_residue(project_dir):
    """B3: models/ 下任何 .stub 残留都是设计-实现断裂。"""
    models = os.path.join(project_dir, 'models')
    if not os.path.isdir(models):
        return []
    stubs = glob.glob(os.path.join(models, '**', '*.stub'), recursive=True)
    return [os.path.relpath(s, project_dir) for s in sorted(stubs)]


def _budget_from_values(v):
    """从 values.json 提取 (iters, evals)。任一缺失为 None。

    A1: 旧实现遇 iters 就 return, 导致 evals 下限永不触发 (死代码).  现在两者
    都提取并各自校验 — iters 证明"真跑过", evals(维度感知)证明"搜得够充分".
    """
    prov = v.get('provenance') or {}
    budget = prov.get('budget') or {}
    iters = evals = None
    for k in _ITER_KEYS:
        if k in budget and budget[k] is not None:
            iters = budget[k]
            break
    for k in _EVAL_KEYS:
        if k in budget and budget[k] is not None:
            evals = budget[k]
            break
    # 回退: 顶层偶尔直接放了迭代数
    if iters is None:
        for k in _ITER_KEYS:
            if k in v and v[k] is not None:
                iters = v[k]
                break
    return (iters, evals)


def _n_vars_from_values(v):
    """决策维度: 优先 n_vars; 否则从 decision.smokes 数 * 每弹 4 参数估计。

    每枚烟幕弹的连续决策是 (theta, v, t_drop, tau) 共 4 维 (P1 固定策略 n_vars=0
    不参与全局搜索, 不会走到这里)。找不到就返回 None (回退到基础下限)。"""
    n = v.get('n_vars')
    if isinstance(n, (int, float)) and n > 0:
        return int(n)
    try:
        smokes = (v.get('decision') or {}).get('smokes') or []
        if smokes:
            return 4 * len(smokes)
    except Exception:
        pass
    return None


def _min_evals_for_dim(n_vars):
    """维度感知评估下限。n_vars=None -> 基础下限。"""
    if not n_vars or n_vars <= 0:
        return _MIN_EVALS_BASE
    return max(_MIN_EVALS_BASE, _EVALS_PER_DIM2 * n_vars * n_vars)


def _solver_needs_budget(v):
    """该子问题是否用了全局搜索族求解器 (决定是否施加 B2)。"""
    hay = ' '.join(str(v.get(k, '')) for k in (
        'canonical_method', 'search_method', 'solver', 'status'))
    prov = v.get('provenance') or {}
    hay += ' ' + str(prov.get('solver', ''))
    return bool(_GLOBAL_SEARCH_HINTS.search(hay))


def check_values(project_dir):
    """B1 + B2: 遍历 values.json, 返回 findings 列表。"""
    findings = []
    files = _find_values_files(project_dir)
    project_name = os.path.basename(os.path.abspath(project_dir))
    if not files:
        return findings, 0

    for path in files:
        rel = os.path.relpath(path, project_dir)
        v = _load_json(path)
        if v is None:
            findings.append(('HARD_FAIL', rel, 'values.json 无法解析'))
            continue

        declared_project = v.get('project')
        if declared_project and declared_project != project_name:
            findings.append((
                'HARD_FAIL',
                rel,
                f'跨项目结果污染: project={declared_project}, 当前项目={project_name}',
            ))

        prov = v.get('provenance')
        # B1: provenance 缺失 -> 来源不可对账
        if not isinstance(prov, dict):
            findings.append(('REPAIR_FALLBACK', rel,
                             'provenance 块缺失 (无 solver/budget/job_id, 来源不可对账)'))
        else:
            if prov.get('repair') is True:
                findings.append(('REPAIR_FALLBACK', rel,
                                 f"provenance.repair=true (solver={prov.get('solver','?')})"))
            job_id = prov.get('job_id')
            if job_id:
                result_dir = os.path.dirname(path)
                jobid_path = os.path.join(result_dir, 'jobid.txt')
                if os.path.isfile(jobid_path):
                    try:
                        recorded_job_id = open(jobid_path, 'r', encoding='utf-8').read().strip()
                    except OSError:
                        recorded_job_id = ''
                    if recorded_job_id != str(job_id):
                        findings.append((
                            'HARD_FAIL',
                            rel,
                            f'jobid.txt={recorded_job_id or "MISSING"} 与 provenance.job_id={job_id} 不一致',
                        ))

                solver_log = os.path.join(result_dir, 'solver.log')
                if os.path.isfile(solver_log):
                    evidence = _load_json(solver_log)
                    if not isinstance(evidence, dict):
                        findings.append((
                            'HARD_FAIL',
                            rel,
                            'solver.log 不是单一结构化 JSON 证据，可能混入旧运行输出',
                        ))
                    elif str(evidence.get('job_id') or '') != str(job_id):
                        findings.append((
                            'HARD_FAIL',
                            rel,
                            f'solver.log.job_id={evidence.get("job_id") or "MISSING"} '
                            f'与 provenance.job_id={job_id} 不一致',
                        ))

                meta = os.path.join(project_dir, 'run_state', 'solver_jobs', f'{job_id}.meta')
                meta_global = os.path.join(os.path.dirname(os.path.dirname(
                    os.path.abspath(__file__))), 'run_state', 'solver_jobs', f'{job_id}.meta')
                if not (os.path.exists(meta) or os.path.exists(meta_global)):
                    findings.append(('REPAIR_FALLBACK', rel,
                                     f'provenance.job_id={job_id} 无对应 solver_jobs/*.meta'))

        # B2: 全局搜索族的预算下限 (A1: iters + 维度感知 evals 都校验)
        if _solver_needs_budget(v):
            iters, evals = _budget_from_values(v)
            n_vars = _n_vars_from_values(v)
            min_evals = _min_evals_for_dim(n_vars)
            if iters is None and evals is None:
                findings.append(('WARN', rel,
                                 '全局搜索求解器未记录迭代/评估预算 (无法证明真跑过)'))
            else:
                if iters is not None:
                    try:
                        if float(iters) < _MIN_ITERS:
                            findings.append(('WARN', rel,
                                             f'迭代预算 {iters} < 下限 {_MIN_ITERS}'))
                    except (TypeError, ValueError):
                        pass
                if evals is not None:
                    try:
                        if float(evals) < min_evals:
                            dimnote = f'{n_vars} 维' if n_vars else '维度未知'
                            findings.append(('WARN', rel,
                                             f'评估预算 {evals} < 维度感知下限 {min_evals} '
                                             f'({dimnote}; 通用经验线，仅作异常提示)'))
                    except (TypeError, ValueError):
                        pass
                elif iters is not None:
                    # 只有 iters 没 evals: 无法施加维度感知充分性检查, 记 WARN 提示补 evals.
                    findings.append(('WARN', rel,
                                     '全局搜索仅记录 iters 未记录 n_eval, 无法核维度感知搜索充分性'))

            # C2: high-dim / multi-resource solves should carry a budget ladder
            # (convergence.json) proving marginal gain flattened.  Advisory
            # (WARN) so it never hard-blocks, but surfaces "solved once and
            # shipped" — the rerun_0706 pattern.
            conv = os.path.join(os.path.dirname(path), 'convergence.json')
            if not os.path.exists(conv):
                findings.append(('WARN', rel,
                                 'C2: 无 convergence.json (全局搜索未记录预算阶梯/边际收益停机)'))
            else:
                cj = _load_json(conv) or {}
                ladder = cj.get('ladder') or []
                if len(ladder) < 2:
                    findings.append(('WARN', rel,
                                     'C2: convergence.json 的 ladder 少于 2 级 (无法证明已收敛)'))

    return findings, len(files)


def main():
    if len(sys.argv) < 2:
        print('Usage: verify_provenance.py <project_dir> [base_name]')
        sys.exit(2)
    project_dir = sys.argv[1]
    if not os.path.isdir(project_dir):
        print(f'ERROR: {project_dir} not found')
        sys.exit(3)

    print('=== Solve Provenance / Reality Gate (B1/B2/B3) ===')
    print(f'Project: {project_dir}')
    print()

    stubs = check_stub_residue(project_dir)
    findings, n_values = check_values(project_dir)

    hard = [f for f in findings if f[0] == 'HARD_FAIL']
    repair = [f for f in findings if f[0] == 'REPAIR_FALLBACK']
    budget = [f for f in findings if f[0] == 'BUDGET_LIMITED']
    warns = [f for f in findings if f[0] == 'WARN']

    if stubs:
        print('[B3] STUB residue (承诺的求解器从未实现):')
        for s in stubs:
            print(f'  - {s}')
        print()
    if repair:
        print('[B1] REPAIR_FALLBACK / 来源不可对账:')
        for _, rel, msg in repair:
            print(f'  - {rel}: {msg}')
        print()
    if budget:
        print('[B2] BUDGET_LIMITED / 预算不足:')
        for _, rel, msg in budget:
            print(f'  - {rel}: {msg}')
        print()
    if hard:
        print('[!!] HARD_FAIL:')
        for _, rel, msg in hard:
            print(f'  - {rel}: {msg}')
        print()
    if warns:
        print('[C2] WARN (advisory, non-blocking):')
        for _, rel, msg in warns:
            print(f'  - {rel}: {msg}')
        print()

    print(f'VALUES_FILES     = {n_values}')
    print(f'STUB_RESIDUE     = {len(stubs)}')
    print(f'REPAIR_FALLBACK  = {len(repair)}')
    print(f'BUDGET_LIMITED   = {len(budget)}')
    print(f'HARD_FAIL        = {len(hard)}')
    print(f'WARN             = {len(warns)}')
    print()

    if stubs or hard:
        print('VERDICT: FAIL (BLOCKING — stub/hard failure, must fix before Step 5 completes)')
        sys.exit(1)
    if repair or budget:
        print('VERDICT: REPAIR_FALLBACK (results present but provenance/budget insufficient; '
              'record BLOCKING issue + trigger consult; must be cleared before Step 16)')
        sys.exit(1)
    if n_values == 0:
        print('VERDICT: INDETERMINATE (no results/p*/values.json found)')
        sys.exit(2)
    print('VERDICT: PASS')
    sys.exit(0)


if __name__ == '__main__':
    main()
