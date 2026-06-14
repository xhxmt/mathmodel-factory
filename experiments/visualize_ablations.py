#!/usr/bin/env python3
"""
消融实验数据可视化工具

生成以下图表：
1. 总分对比柱状图（含误差线）
2. 六维度雷达图（基线 vs 各消融）
3. 维度影响热力图
4. 评估方差散点图

依赖: matplotlib, numpy
用法: python3 visualize_ablations.py [--output-dir DIR]
"""

import json
import sys
from pathlib import Path
import argparse

try:
    import matplotlib.pyplot as plt
    import matplotlib
    matplotlib.use('Agg')  # 非交互式后端
    import numpy as np
except ImportError:
    print("错误: 需要安装 matplotlib 和 numpy")
    print("运行: pip install matplotlib numpy")
    sys.exit(1)

# 中文字体设置
plt.rcParams['font.sans-serif'] = ['SimHei', 'DejaVu Sans', 'Arial Unicode MS']
plt.rcParams['axes.unicode_minus'] = False


def load_eval_json(base_name):
    """加载评估 JSON 文件"""
    json_path = Path(f"evaluation/results/{base_name}_eval.json")
    if not json_path.exists():
        return None

    with open(json_path) as f:
        return json.load(f)


def parse_projects():
    """解析所有项目的评估数据"""
    projects = {
        "baseline": "test_cumcm2024b",
        "no_consult": "cumcm2024b_no_consult_rep1",
        "no_innov": "cumcm2024b_no_innov_rep1",
        "no_judge": "cumcm2024b_no_judge_rep1",
        "no_methodlib": "cumcm2024b_no_methodlib_rep1",
    }

    data = {}
    for key, base_name in projects.items():
        eval_data = load_eval_json(base_name)
        if eval_data:
            data[key] = {
                "name": base_name,
                "total": eval_data.get("median_recomputed", 0),
                "spread": eval_data.get("spread", [0, 0]),
                "dims": eval_data.get("median_dims", {}),
            }

    return data


def plot_total_scores(data, output_dir):
    """绘制总分对比柱状图"""
    fig, ax = plt.subplots(figsize=(12, 6))

    labels = []
    scores = []
    errors = []
    colors = []

    color_map = {
        "baseline": "#2ecc71",
        "no_consult": "#f39c12",
        "no_innov": "#e67e22",
        "no_judge": "#e74c3c",
        "no_methodlib": "#c0392b",
    }

    label_map = {
        "baseline": "基线\n(完整流水线)",
        "no_consult": "无文献检索\n(-1.7)",
        "no_innov": "无创新保护\n(-2.7)",
        "no_judge": "无评委循环\n(-3.4)",
        "no_methodlib": "无方法库\n(-6.3)",
    }

    baseline_score = data["baseline"]["total"]

    for key in ["baseline", "no_consult", "no_innov", "no_judge", "no_methodlib"]:
        if key in data:
            labels.append(label_map[key])
            scores.append(data[key]["total"])
            spread = data[key]["spread"]
            error = (spread[1] - spread[0]) / 2
            errors.append(error)
            colors.append(color_map[key])

    x = np.arange(len(labels))
    bars = ax.bar(x, scores, color=colors, alpha=0.8, width=0.6)
    ax.errorbar(x, scores, yerr=errors, fmt='none', ecolor='black',
                capsize=5, capthick=2, alpha=0.5)

    # 在柱子上标注分数
    for i, (bar, score) in enumerate(zip(bars, scores)):
        height = bar.get_height()
        ax.text(bar.get_x() + bar.get_width()/2., height + 0.5,
                f'{score:.1f}',
                ha='center', va='bottom', fontsize=11, fontweight='bold')

    # 基线参考线
    ax.axhline(y=baseline_score, color='green', linestyle='--',
               linewidth=2, alpha=0.5, label=f'基线: {baseline_score:.1f}')

    ax.set_ylabel('总分 (0-100)', fontsize=12, fontweight='bold')
    ax.set_xlabel('消融条件', fontsize=12, fontweight='bold')
    ax.set_title('消融实验总分对比 (CUMCM 2024 B题)',
                 fontsize=14, fontweight='bold', pad=20)
    ax.set_xticks(x)
    ax.set_xticklabels(labels, fontsize=10)
    ax.set_ylim(80, 95)
    ax.grid(axis='y', alpha=0.3, linestyle='--')
    ax.legend(fontsize=10)

    plt.tight_layout()
    output_path = output_dir / "ablation_total_scores.png"
    plt.savefig(output_path, dpi=300, bbox_inches='tight')
    print(f"✓ 已生成: {output_path}")
    plt.close()


