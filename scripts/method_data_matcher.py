#!/usr/bin/env python3
"""
Method Library 数据需求匹配度评分工具

为每个方法评估其数据需求与项目实际数据的匹配度,提前发现数据缺失问题。

功能:
1. 解析 method_library/index.json 中的 required_data 字段
2. 对比项目的 problem/data_inventory.md
3. 计算匹配度评分(0-100)
4. 生成数据可行性预警

使用:
  ./scripts/method_data_matcher.py <project_path>
  ./scripts/method_data_matcher.py <project_path> --method AHP
  ./scripts/method_data_matcher.py <project_path> --top 5
"""

import sys
import json
import argparse
from pathlib import Path
from typing import Dict, List, Tuple

def load_method_registry(factory_root: Path) -> List[Dict]:
    """加载方法库注册表"""
    registry_path = factory_root / "method_library" / "index.json"
    if not registry_path.exists():
        print(f"错误: 方法库注册表不存在: {registry_path}", file=sys.stderr)
        sys.exit(1)

    with open(registry_path, 'r', encoding='utf-8') as f:
        return json.load(f)

def parse_data_inventory(project_path: Path) -> Dict:
    """解析项目的数据清单"""
    inventory_path = project_path / "problem" / "data_inventory.md"
    if not inventory_path.exists():
        return {
            "provided": [],
            "missing": [],
            "sources": []
        }

    content = inventory_path.read_text(encoding='utf-8')
    lines = content.split('\n')

    result = {
        "provided": [],
        "missing": [],
        "sources": []
    }

    current_section = None

    for line in lines:
        line_lower = line.lower()

        if "已提供" in line or "provided" in line_lower or "supplied" in line_lower:
            current_section = "provided"
        elif "缺失" in line or "missing" in line_lower or "lacking" in line_lower:
            current_section = "missing"
        elif "来源" in line or "source" in line_lower or "external" in line_lower:
            current_section = "sources"
        elif line.strip().startswith('-') or line.strip().startswith('*'):
            # 列表项
            item = line.strip().lstrip('-*').strip()
            if current_section and item:
                result[current_section].append(item)

    return result

def compute_data_match_score(method: Dict, inventory: Dict) -> Tuple[float, List[str], List[str]]:
    """
    计算方法的数据匹配度评分

    返回: (评分 0-100, 已满足的需求列表, 缺失的需求列表)
    """
    required_data = method.get("required_data", [])
    if not required_data:
        return 100.0, [], []  # 没有数据需求视为完全匹配

    provided_text = " ".join(inventory["provided"]).lower()
    sources_text = " ".join(inventory["sources"]).lower()
    missing_text = " ".join(inventory["missing"]).lower()

    satisfied = []
    lacking = []

    for requirement in required_data:
        req_lower = requirement.lower()

        # 关键词匹配(简化版)
        keywords = req_lower.split()

        # 检查是否在已提供或来源中
        found_in_provided = any(kw in provided_text for kw in keywords)
        found_in_sources = any(kw in sources_text for kw in keywords)

        # 检查是否明确标记为缺失
        marked_missing = any(kw in missing_text for kw in keywords)

        if (found_in_provided or found_in_sources) and not marked_missing:
            satisfied.append(requirement)
        else:
            lacking.append(requirement)

    # 评分 = (已满足 / 总需求) * 100
    score = (len(satisfied) / len(required_data)) * 100 if required_data else 100.0

    return score, satisfied, lacking

def rank_methods_by_data_match(methods: List[Dict], inventory: Dict) -> List[Tuple[Dict, float, List[str], List[str]]]:
    """
    按数据匹配度对方法排序

    返回: [(method, score, satisfied, lacking), ...]
    """
    results = []
    for method in methods:
        score, satisfied, lacking = compute_data_match_score(method, inventory)
        results.append((method, score, satisfied, lacking))

    # 按评分降序排序
    results.sort(key=lambda x: x[1], reverse=True)
    return results

def print_method_data_match(method: Dict, score: float, satisfied: List[str], lacking: List[str], show_details: bool = True):
    """打印单个方法的数据匹配情况"""
    method_name = method.get("method", "Unknown")
    name_zh = method.get("name_zh", "")
    domain = method.get("domain", "")

    # 评分颜色
    if score >= 80:
        score_icon = "✅"
    elif score >= 50:
        score_icon = "⚠️"
    else:
        score_icon = "❌"

    print(f"\n{score_icon} {method_name} ({name_zh}) — {domain}")
    print(f"   数据匹配度: {score:.0f}/100")

    if show_details:
        if satisfied:
            print(f"   ✅ 已满足 ({len(satisfied)}):")
            for req in satisfied:
                print(f"      - {req}")

        if lacking:
            print(f"   ❌ 缺失 ({len(lacking)}):")
            for req in lacking:
                print(f"      - {req}")

