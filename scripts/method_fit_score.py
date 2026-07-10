#!/usr/bin/env python3
"""方法库引用模式学习器 — 分析高质量论文的方法使用模式，生成适配度打分。

Usage:
    python3 method_fit_score.py <project_dir>
    python3 method_fit_score.py --learn <complete_dir>  # 从历史项目学习

功能：
  1. 提取项目的"问题特征"（约束数/变量数/目标类型/数据类型）
  2. 提取项目使用的方法（从chosen_method.md）
  3. 收集项目质量得分（judge分数或硬指标）
  4. 学习：问题特征 × 方法 → 质量得分 的关联模式
  5. 预测：给定新问题，推荐最适配的方法并打分

数据结构：
    {
        "problem_features": {
            "constraint_count": N,
            "variable_count": M,
            "objective_type": "minimize|maximize|multi",
            "has_integer": bool,
            "has_stochastic": bool,
            "problem_length": N,
            ...
        },
        "methods_used": {
            "primary": "method_library/optimization/milp.md",
            "auxiliary": "method_library/metaheuristic/genetic_algorithm.md"
        },
        "quality_score": 0-100,
        "quality_source": "judge|hard_metrics"
    }
"""

import os
import re
import sys
import json
import glob
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


def extract_problem_features(project_dir):
    """从problem/目录提取问题特征。"""
    problem_dir = os.path.join(project_dir, 'problem')
    brief_path = os.path.join(problem_dir, 'problem_brief.md')
    constraints_path = os.path.join(problem_dir, 'constraints.md')

    brief_text = _read_file(brief_path)
    constraints_text = _read_file(constraints_path)
    combined = brief_text + '\n' + constraints_text

    features = {
        'problem_length': len(brief_text),
        'constraint_count': count_constraints(constraints_text),
        'objective_type': detect_objective_type(combined),
        'variable_count': count_variables(combined),
        'has_integer': any(kw in combined.lower() for kw in ['integer', '整数', 'discrete', '离散', '0-1']),
        'has_stochastic': any(kw in combined.lower() for kw in ['stochastic', '随机', 'uncertain', '不确定', 'probability', '概率']),
        'has_nonlinear': any(kw in combined.lower() for kw in ['nonlinear', '非线性', 'quadratic', '二次']),
        'is_multi_objective': any(kw in combined.lower() for kw in ['multi-objective', '多目标', 'pareto']),
        'is_dynamic': any(kw in combined.lower() for kw in ['dynamic', '动态', 'time-varying', '时变', 'trajectory', '轨迹']),
        'has_network': any(kw in combined.lower() for kw in ['graph', '图', 'network', '网络', 'path', '路径']),
        'is_evaluation': any(kw in combined.lower() for kw in ['evaluate', '评价', 'rank', '排序', 'score', '打分']),
    }

    return features


def count_constraints(text):
    """统计约束数量。"""
    markers = [r'subject\s+to', r's\.t\.', r'约束', r'限制', r'\\leq', r'\\geq']
    count = 0
    for marker in markers:
        count += len(re.findall(marker, text, re.IGNORECASE))
    return count


def detect_objective_type(text):
    """检测目标类型。"""
    text_lower = text.lower()
    min_count = len(re.findall(r'minimi[sz]e|最小化', text_lower))
    max_count = len(re.findall(r'maximi[sz]e|最大化', text_lower))

    if min_count > 0 and max_count > 0:
        return 'multi'
    elif min_count > max_count:
        return 'minimize'
    elif max_count > min_count:
        return 'maximize'
    else:
        return 'unknown'


def count_variables(text):
    """估算决策变量数量。"""
    patterns = [
        r'变量.*?[:：]\s*([^。\n]+)',
        r'决策变量.*?[:：]\s*([^。\n]+)',
        r'\$[xyzwXYZW]_\{?[^}$]+\}?\$',
    ]
    matches = []
    for pat in patterns:
        matches.extend(re.findall(pat, text, re.IGNORECASE))
    return len(matches) * 2  # 粗略估计


def extract_methods_used(project_dir):
    """从chosen_method.md提取使用的方法。"""
    chosen_path = os.path.join(project_dir, 'chosen_method.md')
    text = _read_file(chosen_path)

    methods = {
        'primary': None,
        'auxiliary': None,
        'primary_family': None,
        'auxiliary_family': None,
    }

    # 提取PRIMARY行
    primary_match = re.search(r'PRIMARY:\s*(\w+)\s+family=([^\n]+)', text)
    if primary_match:
        methods['primary'] = primary_match.group(1)
        methods['primary_family'] = primary_match.group(2).strip()

    # 提取AUXILIARY行
    aux_match = re.search(r'AUXILIARY:\s*(\w+)\s+family=([^\n]+)', text)
    if aux_match:
        methods['auxiliary'] = aux_match.group(1)
        methods['auxiliary_family'] = aux_match.group(2).strip()

    # 尝试映射到method_library路径
    methods['primary_path'] = find_method_path(methods['primary_family'])
    methods['auxiliary_path'] = find_method_path(methods['auxiliary_family'])

    return methods


