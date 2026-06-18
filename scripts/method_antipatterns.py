#!/usr/bin/env python3
"""方法库反例库 — 记录历史上失败的"问题类型+方法"组合，供Step 2提前过滤。

Usage:
    python3 method_antipatterns.py --build <complete_dir>
    python3 method_antipatterns.py --check <project_dir> <method_path>

功能：
  1. 从历史项目中识别"失败"的方法选择（低分、求解器不收敛、被ABANDONED）
  2. 提取失败的模式：问题特征 + 方法 → 失败原因
  3. 新项目Step 2时，检查候选方法是否命中反例库

反例定义：
  - judge分数 < 60
  - 硬指标: non_converged > 0 或 dangling_cites > 5 或 symbols_undefined > 20
  - Step 2流被ABANDONED（从critique verdict提取）

数据结构：
    {
        "antipatterns": [
            {
                "pattern_id": "ap_001",
                "problem_features": {...},
                "method_path": "method_library/...",
                "failure_reason": "solver_diverged|low_score|abandoned",
                "evidence_projects": ["cumcm2024b_rep1", ...],
                "severity": 0.0-1.0,
            }
        ]
    }
"""

import os
import re
import sys
import json
import glob
from pathlib import Path
from collections import defaultdict

from problem_paths import resolve_problem_constraints_path


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


def extract_problem_features(project_dir):
    """从problem/目录提取问题特征（复用method_fit_score的逻辑）。"""
    # 这里简化，实际应导入method_fit_score.extract_problem_features
    problem_dir = os.path.join(project_dir, 'problem')
    brief_path = os.path.join(problem_dir, 'problem_brief.md')
    constraints_path = resolve_problem_constraints_path(project_dir)

    brief_text = _read_file(brief_path)
    constraints_text = _read_file(constraints_path)
    combined = brief_text + '\n' + constraints_text

    return {
        'has_integer': any(kw in combined.lower() for kw in ['integer', '整数', 'discrete', '离散', '0-1']),
        'has_stochastic': any(kw in combined.lower() for kw in ['stochastic', '随机', 'uncertain', '不确定']),
        'has_nonlinear': any(kw in combined.lower() for kw in ['nonlinear', '非线性', 'quadratic', '二次']),
        'is_multi_objective': any(kw in combined.lower() for kw in ['multi-objective', '多目标', 'pareto']),
        'is_evaluation': any(kw in combined.lower() for kw in ['evaluate', '评价', 'rank', '排序']),
        'is_large_scale': any(kw in combined.lower() for kw in ['large-scale', '大规模', 'high-dimensional', '高维']),
    }


def is_failure(project_dir, base_name):
    """判断项目是否"失败"（质量低或求解失败）。"""
    # 检查judge分数
    judge_path = os.path.join(project_dir, 'judge_evaluation.md')
    judge_text = _read_file(judge_path)
    score_match = re.search(r'总分.*?[:：]\s*(\d+)', judge_text)
    if score_match:
        score = int(score_match.group(1))
        if score < 60:
            return True, f'judge_score_{score}'

    # 检查硬指标
    try:
        import sys
        sys.path.insert(0, os.path.dirname(__file__))
        from hard_metrics import collect_all

        metrics = collect_all(project_dir, base_name)

        if metrics.get('non_converged', 0) > 0:
            return True, f'solver_non_converged_{metrics["non_converged"]}'
        if metrics.get('dangling_cites', 0) > 5:
            return True, f'dangling_cites_{metrics["dangling_cites"]}'
        if metrics.get('symbols_undefined', 0) > 20:
            return True, f'symbols_undefined_{metrics["symbols_undefined"]}'
    except Exception:
        pass

    return False, None


def extract_abandoned_streams(project_dir):
    """从Step 2的critique文件中提取ABANDONED流。"""
    abandoned = []

    critique_files = glob.glob(os.path.join(project_dir, 'm*_critique.md'))
    for crit_file in critique_files:
        text = _read_file(crit_file)
        if 'VERDICT: ABANDONED' in text or 'ABANDONED' in text:
            # 提取stream id
            basename = os.path.basename(crit_file)
            stream_id = basename.split('_')[0]  # m1, m2, ...

            # 提取reason
            reason_match = re.search(r'Reason:\s*([^\n]+)', text)
            reason = reason_match.group(1).strip() if reason_match else 'unknown'

            abandoned.append({
                'stream_id': stream_id,
                'reason': reason,
            })

    return abandoned


def extract_method_from_stream(project_dir, stream_id):
    """从stream的spec/critique中提取使用的方法。"""
    spec_file = os.path.join(project_dir, f'{stream_id}_spec.md')
    text = _read_file(spec_file)

    # 简单启发式：查找method_library引用
    method_refs = re.findall(r'method_library/[A-Za-z0-9_/.]+\.md', text)
    if method_refs:
        return method_refs[0]

    return None


