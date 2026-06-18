#!/usr/bin/env python3
"""Step 2 资源配额决策 — 根据问题复杂度动态分配并行流数量。

Usage:
    python3 step2_resource_quota.py <project_dir>

分析问题复杂度，给出推荐的并行流数量：
  - 简单问题（单变量/短时间窗）：3流
  - 中等问题（多变量/中等规模）：4-5流
  - 复杂问题（多目标/大规模/约束密集）：6-7流

基于：
  - problem/problem_brief.md 中的问题描述
  - problem/feasibility_constraints.md 中的约束数量（兼容旧名 constraints.md）
  - viable_streams.md 中Step 1提出的候选方法数

返回 JSON:
    {
        "recommended_streams": N,
        "complexity_score": 0-100,
        "factors": {
            "problem_length": ...,
            "constraint_count": ...,
            "candidate_methods": ...,
            ...
        },
        "reasoning": "..."
    }
"""

import os
import re
import sys
import json

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


def count_constraints(text):
    """统计约束数量的启发式。"""
    # 数学符号约束
    constraint_markers = [
        r'subject\s+to',
        r's\.t\.',
        r'约束',
        r'限制',
        r'条件',
        r'\\leq',
        r'\\geq',
        r'\<=',
        r'>=',
        r'\\in',
    ]
    count = 0
    text_lower = text.lower()
    for marker in constraint_markers:
        count += len(re.findall(marker, text_lower))
    return count


def count_objectives(text):
    """统计目标函数数量。"""
    objective_markers = [
        r'minimize',
        r'maximize',
        r'optimize',
        r'最小化',
        r'最大化',
        r'优化目标',
        r'objective',
    ]
    count = 0
    text_lower = text.lower()
    for marker in objective_markers:
        count += len(re.findall(marker, text_lower))
    return max(1, count)  # 至少有1个目标


def count_variables(text):
    """估算决策变量数量（粗略）。"""
    # 查找变量声明模式
    var_patterns = [
        r'变量.*?[:：]\s*([^。\n]+)',
        r'决策变量.*?[:：]\s*([^。\n]+)',
        r'variables?.*?[:：]\s*([^。\n]+)',
        r'\$[xyzwXYZW]_\{?[^}$]+\}?\$',  # LaTeX变量
    ]
    var_mentions = []
    for pat in var_patterns:
        var_mentions.extend(re.findall(pat, text, re.IGNORECASE))
    # 粗略估计：每个匹配对应1-5个变量
    return len(var_mentions) * 2


def count_data_files(project_dir):
    """统计数据文件数量（复杂度标志）。"""
    problem_dir = os.path.join(project_dir, 'problem')
    if not os.path.isdir(problem_dir):
        return 0
    count = 0
    for ext in ('.csv', '.xlsx', '.json', '.txt', '.dat'):
        count += len([f for f in os.listdir(problem_dir) if f.endswith(ext)])
    return count


def count_candidate_methods(viable_streams_path):
    """统计 viable_streams.md 中的候选方法数。"""
    text = _read_file(viable_streams_path)
    if not text:
        return 0
    # 统计 ## Stream m<N>: 标题
    return len(re.findall(r'##\s+Stream\s+m\d+:', text, re.IGNORECASE))


def detect_keywords(text):
    """检测问题类型关键词。"""
    keywords = {
        'multi_objective': any(kw in text.lower() for kw in ['multi-objective', '多目标', 'pareto']),
        'stochastic': any(kw in text.lower() for kw in ['stochastic', '随机', 'uncertainty', '不确定']),
        'dynamic': any(kw in text.lower() for kw in ['dynamic', '动态', 'time-varying', '时变']),
        'large_scale': any(kw in text.lower() for kw in ['large-scale', '大规模', 'high-dimensional', '高维']),
        'nonlinear': any(kw in text.lower() for kw in ['nonlinear', '非线性', 'nonconvex', '非凸']),
        'integer': any(kw in text.lower() for kw in ['integer', '整数', 'discrete', '离散', 'combinatorial', '组合']),
    }
    return keywords


