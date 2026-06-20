#!/usr/bin/env python3
"""
生成论文代码附录（精简版）

用途：
- 从 models/ 目录提取核心代码文件
- 按"最大价值优先"策略精简（避免超页数限制）
- 生成 LaTeX \lstinputlisting 代码块或直接嵌入
- 估算页数，提供压缩建议

策略：
- MCM/ICM（≤25页）：仅核心求解文件（03_solve.py）
- CUMCM（无硬限）：完整代码清单

使用：
    python3 generate_code_appendix.py <project_dir> <base_name> [--mode=cumcm|mcm]
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path


def estimate_latex_pages(lines: int) -> float:
    """估算 LaTeX lstlisting 代码的页数（粗略）"""
    # 假设：12pt 字体，lstlisting 单倍行距，每页约 50 行代码
    return lines / 50.0


def get_file_priority(filepath: Path) -> int:
    """返回文件优先级（数字越小越优先）"""
    name = filepath.name.lower()

    # 核心求解文件（最高优先级）
    if '03_solve' in name or 'solve' in name:
        return 1

    # 模型构建文件
    if '02_model' in name or 'model' in name:
        return 2

    # 数据处理文件
    if '01_data' in name or 'data' in name:
        return 3

    # 后处理文件
    if '04_postprocess' in name or 'postprocess' in name:
        return 4

    # 灵敏度文件
    if '05_sensitivity' in name or 'sensitivity' in name:
        return 5

    # 画图文件（优先级最低）
    if '06_figures' in name or 'figures' in name or 'plot' in name:
        return 6

    # 其他文件
    return 7


def collect_code_files(project_dir: Path) -> list:
    """收集所有代码文件及其元信息"""
    models_dir = project_dir / 'models'
    if not models_dir.exists():
        return []

    files = []
    for py_file in models_dir.rglob('*.py'):
        # 跳过 __init__.py 和测试文件
        if py_file.name == '__init__.py' or 'test_' in py_file.name:
            continue

        # 读取行数
        try:
            with open(py_file, 'r', encoding='utf-8') as f:
                lines = len(f.readlines())
        except:
            lines = 0

        files.append({
            'path': py_file,
            'relative_path': py_file.relative_to(project_dir),
            'name': py_file.name,
            'lines': lines,
            'priority': get_file_priority(py_file),
            'pages': estimate_latex_pages(lines)
        })

    # 按优先级排序
    files.sort(key=lambda x: (x['priority'], -x['lines']))
    return files


def generate_appendix_cumcm(files: list, project_dir: Path) -> str:
    """生成 CUMCM 模式附录（完整代码）"""
    latex = []
    latex.append(r"\appendix")
    latex.append(r"\section{代码清单}")
    latex.append("")

    total_lines = 0
    for f in files:
        total_lines += f['lines']
        latex.append(f"\\subsection{{{f['name']}}}")
        latex.append(f"% 文件: {f['relative_path']}, {f['lines']} 行")
        latex.append(r"\begin{lstlisting}[language=Python, basicstyle=\ttfamily\small]")
        latex.append(f"% 路径: {f['relative_path']}")
        latex.append(r"\end{lstlisting}")
        latex.append(f"% 实际使用时替换为: \\lstinputlisting[language=Python]{{{f['relative_path']}}}")
        latex.append("")

    # 统计信息
    latex.insert(2, f"% 代码总行数: {total_lines}, 估算页数: {estimate_latex_pages(total_lines):.1f}")
    latex.insert(3, f"% 文件总数: {len(files)}")
    latex.insert(4, "")

    return "\n".join(latex)


def generate_appendix_mcm(files: list, project_dir: Path, page_budget: float = 3.0) -> str:
    """生成 MCM/ICM 模式附录（精简，控制页数）"""
    latex = []
    latex.append(r"\appendix")
    latex.append(r"\section{Code Listings}")
    latex.append(f"% Page budget: {page_budget:.1f} pages")
    latex.append("")

    # 累积选择文件，直到达到页数预算
    selected = []
    cumulative_pages = 0.0

    for f in files:
        if cumulative_pages + f['pages'] <= page_budget:
            selected.append(f)
            cumulative_pages += f['pages']
        else:
            # 预算不足，只能截断
            remaining_lines = int((page_budget - cumulative_pages) * 50)
            if remaining_lines > 20:  # 至少保留 20 行才有意义
                selected.append({
                    **f,
                    'lines': remaining_lines,
                    'pages': estimate_latex_pages(remaining_lines),
                    'truncated': True
                })
            break

    # 生成 LaTeX
    for f in selected:
        truncated_tag = " (truncated)" if f.get('truncated') else ""
        latex.append(f"\\subsection{{{f['name']}{truncated_tag}}}")
        latex.append(f"% Priority {f['priority']}, {f['lines']} lines, ~{f['pages']:.1f} pages")
        latex.append(r"\begin{lstlisting}[language=Python, basicstyle=\ttfamily\footnotesize]")
        latex.append(f"% 路径: {f['relative_path']}")
        latex.append(r"\end{lstlisting}")
        latex.append(f"% 实际使用时替换为: \\lstinputlisting[language=Python]{{{f['relative_path']}}}")
        latex.append("")

    # 统计信息
    omitted = len(files) - len(selected)
    if omitted > 0:
        latex.append(f"% 省略 {omitted} 个低优先级文件以控制页数")
        latex.append("% 省略文件清单:")
        for f in files[len(selected):]:
            latex.append(f"%   - {f['name']} ({f['lines']} lines, priority {f['priority']})")

    return "\n".join(latex)


def main():
    parser = argparse.ArgumentParser(description='生成论文代码附录（精简版）')
    parser.add_argument('project_dir', help='项目目录路径')
    parser.add_argument('base_name', help='项目基础名称')
    parser.add_argument('--mode', choices=['cumcm', 'mcm'], default='cumcm',
                        help='竞赛模式（cumcm=完整，mcm=精简）')
    parser.add_argument('--mcm-page-budget', type=float, default=3.0,
                        help='MCM 模式下代码附录页数预算（默认 3 页）')
    parser.add_argument('--output', help='输出文件路径（默认：project_dir/paper/appendix_code.tex）')

    args = parser.parse_args()

    project_dir = Path(args.project_dir).resolve()
    if not project_dir.exists():
        print(f"❌ 项目目录不存在: {project_dir}", file=sys.stderr)
        sys.exit(1)

    # 收集代码文件
    files = collect_code_files(project_dir)
    if not files:
        print("⚠️  未找到任何代码文件", file=sys.stderr)
        sys.exit(0)

    # 生成附录
    if args.mode == 'cumcm':
        appendix = generate_appendix_cumcm(files, project_dir)
    else:
        appendix = generate_appendix_mcm(files, project_dir, args.mcm_page_budget)

    # 输出
    if args.output:
        output_file = Path(args.output)
    else:
        output_file = project_dir / 'paper' / 'appendix_code.tex'

    output_file.parent.mkdir(parents=True, exist_ok=True)
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(appendix)

    # 统计信息
    total_lines = sum(f['lines'] for f in files)
    total_pages = estimate_latex_pages(total_lines)

    print(f"✅ 代码附录已生成: {output_file}")
    print(f"📊 统计:")
    print(f"   - 模式: {args.mode.upper()}")
    print(f"   - 总文件数: {len(files)}")
    print(f"   - 总代码行数: {total_lines}")
    print(f"   - 估算页数（完整）: {total_pages:.1f}")

    if args.mode == 'mcm':
        selected_lines = sum(f['lines'] for f in files[:3])  # 粗略估计
        selected_pages = estimate_latex_pages(selected_lines)
        print(f"   - 精简后页数: {selected_pages:.1f} (预算 {args.mcm_page_budget:.1f})")
        print(f"   - 压缩率: {selected_lines/total_lines*100:.1f}%")


if __name__ == '__main__':
    main()