def build_antipatterns(complete_dir):
    """从complete/目录构建反例库。"""
    antipatterns = []
    pattern_id_counter = 1

    for project_name in os.listdir(complete_dir):
        project_dir = os.path.join(complete_dir, project_name)
        if not os.path.isdir(project_dir):
            continue

        # 检查是否失败
        is_fail, fail_reason = is_failure(project_dir, project_name)

        # 提取abandoned流
        abandoned_streams = extract_abandoned_streams(project_dir)

        # 提取问题特征
        try:
            features = extract_problem_features(project_dir)
        except Exception:
            continue

        # 如果整体失败，记录PRIMARY方法为反例
        if is_fail:
            chosen_path = os.path.join(project_dir, 'chosen_method.md')
            text = _read_file(chosen_path)
            primary_match = re.search(r'PRIMARY:\s*(\w+)', text)
            if primary_match:
                stream_id = primary_match.group(1)
                method_path = extract_method_from_stream(project_dir, stream_id)
                if method_path:
                    antipatterns.append({
                        'pattern_id': f'ap_{pattern_id_counter:03d}',
                        'problem_features': features,
                        'method_path': method_path,
                        'failure_reason': fail_reason,
                        'evidence_projects': [project_name],
                        'severity': 0.8,  # 整体失败较严重
                    })
                    pattern_id_counter += 1

        # 记录abandoned流为反例
        for stream in abandoned_streams:
            method_path = extract_method_from_stream(project_dir, stream['stream_id'])
            if method_path:
                antipatterns.append({
                    'pattern_id': f'ap_{pattern_id_counter:03d}',
                    'problem_features': features,
                    'method_path': method_path,
                    'failure_reason': f"abandoned_{stream['reason']}",
                    'evidence_projects': [project_name],
                    'severity': 0.5,  # abandoned严重度中等
                })
                pattern_id_counter += 1

    # 合并相同的反例模式
    merged = defaultdict(lambda: {'evidence': [], 'count': 0})
    for ap in antipatterns:
        key = (ap['method_path'], tuple(sorted(ap['problem_features'].items())))
        merged[key]['evidence'].extend(ap['evidence_projects'])
        merged[key]['count'] += 1
        if 'severity_sum' not in merged[key]:
            merged[key]['severity_sum'] = 0
            merged[key]['pattern'] = ap
        merged[key]['severity_sum'] += ap['severity']

    # 生成最终反例列表
    final_antipatterns = []
    for key, data in merged.items():
        if data['count'] >= 1:  # 至少出现1次
            pattern = data['pattern'].copy()
            pattern['evidence_projects'] = list(set(data['evidence']))
            pattern['severity'] = round(data['severity_sum'] / data['count'], 2)
            pattern['occurrence_count'] = data['count']
            final_antipatterns.append(pattern)

    # 按严重度排序
    final_antipatterns.sort(key=lambda x: (x['severity'], x['occurrence_count']), reverse=True)

    return {
        'antipatterns': final_antipatterns,
        'summary': {
            'total_antipatterns': len(final_antipatterns),
            'high_severity_count': sum(1 for ap in final_antipatterns if ap['severity'] >= 0.7),
        }
    }


def check_antipattern(problem_features, method_path, antipattern_db):
    """检查给定方法是否命中反例库。"""
    antipatterns = antipattern_db.get('antipatterns', [])

    matches = []
    for ap in antipatterns:
        # 检查method_path是否匹配
        if ap['method_path'] != method_path:
            continue

        # 检查问题特征重合度
        common_features = set(problem_features.keys()) & set(ap['problem_features'].keys())
        if not common_features:
            continue

        match_count = sum(
            1 for k in common_features
            if problem_features.get(k) == ap['problem_features'].get(k)
        )

        match_ratio = match_count / len(common_features)

        if match_ratio >= 0.5:  # 至少50%特征匹配
            matches.append({
                'pattern_id': ap['pattern_id'],
                'match_ratio': round(match_ratio, 2),
                'severity': ap['severity'],
                'failure_reason': ap['failure_reason'],
                'evidence_projects': ap['evidence_projects'],
            })

    return {
        'has_antipattern': len(matches) > 0,
        'matches': matches,
        'max_severity': max((m['severity'] for m in matches), default=0),
    }


def main():
    args = sys.argv[1:]

    if len(args) >= 2 and args[0] == '--build':
        complete_dir = args[1]
        print(f"构建反例库: {complete_dir}", file=sys.stderr)

        db = build_antipatterns(complete_dir)
        print(json.dumps(db, ensure_ascii=False, indent=2))

        # 保存反例库
        db_path = os.path.join(os.path.dirname(__file__), 'method_antipatterns.json')
        with open(db_path, 'w', encoding='utf-8') as f:
            json.dump(db, f, ensure_ascii=False, indent=2)
        print(f"\n反例库已保存: {db_path}", file=sys.stderr)

        sys.exit(0)

    elif len(args) >= 3 and args[0] == '--check':
        project_dir = args[1]
        method_path = args[2]

        # 加载反例库
        db_path = os.path.join(os.path.dirname(__file__), 'method_antipatterns.json')
        if not os.path.exists(db_path):
            print(f"ERROR: 反例库不存在: {db_path}", file=sys.stderr)
            print("请先运行: python3 method_antipatterns.py --build complete/", file=sys.stderr)
            sys.exit(2)

        with open(db_path, 'r', encoding='utf-8') as f:
            db = json.load(f)

        # 提取问题特征
        features = extract_problem_features(project_dir)

        # 检查反例
        result = check_antipattern(features, method_path, db)

        print(json.dumps(result, ensure_ascii=False, indent=2))

        # Exit code: 0=无反例, 1=有反例
        sys.exit(1 if result['has_antipattern'] else 0)

    else:
        print("Usage: method_antipatterns.py --build <complete_dir>", file=sys.stderr)
        print("   or: method_antipatterns.py --check <project_dir> <method_path>", file=sys.stderr)
        sys.exit(2)


if __name__ == '__main__':
    main()