def generate_data_feasibility_warnings(ranked_methods: List, threshold: float = 50.0) -> List[str]:
    """生成数据可行性预警"""
    warnings = []

    low_score_methods = [(m, s) for m, s, _, _ in ranked_methods if s < threshold]

    if low_score_methods:
        warnings.append(f"⚠️  {len(low_score_methods)} 个方法数据匹配度 < {threshold}%:")
        for method, score in low_score_methods[:5]:  # 最多显示 5 个
            method_name = method.get("method", "Unknown")
            warnings.append(f"   - {method_name}: {score:.0f}%")

    # 检查是否所有方法都缺少某个公共数据
    all_lacking = set()
    for method, score, satisfied, lacking in ranked_methods:
        all_lacking.update(lacking)

    common_lacking = []
    for req in all_lacking:
        count = sum(1 for _, _, _, lacking in ranked_methods if req in lacking)
        ratio = count / len(ranked_methods)
        if ratio >= 0.5:  # 超过 50% 的方法缺少此数据
            common_lacking.append((req, count))

    if common_lacking:
        warnings.append(f"\n⚠️  普遍缺失的数据需求:")
        for req, count in sorted(common_lacking, key=lambda x: x[1], reverse=True):
            warnings.append(f"   - {req} (影响 {count} 个方法)")

    return warnings

def main():
    parser = argparse.ArgumentParser(description="Method Library 数据匹配度评分")
    parser.add_argument("project_path", type=Path, help="项目目录路径")
    parser.add_argument("--method", help="仅评估指定方法(如 AHP)")
    parser.add_argument("--top", type=int, help="显示前 N 个匹配度最高的方法")
    parser.add_argument("--threshold", type=float, default=50.0, help="预警阈值(默认 50)")
    parser.add_argument("--factory", type=Path, help="Factory 根目录(默认从项目路径推断)")
    args = parser.parse_args()

    project_path = args.project_path.resolve()
    if not project_path.exists():
        print(f"错误: 项目目录不存在: {project_path}", file=sys.stderr)
        sys.exit(1)

    # 推断 factory 根目录
    if args.factory:
        factory_root = args.factory.resolve()
    else:
        factory_root = project_path.parent.parent

    if not (factory_root / "method_library").exists():
        print(f"错误: 无法找到 method_library/ 目录于 {factory_root}", file=sys.stderr)
        sys.exit(1)

    # 加载方法库和数据清单
    methods = load_method_registry(factory_root)
    inventory = parse_data_inventory(project_path)

    print(f"Method Library 数据匹配度分析")
    print(f"项目: {project_path.name}")
    print(f"方法总数: {len(methods)}")
    print("=" * 60)

    print(f"\n数据清单概况:")
    print(f"  已提供: {len(inventory['provided'])} 项")
    print(f"  缺失: {len(inventory['missing'])} 项")
    print(f"  外部来源: {len(inventory['sources'])} 项")

    # 如果指定了方法,只评估该方法
    if args.method:
        method = next((m for m in methods if m.get("method", "").lower() == args.method.lower()), None)
        if not method:
            print(f"\n错误: 未找到方法 '{args.method}'", file=sys.stderr)
            sys.exit(1)

        score, satisfied, lacking = compute_data_match_score(method, inventory)
        print_method_data_match(method, score, satisfied, lacking, show_details=True)
        sys.exit(0)

    # 对所有方法排序
    ranked_methods = rank_methods_by_data_match(methods, inventory)

    # 显示 top N
    if args.top:
        print(f"\n数据匹配度 Top {args.top}:")
        print("-" * 60)
        for method, score, satisfied, lacking in ranked_methods[:args.top]:
            print_method_data_match(method, score, satisfied, lacking, show_details=True)
    else:
        # 显示所有(简化版)
        print(f"\n所有方法数据匹配度:")
        print("-" * 60)
        for method, score, satisfied, lacking in ranked_methods:
            print_method_data_match(method, score, satisfied, lacking, show_details=False)

    # 生成预警
    warnings = generate_data_feasibility_warnings(ranked_methods, args.threshold)
    if warnings:
        print("\n" + "=" * 60)
        print("数据可行性预警:")
        print("=" * 60)
        for warning in warnings:
            print(warning)

    # 建议
    print("\n" + "=" * 60)
    print("建议:")
    print("=" * 60)

    high_score_count = sum(1 for _, s, _, _ in ranked_methods if s >= 80)
    medium_score_count = sum(1 for _, s, _, _ in ranked_methods if 50 <= s < 80)
    low_score_count = sum(1 for _, s, _, _ in ranked_methods if s < 50)

    print(f"  - {high_score_count} 个方法数据完备(≥80%),可直接使用")
    print(f"  - {medium_score_count} 个方法数据部分缺失(50-80%),需补充数据")
    print(f"  - {low_score_count} 个方法数据严重不足(<50%),不建议使用")

    if low_score_count > len(methods) * 0.5:
        print(f"\n⚠️  超过 50% 的方法数据不足,建议:")
        print(f"     1. 检查 problem/data_inventory.md 是否完整")
        print(f"     2. 考虑补充外部数据源")
        print(f"     3. 优先选择高匹配度方法")

if __name__ == "__main__":
    main()
