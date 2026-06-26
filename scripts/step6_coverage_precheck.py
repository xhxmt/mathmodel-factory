#!/usr/bin/env python3
"""
Step 6 敏感性分析覆盖度预检查工具

在 Step 6 正式开始前检查必要条件,避免进入重试循环后才发现基础数据缺失。
作为 run_paper.sh 在 dispatch_step step6_sensitivity.txt 之前的前置检查。

退出码:
  0 = 通过所有预检查,可以启动 Step 6
  1 = 警告级别缺失(可继续但有风险)
  2 = 阻塞级别缺失(必须先修复 Step 5 产物)
"""

import sys
import json
import argparse
from pathlib import Path
from typing import List, Tuple

def check_solve_log_step6_section(project_path: Path) -> Tuple[bool, str]:
    """检查 solve_log.md 是否包含 Step 6 接力段落"""
    solve_log = project_path / "solve_log.md"
    if not solve_log.exists():
        return False, "solve_log.md 不存在"

    content = solve_log.read_text(encoding='utf-8')
    if "Step 6 接力" not in content and "##Step 6" not in content:
        return False, "solve_log.md 缺少 'Step 6 接力' 段落(Step 5 未准备好 sweep 清单)"

    # 检查是否有至少一个参数扫描任务
    if "sweep" not in content.lower() and "扫描" not in content and "sensitivity" not in content.lower():
        return False, "solve_log.md 中未找到任何扫描任务(sweep/sensitivity)"

    return True, "solve_log.md §Step 6 接力段落存在"

def check_results_baseline(project_path: Path) -> Tuple[bool, str]:
    """检查 results/<subproblem>/values.json 基线数据"""
    results_dir = project_path / "results"
    if not results_dir.exists():
        return False, "results/ 目录不存在"

    subproblem_dirs = [d for d in results_dir.iterdir() if d.is_dir() and not d.name.startswith('.')]
    if not subproblem_dirs:
        return False, "results/ 目录下无子问题目录"

    missing_values = []
    for subdir in subproblem_dirs:
        values_json = subdir / "values.json"
        if not values_json.exists():
            missing_values.append(subdir.name)

    if missing_values:
        return False, f"{len(missing_values)} 个子问题缺少 values.json: {', '.join(missing_values)}"

    return True, f"找到 {len(subproblem_dirs)} 个子问题的 baseline 数据"

def check_assumption_ledger(project_path: Path) -> Tuple[bool, str, dict]:
    """检查 assumption_ledger.md 假设状态分布"""
    ledger = project_path / "assumption_ledger.md"
    if not ledger.exists():
        return False, "assumption_ledger.md 不存在", {}

    content = ledger.read_text(encoding='utf-8')
    lines = content.split('\n')

    status_counts = {
        'OPEN': 0,
        'INHERITED': 0,
        'CONFIRMED': 0,
        'RELAXED': 0,
        'PROTECTED': 0
    }

    for line in lines:
        if '|' in line and not line.strip().startswith('#'):
            parts = [p.strip() for p in line.split('|')]
            if len(parts) >= 6:  # id | statement | source | impact | status | tags
                status = parts[4].strip().upper()
                for key in status_counts:
                    if key in status:
                        status_counts[key] += 1
                        break

    total_assumptions = sum(status_counts.values())
    if total_assumptions == 0:
        return False, "assumption_ledger.md 无有效假设条目", status_counts

    # OPEN 假设过多是警告(Step 6 需要处理它们)
    open_ratio = status_counts['OPEN'] / total_assumptions if total_assumptions > 0 else 0

    msg = f"假设总数 {total_assumptions}, OPEN {status_counts['OPEN']} ({open_ratio:.0%})"
    return True, msg, status_counts

