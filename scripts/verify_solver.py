#!/usr/bin/env python3
"""求解器收敛性检测器 — 检查优化问题是否真正收敛。

Usage:
    python3 verify_solver.py <project_dir> <base_name>

检查项目中所有求解器日志，识别常见的非收敛模式：
  - 梯度爆炸/NaN
  - 迭代数达上限但未收敛
  - gap仍然很大
  - 求解器报告infeasible/unbounded
  - 超时终止

输出：
    SOLVER_RUNS         = N   (检测到的求解器运行数)
    CONVERGED           = M   (明确收敛的运行)
    NON_CONVERGED       = K   (未收敛/可疑的运行)
    SOLVER_WARNINGS     = W   (警告数量)

Exit code: 0 if all runs converged, 1 if any non-converged, 2 if cannot determine.
"""

import os
import re
import sys
import glob
import json
from pathlib import Path


# 收敛信号模式（正面）
_CONVERGED_PATTERNS = [
    r'optimal\s+solution\s+found',
    r'converged',
    r'solution\s+status:\s*optimal',
    r'solver\s+terminated\s+successfully',
    r'exit\s+flag:\s*1',
    r'exitflag\s*=\s*1',
    r'status:\s*0',
    r'optimal',
    r'Solver\s+status:\s*ok',
]

# 非收敛信号模式（负面）
_NON_CONVERGED_PATTERNS = [
    (r'infeasible', 'INFEASIBLE'),
    (r'unbounded', 'UNBOUNDED'),
    (r'iteration\s+limit\s+reached', 'ITER_LIMIT'),
    (r'time\s+limit\s+reached', 'TIMEOUT'),
    (r'numerical\s+error', 'NUMERICAL_ERROR'),
    (r'nan', 'NAN_DETECTED'),
    (r'inf(?!easible)', 'INF_DETECTED'),
    (r'failed\s+to\s+converge', 'FAILED_CONVERGE'),
    (r'error:', 'ERROR'),
    (r'warning.*not\s+converged', 'WARN_NOT_CONV'),
]

# Gap阈值模式（最优性间隙）
_GAP_PATTERN = re.compile(r'(?:gap|mip\s*gap|relative\s*gap)[\s:=]+([\d.e+-]+)', re.IGNORECASE)
_GAP_THRESHOLD = 0.05  # 5% gap认为不够优


def _read_log(path):
    """读取日志文件，容错多种编码。"""
    for enc in ('utf-8', 'latin-1', 'cp1252'):
        try:
            with open(path, 'r', encoding=enc, errors='replace') as f:
                return f.read()
        except (IOError, UnicodeDecodeError):
            continue
    return ""


def analyze_log(log_path):
    """分析单个日志文件，返回 (converged, issues)。

    converged: True/False/None (None表示无法判断)
    issues: list of (issue_type, line_num, excerpt)
    """
    text = _read_log(log_path)
    if not text:
        return None, [('READ_ERROR', 0, 'Failed to read log')]

    lines = text.splitlines()
    issues = []
    has_converged_signal = False
    has_problem_signal = False

    # 检查收敛信号
    for i, line in enumerate(lines, 1):
        line_lower = line.lower()
        for pat in _CONVERGED_PATTERNS:
            if re.search(pat, line_lower):
                has_converged_signal = True
                break

    # 检查非收敛信号
    for i, line in enumerate(lines, 1):
        line_lower = line.lower()
        for pat, label in _NON_CONVERGED_PATTERNS:
            if re.search(pat, line_lower):
                has_problem_signal = True
                issues.append((label, i, line.strip()[:80]))

    # 检查gap
    for i, line in enumerate(lines, 1):
        m = _GAP_PATTERN.search(line)
        if m:
            try:
                gap = float(m.group(1))
                if gap > _GAP_THRESHOLD:
                    has_problem_signal = True
                    issues.append(('LARGE_GAP', i, f'gap={gap:.3f} > {_GAP_THRESHOLD}'))
            except ValueError:
                pass

    # 综合判断
    if has_converged_signal and not has_problem_signal:
        return True, issues
    if has_problem_signal:
        return False, issues
    # 没有明确信号 — 保守判断为可疑
    return None, issues


