#!/usr/bin/env python3
"""方法库增量更新协议 — 当method_library/更新时，识别受影响的历史项目并重新验证。

Usage:
    python3 method_library_update.py --diff [<git_ref1>] [<git_ref2>]
    python3 method_library_update.py --validate <method_path> <complete_dir>

功能：
  1. 检测method_library/的变更（通过git diff）
  2. 识别哪些历史项目使用了变更的方法
  3. 为每个受影响项目重跑关键验证（求解器、硬指标）
  4. 生成变更影响报告

工作流：
  1. 开发者修改method_library/optimization/milp.md
  2. 运行: python3 method_library_update.py --diff HEAD~1 HEAD
  3. 输出: 哪些项目受影响，建议重跑哪些验证
  4. 可选: --validate 自动重跑验证

数据结构：
    {
        "changed_methods": [
            {
                "path": "method_library/optimization/milp.md",
                "change_type": "modified|added|deleted",
                "diff_summary": "...",
            }
        ],
        "affected_projects": [
            {
                "project_name": "cumcm2024b_rep1",
                "uses_method": "method_library/optimization/milp.md",
                "usage_type": "primary|auxiliary",
                "validation_status": "pending|pass|fail",
            }
        ],
        "recommendations": [
            "Re-run solver validation for 3 projects",
            "Re-run hard_metrics for 5 projects",
        ]
    }
"""

import os
import re
import sys
import json
import subprocess
from pathlib import Path
from collections import defaultdict


def _read_file(path):
    """读取文件，容错编码。"""
    if not os.path.exists(path):
        return ""
    for enc in ('utf-8', 'latin-1', 'cp1252'):
        try:
            with open(path, 'r', encoding=enc, errors='replace') as f:
                return f.read()
        except (IOError, UnicodeDecodeError):
            continue
    return ""


def get_changed_methods(git_ref1='HEAD~1', git_ref2='HEAD'):
    """通过git diff检测method_library/的变更。"""
    try:
        # 获取变更文件列表
        result = subprocess.run(
            ['git', 'diff', '--name-status', git_ref1, git_ref2, 'method_library/'],
            capture_output=True,
            text=True,
            timeout=10
        )

        if result.returncode != 0:
            print(f"[WARN] git diff failed: {result.stderr}", file=sys.stderr)
            return []

        changes = []
        for line in result.stdout.strip().split('\n'):
            if not line:
                continue

            parts = line.split('\t')
            if len(parts) < 2:
                continue

            status = parts[0]
            path = parts[1]

            # 只关注.md文件
            if not path.endswith('.md'):
                continue

            # 排除README.md和索引文件
            if 'README' in path or 'index' in path:
                continue

            change_type = {
                'M': 'modified',
                'A': 'added',
                'D': 'deleted',
            }.get(status, 'unknown')

            # 获取diff摘要（简化版）
            try:
                diff_result = subprocess.run(
                    ['git', 'diff', git_ref1, git_ref2, '--', path],
                    capture_output=True,
                    text=True,
                    timeout=10
                )
                diff_lines = diff_result.stdout.split('\n')
                # 提取添加/删除的行数
                added = sum(1 for line in diff_lines if line.startswith('+') and not line.startswith('+++'))
                removed = sum(1 for line in diff_lines if line.startswith('-') and not line.startswith('---'))
                diff_summary = f'+{added}/-{removed} lines'
            except Exception:
                diff_summary = 'unknown'

            changes.append({
                'path': path,
                'change_type': change_type,
                'diff_summary': diff_summary,
            })

        return changes

    except FileNotFoundError:
        print("[WARN] git not found, cannot detect changes", file=sys.stderr)
        return []
    except Exception as e:
        print(f"[WARN] git diff error: {e}", file=sys.stderr)
        return []


def find_projects_using_method(method_path, complete_dir):
    """找到使用了指定方法的所有项目。"""
    affected = []

    if not os.path.isdir(complete_dir):
        return affected

    for project_name in os.listdir(complete_dir):
        project_dir = os.path.join(complete_dir, project_name)
        if not os.path.isdir(project_dir):
            continue

        # 检查chosen_method.md
        chosen_path = os.path.join(project_dir, 'chosen_method.md')
        if not os.path.exists(chosen_path):
            continue

        text = _read_file(chosen_path)

        # 检查method_library引用（简单匹配）
        if method_path in text:
            # 判断是PRIMARY还是AUXILIARY
            usage_type = 'unknown'
            if re.search(rf'PRIMARY:.*{re.escape(method_path)}', text, re.DOTALL):
                usage_type = 'primary'
            elif re.search(rf'AUXILIARY:.*{re.escape(method_path)}', text, re.DOTALL):
                usage_type = 'auxiliary'

            affected.append({
                'project_name': project_name,
                'project_dir': project_dir,
                'uses_method': method_path,
                'usage_type': usage_type,
                'validation_status': 'pending',
            })

    return affected