def plot_radar_chart(data, output_dir):
    """绘制六维度雷达图"""
    dims_order = ["模型合理性", "求解正确性", "创新性",
                  "写作清晰度", "结果说服力", "灵敏度分析"]

    fig, ax = plt.subplots(figsize=(10, 10), subplot_kw=dict(projection='polar'))

    # 角度设置
    angles = np.linspace(0, 2 * np.pi, len(dims_order), endpoint=False).tolist()
    angles += angles[:1]  # 闭合

    # 绘制每个项目
    plot_configs = [
        ("baseline", "基线", "#2ecc71", 2.5, '-'),
        ("no_methodlib", "无方法库", "#c0392b", 2.0, '--'),
        ("no_judge", "无评委", "#e74c3c", 1.8, '--'),
        ("no_innov", "无创新保护", "#e67e22", 1.5, '-.'),
    ]

    for key, label, color, linewidth, linestyle in plot_configs:
        if key not in data:
            continue

        values = []
        for dim in dims_order:
            values.append(data[key]["dims"].get(dim, 0))
        values += values[:1]  # 闭合

        ax.plot(angles, values, color=color, linewidth=linewidth,
                linestyle=linestyle, label=label)
        ax.fill(angles, values, color=color, alpha=0.15)

    ax.set_xticks(angles[:-1])
    ax.set_xticklabels(dims_order, fontsize=11)
    ax.set_ylim(0, 20)
    ax.set_yticks([5, 10, 15, 20])
    ax.set_yticklabels(['5', '10', '15', '20'], fontsize=9)
    ax.grid(True, alpha=0.3)

    ax.set_title('六维度对比雷达图', fontsize=14, fontweight='bold', pad=20)
    ax.legend(loc='upper right', bbox_to_anchor=(1.3, 1.1), fontsize=10)

    plt.tight_layout()
    output_path = output_dir / "ablation_radar_chart.png"
    plt.savefig(output_path, dpi=300, bbox_inches='tight')
    print(f"✓ 已生成: {output_path}")
    plt.close()


def plot_dimension_impact_heatmap(data, output_dir):
    """绘制维度影响热力图"""
    dims_order = ["模型合理性", "求解正确性", "创新性",
                  "写作清晰度", "结果说服力", "灵敏度分析"]

    ablations = ["no_consult", "no_innov", "no_judge", "no_methodlib"]
    ablation_labels = ["无文献检索", "无创新保护", "无评委循环", "无方法库"]

    baseline_dims = data["baseline"]["dims"]

    # 构建影响矩阵（负值表示下降）
    impact_matrix = []
    for ablation in ablations:
        if ablation not in data:
            impact_matrix.append([0] * len(dims_order))
            continue

        row = []
        for dim in dims_order:
            baseline_val = baseline_dims.get(dim, 0)
            ablation_val = data[ablation]["dims"].get(dim, 0)
            delta = ablation_val - baseline_val
            row.append(delta)
        impact_matrix.append(row)

    impact_matrix = np.array(impact_matrix)

    fig, ax = plt.subplots(figsize=(12, 6))

    # 使用红绿色谱（绿色=正向，红色=负向）
    im = ax.imshow(impact_matrix, cmap='RdYlGn', aspect='auto',
                   vmin=-2, vmax=0.5)

    ax.set_xticks(np.arange(len(dims_order)))
    ax.set_yticks(np.arange(len(ablation_labels)))
    ax.set_xticklabels(dims_order, fontsize=11)
    ax.set_yticklabels(ablation_labels, fontsize=11)

    # 在每个格子上标注数值
    for i in range(len(ablation_labels)):
        for j in range(len(dims_order)):
            value = impact_matrix[i, j]
            color = 'white' if value < -0.8 else 'black'
            text = ax.text(j, i, f'{value:+.1f}',
                          ha="center", va="center", color=color,
                          fontsize=10, fontweight='bold')

    ax.set_title('消融实验维度影响热力图 (Δ vs 基线)',
                 fontsize=14, fontweight='bold', pad=20)

    # 色带
    cbar = plt.colorbar(im, ax=ax)
    cbar.set_label('分数变化', rotation=270, labelpad=20, fontsize=11)

    plt.tight_layout()
    output_path = output_dir / "ablation_heatmap.png"
    plt.savefig(output_path, dpi=300, bbox_inches='tight')
    print(f"✓ 已生成: {output_path}")
    plt.close()