def discover_solver_logs(project_dir):
    """发现项目中所有求解器日志。

    求解器日志来源：
      1. models/**/*.log (solver_submit.sh输出)
      2. results/**/*.log
      3. logs/*.log (但跳过step_*.log等流程日志)
    """
    log_paths = []

    # models/ 下的 .log
    log_paths.extend(glob.glob(os.path.join(project_dir, 'models', '**', '*.log'), recursive=True))

    # results/ 下的 .log
    results_dir = os.path.join(project_dir, 'results')
    if os.path.isdir(results_dir):
        log_paths.extend(glob.glob(os.path.join(results_dir, '**', '*.log'), recursive=True))

    # logs/ 下的求解器相关日志（跳过step_*.log）
    logs_dir = os.path.join(project_dir, 'logs')
    if os.path.isdir(logs_dir):
        for log_file in glob.glob(os.path.join(logs_dir, '*.log')):
            base = os.path.basename(log_file)
            # 跳过流程日志
            if not base.startswith('step_') and not base.startswith('run_'):
                log_paths.append(log_file)

    return sorted(set(log_paths))


def collect_solver_metrics(project_dir, base_name):
    """收集求解器收敛指标 dict；不打印。无日志返回 None。"""
    log_paths = discover_solver_logs(project_dir)
    if not log_paths:
        return None

    converged_count = 0
    non_converged_count = 0
    uncertain_count = 0
    all_issues = []

    run_details = []
    for lp in log_paths:
        result, issues = analyze_log(lp)
        rel_path = os.path.relpath(lp, project_dir)

        if result is True:
            converged_count += 1
            status = 'OK'
        elif result is False:
            non_converged_count += 1
            status = 'FAIL'
        else:
            uncertain_count += 1
            status = 'UNCERTAIN'

        all_issues.extend(issues)
        run_details.append({
            'log': rel_path,
            'status': status,
            'issues': len(issues),
        })

    total_runs = len(log_paths)
    warning_count = len(all_issues)

    return {
        'solver_runs': total_runs,
        'converged': converged_count,
        'non_converged': non_converged_count,
        'uncertain': uncertain_count,
        'solver_warnings': warning_count,
        '_run_details': run_details,
        '_all_issues': all_issues,
    }


def _print_report(project_dir, metrics):
    """打印求解器收敛报告。"""
    print("=== Solver Convergence Report ===")
    print(f"Project: {project_dir}")
    print()
    print(f"SOLVER_RUNS      = {metrics['solver_runs']}")
    print(f"CONVERGED        = {metrics['converged']}")
    print(f"NON_CONVERGED    = {metrics['non_converged']}")
    print(f"UNCERTAIN        = {metrics['uncertain']}")
    print(f"SOLVER_WARNINGS  = {metrics['solver_warnings']}")
    print()

    # 打印每个运行的状态
    if metrics['_run_details']:
        print("=" * 70)
        print("Run Details:")
        print("=" * 70)
        for rd in metrics['_run_details']:
            status_mark = '✓' if rd['status'] == 'OK' else ('✗' if rd['status'] == 'FAIL' else '?')
            print(f"  {status_mark}  {rd['log']:<50s}  issues={rd['issues']}")
        print()

    # 打印问题详情
    if metrics['_all_issues']:
        print("=" * 70)
        print("Issues Found:")
        print("=" * 70)
        for issue_type, line_num, excerpt in metrics['_all_issues'][:20]:  # 最多显示20条
            print(f"  [{issue_type}] line {line_num}: {excerpt}")
        if len(metrics['_all_issues']) > 20:
            print(f"  ... and {len(metrics['_all_issues']) - 20} more issues")
        print()


def main():
    if len(sys.argv) < 3:
        print("Usage: verify_solver.py <project_dir> <base_name>")
        sys.exit(2)

    project_dir = sys.argv[1]
    base_name = sys.argv[2]

    if not os.path.isdir(project_dir):
        print(f"ERROR: {project_dir} not found")
        sys.exit(3)

    metrics = collect_solver_metrics(project_dir, base_name)

    if metrics is None:
        print(f"=== Solver Convergence Report ===")
        print(f"Project: {project_dir}")
        print()
        print("No solver logs found.")
        sys.exit(2)

    _print_report(project_dir, metrics)

    # Exit code: 0=all OK, 1=some failed, 2=cannot determine
    if metrics['non_converged'] > 0:
        sys.exit(1)
    elif metrics['converged'] > 0 and metrics['uncertain'] == 0:
        sys.exit(0)
    else:
        sys.exit(2)


if __name__ == '__main__':
    main()