def check_model_specification(project_path: Path) -> Tuple[bool, str]:
    """检查 model.md 和 chosen_method.md 是否完整"""
    model_md = project_path / "model.md"
    chosen_md = project_path / "chosen_method.md"

    if not model_md.exists():
        return False, "model.md 不存在"

    if not chosen_md.exists():
        return False, "chosen_method.md 不存在"

    model_content = model_md.read_text(encoding='utf-8')
    if len(model_content) < 500:
        return False, f"model.md 内容过少({len(model_content)} 字符)"

    return True, "model.md 和 chosen_method.md 完整"

def check_solver_scripts_exist(project_path: Path) -> Tuple[bool, str]:
    """检查 models/m*/ 下是否有可执行的求解脚本"""
    models_dir = project_path / "models"
    if not models_dir.exists():
        return False, "models/ 目录不存在"

    model_dirs = [d for d in models_dir.iterdir() if d.is_dir() and d.name.startswith('m')]
    if not model_dirs:
        return False, "models/ 目录下无 m*_* 建模流目录"

    # 检查是否有 05_sensitivity.py 或类似的敏感性脚本骨架
    sensitivity_scripts = []
    for mdir in model_dirs:
        scripts = list(mdir.glob("05_sensitivity.*")) + list(mdir.glob("*sensitivity*.*"))
        sensitivity_scripts.extend(scripts)

    msg = f"找到 {len(model_dirs)} 个建模流目录"
    if sensitivity_scripts:
        msg += f", {len(sensitivity_scripts)} 个敏感性脚本骨架"

    return True, msg

def main():
    parser = argparse.ArgumentParser(description="Step 6 敏感性分析覆盖度预检查")
    parser.add_argument("project_path", type=Path, help="项目目录路径")
    parser.add_argument("--strict", action="store_true", help="严格模式(任何警告都返回非零)")
    args = parser.parse_args()

    project_path = args.project_path.resolve()
    if not project_path.exists():
        print(f"❌ 项目目录不存在: {project_path}", file=sys.stderr)
        sys.exit(2)

    print(f"Step 6 覆盖度预检查: {project_path.name}")
    print("=" * 60)

    checks = []
    warnings = []
    blockers = []

    # 1. solve_log.md Step 6 接力段落
    ok, msg = check_solve_log_step6_section(project_path)
    checks.append(("solve_log.md Step 6 接力", ok, msg))
    if not ok:
        blockers.append(msg)

    # 2. results/ baseline 数据
    ok, msg = check_results_baseline(project_path)
    checks.append(("results/ baseline 数据", ok, msg))
    if not ok:
        blockers.append(msg)

    # 3. assumption_ledger.md
    ok, msg, status_counts = check_assumption_ledger(project_path)
    checks.append(("assumption_ledger.md", ok, msg))
    if not ok:
        blockers.append(msg)
    elif status_counts.get('OPEN', 0) > 10:
        warnings.append(f"OPEN 假设过多({status_counts['OPEN']}), Step 6 工作量可能很大")

    # 4. model.md 和 chosen_method.md
    ok, msg = check_model_specification(project_path)
    checks.append(("model.md 规格", ok, msg))
    if not ok:
        blockers.append(msg)

    # 5. solver 脚本存在性
    ok, msg = check_solver_scripts_exist(project_path)
    checks.append(("models/ 建模流", ok, msg))
    if not ok:
        warnings.append(msg)

    # 打印结果
    for name, ok, msg in checks:
        status = "✅" if ok else "❌"
        print(f"{status} {name}: {msg}")

    if warnings:
        print("\n⚠️  警告:")
        for w in warnings:
            print(f"  - {w}")

    if blockers:
        print("\n❌ 阻塞问题(必须修复):")
        for b in blockers:
            print(f"  - {b}")
        print("\n建议: 回退到 Step 5, 确保 solve_log.md 包含完整的 Step 6 接力清单")
        sys.exit(2)

    if warnings and args.strict:
        print("\n⚠️  严格模式: 警告视为失败")
        sys.exit(1)

    print("\n✅ 预检查通过, 可以启动 Step 6")
    sys.exit(0)

if __name__ == "__main__":
    main()
