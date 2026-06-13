#!/usr/bin/env python3
"""数值溯源链检测器 — 追踪论文中数字从计算源到正文到结论的完整链条。

Usage:
    python3 verify_number_chain.py <project_dir> <base_name>

扩展 verify_numbers.py，不仅检查数字是否在日志中出现，还检查：
  1. 关键数字（results/*.json中标记为"key_result"的值）
  2. 正文中的引用（是否配备了上下文解释）
  3. 结论部分的复述（数字是否从正文传递到结论）

输出：
    KEY_NUMBERS         = N   (results/中的关键数字)
    CITED_IN_BODY       = M   (在正文中被引用的)
    CITED_IN_CONCLUSION = K   (在结论中被复述的)
    CHAIN_BREAKS        = W   (关键数字未传递到结论的次数)

Exit code: 0 if all chains intact, 1 if any chain breaks.
"""

import os
import re
import sys
import json
import glob
from pathlib import Path


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


def extract_key_results(project_dir):
    """从 results/**/*.json 中提取标记为关键结果的数字。

    返回: list of {'value': float, 'label': str, 'source': str}
    """
    key_results = []
    results_dir = os.path.join(project_dir, 'results')
    if not os.path.isdir(results_dir):
        return key_results

    for json_file in glob.glob(os.path.join(results_dir, '**', '*.json'), recursive=True):
        try:
            with open(json_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except (json.JSONDecodeError, IOError):
            continue

        rel_path = os.path.relpath(json_file, project_dir)

        # 检查多种可能的结构
        # 结构1: {"key_results": [{value: ..., label: ...}]}
        if isinstance(data, dict) and 'key_results' in data:
            for item in data['key_results']:
                if isinstance(item, dict) and 'value' in item:
                    try:
                        val = float(item['value'])
                        key_results.append({
                            'value': val,
                            'label': item.get('label', ''),
                            'source': rel_path,
                        })
                    except (ValueError, TypeError):
                        pass

        # 结构2: {"optimal_value": ..., "is_key": true}
        if isinstance(data, dict) and data.get('is_key'):
            for k in ('optimal_value', 'value', 'result'):
                if k in data:
                    try:
                        val = float(data[k])
                        key_results.append({
                            'value': val,
                            'label': data.get('name', k),
                            'source': rel_path,
                        })
                        break
                    except (ValueError, TypeError):
                        pass

        # 结构3: 扁平dict，所有数字都是关键的（demo_result.json风格）
        # 只在文件名包含"summary"/"key"时触发
        if any(kw in os.path.basename(json_file).lower() for kw in ('summary', 'key', 'final')):
            if isinstance(data, dict):
                for k, v in data.items():
                    if isinstance(v, (int, float)):
                        try:
                            val = float(v)
                            if val != 0:
                                key_results.append({
                                    'value': val,
                                    'label': k,
                                    'source': rel_path,
                                })
                        except (ValueError, TypeError):
                            pass

    return key_results


def extract_tex_numbers_detailed(tex_path):
    """提取论文中的数字，带段落上下文和section标记。

    返回: list of {'number': str, 'value': float, 'section': str, 'context': str}
    """
    text = _read_file(tex_path)
    if not text:
        return []

    # 去除preamble
    doc_start = text.find(r'\begin{document}')
    if doc_start >= 0:
        text = text[doc_start:]

    # 去除非正文内容
    for env in ('tabular', 'lstlisting', 'verbatim', 'minted', 'figure', 'table'):
        text = re.sub(rf'\\begin\{{{env}\*?\}}.*?\\end\{{{env}\*?\}}', ' ', text, flags=re.DOTALL)
    text = re.sub(r'<table>.*?</table>', ' ', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'```.*?```', ' ', text, flags=re.DOTALL)
    text = re.sub(r'\\(?:label|ref|cite[tp]?)\{[^}]*\}', '', text)

    results = []
    current_section = 'preamble'

    # 追踪当前section
    section_pattern = re.compile(r'\\(?:section|subsection)\{([^}]+)\}')
    conclusion_keywords = ['结论', 'conclusion', '总结', 'summary', '讨论', 'discussion']

    lines = text.split('\n')
    for i, line in enumerate(lines, 1):
        # 更新section
        sec_match = section_pattern.search(line)
        if sec_match:
            current_section = sec_match.group(1).lower()

        # 跳过注释和命令行
        if line.strip().startswith('%'):
            continue

        # 提取数字
        for m in re.finditer(r'-?\d[\d,]*\.?\d*%?', line):
            num_str = m.group()

            # 过滤规则（同 verify_numbers.py）
            if re.match(r'^(19|20)\d{2}$', num_str):
                continue

            clean = num_str.replace(',', '').rstrip('%')
            try:
                val = float(clean)
            except ValueError:
                continue

            if val == 0:
                continue
            if val == int(val) and 1 <= val <= 20 and '.' not in num_str:
                continue

            # 上下文
            start = max(0, m.start() - 50)
            end = min(len(line), m.end() + 50)
            context = line[start:end].strip()

            # 判断是否在结论部分
            in_conclusion = any(kw in current_section for kw in conclusion_keywords)

            results.append({
                'number': num_str,
                'value': val,
                'section': current_section,
                'context': context,
                'in_conclusion': in_conclusion,
            })

    return results


def match_number(val, target, tolerance=0.02):
    """判断两个数字是否匹配（考虑舍入、缩放）。"""
    if abs(val - target) < 1e-9:
        return True
    if target != 0:
        if abs(val - target) / abs(target) <= tolerance:
            return True
        # 百分数转换
        if abs(val - target * 100) / abs(target * 100) <= tolerance:
            return True
        if abs(val - target / 100) / abs(target / 100) <= tolerance:
            return True
        # 千分转换
        if abs(val - target * 1000) / abs(target * 1000) <= tolerance:
            return True
    return False


def collect_number_chain_metrics(project_dir, base_name):
    """收集数值链指标 dict；不打印。paper 缺失返回 None。"""
    tex_path = os.path.join(project_dir, f'{base_name}_paper.tex')
    if not os.path.exists(tex_path):
        return None

    key_results = extract_key_results(project_dir)
    paper_numbers = extract_tex_numbers_detailed(tex_path)

    if not key_results:
        # 没有标记关键结果，无法做链检查
        return {
            'key_numbers': 0,
            'cited_in_body': 0,
            'cited_in_conclusion': 0,
            'chain_breaks': 0,
            '_key_results': [],
            '_chains': [],
        }

    # 为每个关键结果建立链
    chains = []
    for kr in key_results:
        target = kr['value']
        label = kr['label']
        source = kr['source']

        # 在正文中找匹配
        body_matches = [
            pn for pn in paper_numbers
            if match_number(pn['value'], target) and not pn['in_conclusion']
        ]

        # 在结论中找匹配
        conclusion_matches = [
            pn for pn in paper_numbers
            if match_number(pn['value'], target) and pn['in_conclusion']
        ]

        chain = {
            'label': label,
            'value': target,
            'source': source,
            'in_body': len(body_matches) > 0,
            'in_conclusion': len(conclusion_matches) > 0,
            'body_contexts': [pm['context'] for pm in body_matches[:2]],  # 最多2个
            'conclusion_contexts': [cm['context'] for cm in conclusion_matches[:2]],
        }
        chains.append(chain)

    cited_in_body = sum(1 for c in chains if c['in_body'])
    cited_in_conclusion = sum(1 for c in chains if c['in_conclusion'])
    chain_breaks = sum(1 for c in chains if c['in_body'] and not c['in_conclusion'])

    return {
        'key_numbers': len(key_results),
        'cited_in_body': cited_in_body,
        'cited_in_conclusion': cited_in_conclusion,
        'chain_breaks': chain_breaks,
        '_key_results': key_results,
        '_chains': chains,
    }


def _print_report(project_dir, metrics):
    """打印数值链报告。"""
    print("=== Number Chain Verification Report ===")
    print(f"Project: {project_dir}")
    print()
    print(f"KEY_NUMBERS          = {metrics['key_numbers']}")
    print(f"CITED_IN_BODY        = {metrics['cited_in_body']}")
    print(f"CITED_IN_CONCLUSION  = {metrics['cited_in_conclusion']}")
    print(f"CHAIN_BREAKS         = {metrics['chain_breaks']}")
    print()

    if not metrics['_chains']:
        print("No key results found in results/ directory.")
        return

    # 打印完整链
    print("=" * 70)
    print("Chain Details:")
    print("=" * 70)
    for c in metrics['_chains']:
        status = '✓' if c['in_body'] and c['in_conclusion'] else (
            '⚠' if c['in_body'] else '✗'
        )
        print(f"{status}  {c['label']}: {c['value']}")
        print(f"     Source: {c['source']}")
        if c['in_body']:
            print(f"     Body: {len(c['body_contexts'])} citation(s)")
            for ctx in c['body_contexts'][:1]:
                print(f"         ...{ctx[:60]}...")
        else:
            print(f"     Body: NOT CITED")
        if c['in_conclusion']:
            print(f"     Conclusion: ✓ cited")
        elif c['in_body']:
            print(f"     Conclusion: ✗ NOT propagated (chain break)")
        print()

    # 打印断链汇总
    if metrics['chain_breaks'] > 0:
        print("=" * 70)
        print("Chain Breaks (key numbers cited in body but not in conclusion):")
        print("=" * 70)
        for c in metrics['_chains']:
            if c['in_body'] and not c['in_conclusion']:
                print(f"  {c['label']}: {c['value']} (from {c['source']})")
        print()


def main():
    if len(sys.argv) < 3:
        print("Usage: verify_number_chain.py <project_dir> <base_name>")
        sys.exit(2)

    project_dir = sys.argv[1]
    base_name = sys.argv[2]

    if not os.path.isdir(project_dir):
        print(f"ERROR: {project_dir} not found")
        sys.exit(3)

    metrics = collect_number_chain_metrics(project_dir, base_name)

    if metrics is None:
        tex_path = os.path.join(project_dir, f'{base_name}_paper.tex')
        print(f"ERROR: {tex_path} not found")
        sys.exit(3)

    _print_report(project_dir, metrics)

    # Exit code: 0=all chains intact, 1=any break
    sys.exit(0 if metrics['chain_breaks'] == 0 else 1)


if __name__ == '__main__':
    main()