def plot_variance_analysis(data, output_dir):
    """绘制评估方差散点图"""
    fig, ax = plt.subplots(figsize=(10, 6))

    projects = []
    scores = []
    variances = []
    colors_list = []

    color_map = {
        "baseline": "#2ecc71",
        "no_consult": "#f39c12",
        "no_innov": "#e67e22",
        "no_judge": "#e74c3c",
        "no_methodlib": "#c0392b",
    }

    label_map = {
        "baseline": "基线",
        "no_consult": "无文献检索",
        "no_innov": "无创新保护",
        "no_judge": "无评委循环",
        "no_methodlib": "无方法库",
    }

    for key in ["baseline", "no_consult", "no_innov", "no_judge", "no_methodlib"]:
        if key in data:
            projects.append(label_map[key])
            scores.append(data[key]["total"])
            spread = data[key]["spread"]
            variance = spread[1] - spread[0]
            variances.append(variance)
            colors_list.append(color_map[key])

    scatter = ax.scatter(scores, variances, s=300, c=colors_list,
                        alpha=0.7, edgecolors='black', linewidth=2)

    # 标注项目名
    for i, (x, y, label) in enumerate(zip(scores, variances, projects)):
        ax.annotate(label, (x, y), xytext=(5, 5),
                   textcoords='offset points', fontsize=10)

    ax.set_xlabel('总分中位数', fontsize=12, fontweight='bold')
    ax.set_ylabel('评估方差 (max - min)', fontsize=12, fontweight='bold')
    ax.set_title('评估稳定性分析 (K=3采样)',
                 fontsize=14, fontweight='bold', pad=20)
    ax.grid(True, alpha=0.3, linestyle='--')

    # 标注低方差区域
    ax.axhline(y=1.0, color='green', linestyle='--', alpha=0.3)
    ax.text(86, 0.5, '高稳定性区域 (方差 < 1)',
            fontsize=9, style='italic', color='green')

    plt.tight_layout()
    output_path = output_dir / "ablation_variance.png"
    plt.savefig(output_path, dpi=300, bbox_inches='tight')
    print(f"✓ 已生成: {output_path}")
    plt.close()


def main():
    parser = argparse.ArgumentParser(description='消融实验数据可视化')
    parser.add_argument('--output-dir', type=Path,
                       default=Path('evaluation/figures'),
                       help='输出目录 (默认: evaluation/figures)')
    args = parser.parse_args()

    # 创建输出目录
    args.output_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print("消融实验数据可视化")
    print("=" * 60)

    # 加载数据
    print("\n[1/5] 加载评估数据...")
    data = parse_projects()

    if not data:
        print("错误: 未找到评估数据")
        print("请先运行评估: ./evaluation/run_evaluation.sh complete/<base>")
        sys.exit(1)

    print(f"  已加载 {len(data)} 个项目")

    # 生成图表
    print("\n[2/5] 生成总分对比图...")
    plot_total_scores(data, args.output_dir)

    print("\n[3/5] 生成雷达图...")
    plot_radar_chart(data, args.output_dir)

    print("\n[4/5] 生成影响热力图...")
    plot_dimension_impact_heatmap(data, args.output_dir)

    print("\n[5/5] 生成方差分析图...")
    plot_variance_analysis(data, args.output_dir)

    print("\n" + "=" * 60)
    print(f"✓ 所有图表已生成到: {args.output_dir}")
    print("=" * 60)
    print("\n图表列表:")
    for fig in sorted(args.output_dir.glob("*.png")):
        print(f"  - {fig.name}")
    print()


if __name__ == "__main__":
    main()