def calculate_complexity_score(factors):
    """综合计算复杂度分数（0-100）。"""
    score = 0

    # 问题描述长度（归一化到0-10）
    prob_len = factors['problem_length']
    score += min(10, prob_len / 500)

    # 约束数量（归一化到0-15）
    constraints = factors['constraint_count']
    score += min(15, constraints * 0.5)

    # 变量数量（归一化到0-15）
    variables = factors['variable_estimate']
    score += min(15, variables * 0.3)

    # 目标数量（多目标+10分）
    objectives = factors['objective_count']
    if objectives > 1:
        score += 10

    # 数据文件（每个+2分，最多10分）
    data_files = factors['data_file_count']
    score += min(10, data_files * 2)

    # 候选方法数（多样性标志，每个+3分，最多15分）
    candidates = factors['candidate_methods']
    score += min(15, candidates * 3)

    # 关键词加分
    kw = factors['keywords']
    if kw['multi_objective']:
        score += 10
    if kw['stochastic']:
        score += 8
    if kw['nonlinear']:
        score += 5
    if kw['large_scale']:
        score += 10
    if kw['integer']:
        score += 5

    return min(100, round(score, 1))


def recommend_stream_count(complexity_score, candidate_methods):
    """根据复杂度分数推荐并行流数量。"""
    # 基础逻辑：复杂度 → 流数
    # 0-30: 简单 → 3流
    # 31-60: 中等 → 4-5流
    # 61-100: 复杂 → 5-7流

    if complexity_score <= 30:
        base = 3
    elif complexity_score <= 60:
        base = 4
    else:
        base = 5

    # 如果候选方法很多（>5），增加1流
    if candidate_methods > 5:
        base += 1

    # 上限7流（避免过度并行）
    return min(7, base)


def analyze_resource_quota(project_dir):
    """分析项目，返回资源配额推荐。"""
    problem_dir = os.path.join(project_dir, 'problem')
    brief_path = os.path.join(problem_dir, 'problem_brief.md')
    constraints_path = resolve_problem_constraints_path(project_dir)
    viable_path = os.path.join(project_dir, 'viable_streams.md')

    brief_text = _read_file(brief_path)
    constraints_text = _read_file(constraints_path)
    viable_text = _read_file(viable_path)

    # 收集因子
    factors = {
        'problem_length': len(brief_text),
        'constraint_count': count_constraints(constraints_text),
        'objective_count': count_objectives(brief_text + constraints_text),
        'variable_estimate': count_variables(brief_text + constraints_text),
        'data_file_count': count_data_files(project_dir),
        'candidate_methods': count_candidate_methods(viable_path),
        'keywords': detect_keywords(brief_text + constraints_text),
    }

    complexity_score = calculate_complexity_score(factors)
    recommended_streams = recommend_stream_count(complexity_score, factors['candidate_methods'])

    # 生成reasoning
    reasoning_parts = []
    if complexity_score <= 30:
        reasoning_parts.append("问题复杂度较低（单变量或短时间窗）")
    elif complexity_score <= 60:
        reasoning_parts.append("问题复杂度中等（多变量或中等规模）")
    else:
        reasoning_parts.append("问题复杂度较高（多目标/大规模/约束密集）")

    if factors['candidate_methods'] > 0:
        reasoning_parts.append(f"Step 1提出{factors['candidate_methods']}个候选方法")

    kw = factors['keywords']
    active_kw = [k for k, v in kw.items() if v]
    if active_kw:
        reasoning_parts.append(f"检测到特征: {', '.join(active_kw)}")

    reasoning = '; '.join(reasoning_parts) + f" → 推荐{recommended_streams}流并行"

    return {
        'recommended_streams': recommended_streams,
        'complexity_score': complexity_score,
        'factors': factors,
        'reasoning': reasoning,
    }


def main():
    if len(sys.argv) < 2:
        print(json.dumps({
            'error': 'Usage: step2_resource_quota.py <project_dir>'
        }), file=sys.stderr)
        sys.exit(2)

    project_dir = sys.argv[1]

    if not os.path.isdir(project_dir):
        print(json.dumps({
            'error': f'{project_dir} not found'
        }), file=sys.stderr)
        sys.exit(2)

    result = analyze_resource_quota(project_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0)


if __name__ == '__main__':
    main()