def find_method_path(family_desc):
    """根据family描述找到method_library路径（启发式）。"""
    if not family_desc:
        return None

    # 关键词映射
    keyword_map = {
        'milp': 'method_library/optimization/milp.md',
        '混合整数': 'method_library/optimization/milp.md',
        'nlp': 'method_library/optimization/nonlinear_programming.md',
        '非线性规划': 'method_library/optimization/nonlinear_programming.md',
        'ga': 'method_library/metaheuristic/genetic_algorithm.md',
        '遗传算法': 'method_library/metaheuristic/genetic_algorithm.md',
        'pso': 'method_library/metaheuristic/pso.md',
        'ahp': 'method_library/evaluation/ahp.md',
        'topsis': 'method_library/evaluation/topsis.md',
        'arima': 'method_library/prediction/arima.md',
        'ode': 'method_library/dynamics/ode_system.md',
        '动态规划': 'method_library/optimization/dynamic_programming.md',  # 假设存在
        '螺线': 'method_library/geometry/archimedean_spiral.md',
    }

    family_lower = family_desc.lower()
    for kw, path in keyword_map.items():
        if kw in family_lower:
            return path

    return None


def extract_quality_score(project_dir, base_name):
    """Load an explicit human/award label; never learn from self-judging proxies."""
    label_path = os.path.join(project_dir, 'quality_label.json')
    try:
        label = json.loads(_read_file(label_path))
    except Exception:
        label = None
    if isinstance(label, dict):
        score = label.get('score')
        source = str(label.get('source') or '')
        if isinstance(score, (int, float)) and source in {'human', 'award'}:
            return {'score': float(score), 'source': source}
    return {'score': None, 'source': 'unavailable'}


def _is_current_pass(project_dir):
    try:
        manifest = json.loads(_read_file(os.path.join(project_dir, 'delivery_manifest.json')))
    except Exception:
        return False
    return (
        manifest.get('status') == 'CURRENT_PASS'
        and (manifest.get('evaluation') or {}).get('passed') is True
    )


def _registered_paths(complete_dir):
    index_path = os.path.join(os.path.dirname(os.path.abspath(complete_dir)), 'method_library', 'index.json')
    try:
        entries = json.loads(_read_file(index_path))
    except Exception:
        return set()
    return {
        str(entry.get('path'))
        for entry in entries
        if isinstance(entry, dict) and entry.get('path')
    }


def learn_from_history(complete_dir):
    """从complete/目录学习历史模式。"""
    projects = []
    registered_paths = _registered_paths(complete_dir)

    for project_name in os.listdir(complete_dir):
        project_dir = os.path.join(complete_dir, project_name)
        if not os.path.isdir(project_dir):
            continue

        if not _is_current_pass(project_dir):
            continue

        # 需要有chosen_method.md
        if not os.path.exists(os.path.join(project_dir, 'chosen_method.md')):
            continue

        try:
            features = extract_problem_features(project_dir)
            methods = extract_methods_used(project_dir)
            quality = extract_quality_score(project_dir, project_name)

            if quality['score'] is None:
                continue
            if methods.get('primary_path') not in registered_paths:
                continue
            if features['constraint_count'] == 0 and features['variable_count'] == 0:
                continue

            projects.append({
                'project_name': project_name,
                'features': features,
                'methods': methods,
                'quality': quality,
            })
        except Exception as e:
            print(f"[WARN] Skip {project_name}: {e}", file=sys.stderr)
            continue

    # 生成适配度矩阵：method_path × feature → avg_score
    fit_matrix = defaultdict(lambda: {'scores': [], 'count': 0})

    for proj in projects:
        primary_path = proj['methods']['primary_path']
        score = proj['quality']['score']

        if not primary_path:
            continue

        # 为每个feature维度记录
        for feat_key, feat_val in proj['features'].items():
            if isinstance(feat_val, bool):
                if feat_val:
                    key = (primary_path, feat_key, True)
                    fit_matrix[key]['scores'].append(score)
                    fit_matrix[key]['count'] += 1
            elif feat_key in ('objective_type',):
                key = (primary_path, feat_key, feat_val)
                fit_matrix[key]['scores'].append(score)
                fit_matrix[key]['count'] += 1

    # 计算平均分
    fit_scores = {}
    for key, data in fit_matrix.items():
        if data['count'] > 0:
            fit_scores[key] = {
                'avg_score': sum(data['scores']) / len(data['scores']),
                'count': data['count'],
                'std': (sum((s - sum(data['scores']) / len(data['scores']))**2 for s in data['scores']) / len(data['scores']))**0.5 if len(data['scores']) > 1 else 0,
            }

    return {
        'projects': projects,
        'fit_scores': {str(k): v for k, v in fit_scores.items()},  # JSON序列化
        'summary': {
            'total_projects': len(projects),
            'methods_seen': len(set(p['methods']['primary_path'] for p in projects if p['methods']['primary_path'])),
        }
    }