def validate_project(project_dir, project_name):
    """重跑项目的关键验证。"""
    validations = {
        'solver': 'unknown',
        'hard_metrics': 'unknown',
    }

    # 运行求解器验证
    try:
        result = subprocess.run(
            ['python3', 'scripts/verify_solver.py', project_dir, project_name],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=os.path.dirname(os.path.dirname(__file__))
        )
        validations['solver'] = 'pass' if result.returncode == 0 else 'fail'
    except Exception as e:
        validations['solver'] = f'error: {e}'

    # 运行硬指标验证
    try:
        result = subprocess.run(
            ['python3', 'scripts/hard_metrics.py', project_dir, project_name],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=os.path.dirname(os.path.dirname(__file__))
        )
        validations['hard_metrics'] = 'pass' if result.returncode == 0 else 'fail'

        # 提取关键指标
        output = result.stdout
        metrics = {}
        for key in ['non_converged', 'dangling_cites', 'symbols_undefined']:
            match = re.search(rf'{key}[^\d]*(\d+)', output)
            if match:
                metrics[key] = int(match.group(1))

        validations['metrics'] = metrics

    except Exception as e:
        validations['hard_metrics'] = f'error: {e}'

    return validations


def generate_recommendations(affected_projects):
    """生成重跑验证的推荐。"""
    recommendations = []

    primary_count = sum(1 for p in affected_projects if p['usage_type'] == 'primary')
    total_count = len(affected_projects)

    if primary_count > 0:
        recommendations.append(
            f"Method change affects {primary_count} projects as PRIMARY method - high priority re-validation"
        )

    if total_count > primary_count:
        recommendations.append(
            f"{total_count - primary_count} projects use as AUXILIARY - medium priority re-validation"
        )

    recommendations.append(
        f"Suggested actions: Run 'python3 method_library_update.py --validate <method_path> complete/'"
    )

    return recommendations


def main():
    args = sys.argv[1:]

    if len(args) >= 1 and args[0] == '--diff':
        ref1 = args[1] if len(args) >= 2 else 'HEAD~1'
        ref2 = args[2] if len(args) >= 3 else 'HEAD'

        print(f"检测变更: {ref1}..{ref2}", file=sys.stderr)

        changes = get_changed_methods(ref1, ref2)

        if not changes:
            print("No changes detected in method_library/", file=sys.stderr)
            sys.exit(0)

        # 假设complete/在当前目录
        complete_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'complete')

        all_affected = []
        for change in changes:
            affected = find_projects_using_method(change['path'], complete_dir)
            all_affected.extend(affected)

        recommendations = generate_recommendations(all_affected)

        result = {
            'changed_methods': changes,
            'affected_projects': all_affected,
            'recommendations': recommendations,
            'summary': {
                'changed_method_count': len(changes),
                'affected_project_count': len(all_affected),
            }
        }

        print(json.dumps(result, ensure_ascii=False, indent=2))
        sys.exit(0)

    elif len(args) >= 3 and args[0] == '--validate':
        method_path = args[1]
        complete_dir = args[2]

        print(f"验证使用 {method_path} 的项目", file=sys.stderr)

        affected = find_projects_using_method(method_path, complete_dir)

        if not affected:
            print(f"No projects found using {method_path}", file=sys.stderr)
            sys.exit(0)

        print(f"Found {len(affected)} affected projects, validating...", file=sys.stderr)

        results = []
        for proj in affected:
            print(f"  Validating {proj['project_name']}...", file=sys.stderr)

            validations = validate_project(proj['project_dir'], proj['project_name'])

            proj['validation_status'] = 'pass' if validations['solver'] == 'pass' and validations['hard_metrics'] == 'pass' else 'fail'
            proj['validations'] = validations

            results.append(proj)

        # 汇总
        pass_count = sum(1 for r in results if r['validation_status'] == 'pass')
        fail_count = len(results) - pass_count

        summary = {
            'method_path': method_path,
            'validated_projects': results,
            'summary': {
                'total': len(results),
                'pass': pass_count,
                'fail': fail_count,
            }
        }

        print(json.dumps(summary, ensure_ascii=False, indent=2))

        # Exit code: 0=全部通过, 1=有失败
        sys.exit(0 if fail_count == 0 else 1)

    else:
        print("Usage: method_library_update.py --diff [<git_ref1>] [<git_ref2>]", file=sys.stderr)
        print("   or: method_library_update.py --validate <method_path> <complete_dir>", file=sys.stderr)
        sys.exit(2)


if __name__ == '__main__':
    main()