def predict_fit_score(problem_features, method_path, fit_model):
    """预测给定问题特征下某个方法的适配度。"""
    fit_scores = fit_model.get('fit_scores', {})

    # 计算匹配特征的加权平均分
    scores = []
    weights = []

    for feat_key, feat_val in problem_features.items():
        if isinstance(feat_val, bool) and feat_val:
            key = str((method_path, feat_key, True))
            if key in fit_scores:
                scores.append(fit_scores[key]['avg_score'])
                weights.append(fit_scores[key]['count'])
        elif feat_key == 'objective_type':
            key = str((method_path, feat_key, feat_val))
            if key in fit_scores:
                scores.append(fit_scores[key]['avg_score'])
                weights.append(fit_scores[key]['count'])

    if not scores:
        return {
            'score': 50,  # 默认中等
            'confidence': 0.0,
            'reason': 'no_history',
        }

    # 加权平均
    total_weight = sum(weights)
    weighted_score = sum(s * w for s, w in zip(scores, weights)) / total_weight

    return {
        'score': round(weighted_score, 1),
        'confidence': min(1.0, total_weight / 10),  # 10个样本达到满信心
        'reason': f'based_on_{len(scores)}_features',
    }


def main():
    args = sys.argv[1:]

    if len(args) >= 2 and args[0] == '--learn':
        complete_dir = args[1]
        print(f"学习历史项目: {complete_dir}", file=sys.stderr)

        model = learn_from_history(complete_dir)
        print(json.dumps(model, ensure_ascii=False, indent=2))

        # 保存模型
        model_path = os.path.join(os.path.dirname(__file__), 'method_fit_model.json')
        with open(model_path, 'w', encoding='utf-8') as f:
            json.dump(model, f, ensure_ascii=False, indent=2)
        print(f"\n模型已保存: {model_path}", file=sys.stderr)

        sys.exit(0)

    elif len(args) >= 1:
        project_dir = args[0]

        # 加载模型
        model_path = os.path.join(os.path.dirname(__file__), 'method_fit_model.json')
        if not os.path.exists(model_path):
            print(f"ERROR: 模型文件不存在: {model_path}", file=sys.stderr)
            print("请先运行: python3 method_fit_score.py --learn complete/", file=sys.stderr)
            sys.exit(2)

        with open(model_path, 'r', encoding='utf-8') as f:
            model = json.load(f)

        # 提取当前项目特征
        features = extract_problem_features(project_dir)

        # 加载方法库索引
        index_path = os.path.join(os.path.dirname(__file__), '../method_library/index.json')
        if not os.path.exists(index_path):
            print(f"ERROR: method_library/index.json 不存在", file=sys.stderr)
            sys.exit(2)

        with open(index_path, 'r', encoding='utf-8') as f:
            method_index = json.load(f)

        # 为所有方法打分
        recommendations = []
        for method in method_index:
            method_path = method['path']
            fit = predict_fit_score(features, method_path, model)

            recommendations.append({
                'method': method['method'],
                'name_zh': method.get('name_zh', ''),
                'path': method_path,
                'fit_score': fit['score'],
                'confidence': fit['confidence'],
                'reason': fit['reason'],
            })

        # 排序
        recommendations.sort(key=lambda x: x['fit_score'], reverse=True)

        # 输出
        result = {
            'problem_features': features,
            'top_recommendations': recommendations[:10],
            'model_summary': model.get('summary', {}),
        }

        print(json.dumps(result, ensure_ascii=False, indent=2))
        sys.exit(0)

    else:
        print("Usage: method_fit_score.py <project_dir>", file=sys.stderr)
        print("   or: method_fit_score.py --learn <complete_dir>", file=sys.stderr)
        sys.exit(2)


if __name__ == '__main__':
    main()
