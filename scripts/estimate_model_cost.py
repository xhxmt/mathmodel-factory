#!/usr/bin/env python3
"""
模型成本预估工具

基于历史数据和模型定价,预估完整运行一个项目的成本和 token 消耗。

使用:
  ./scripts/estimate_model_cost.py --config ongoing/<project>/model_config.json
  ./scripts/estimate_model_cost.py --preset balanced
"""

import sys
import json
import argparse
from pathlib import Path
from typing import Dict, Tuple

# 模型定价 (USD per 1M tokens)
MODEL_PRICING = {
    "claude": {"input": 0.0, "output": 0.0, "note": "订阅制(Claude Pro ~$20/月)"},
    "codex-gpt55": {"input": 0.0, "output": 0.0, "note": "取决于 Codex 定价"},
    "agy-gemini": {"input": 0.0, "output": 0.0, "note": "取决于 Antigravity 定价"},
    "deepseek-chat": {"input": 0.14, "output": 0.28, "note": "DeepSeek API"},
    "deepseek-reasoner": {"input": 0.55, "output": 2.19, "note": "DeepSeek Reasoner"},
    "gemini-api": {"input": 1.25, "output": 5.00, "note": "Gemini 2.5 Pro API"},
    "qwen-max": {"input": 0.40, "output": 1.20, "note": "阿里云 Qwen Max"},
}

# 典型步骤 token 消耗 (input, output) - 基于 CUMCM 2024B 历史数据
STEP_TOKEN_ESTIMATES = {
    "step_1": (50000, 15000),    # 背景研究
    "step_2": (80000, 40000),    # 并行建模提案(多流并发)
    "step_3": (30000, 5000),     # 方法选择
    "step_4": (100000, 50000),   # 模型构建
    "step_5": (120000, 60000),   # 求解编排
    "step_6": (80000, 40000),    # 敏感性分析
    "step_7": (60000, 30000),    # 模型评价
    "step_8": (40000, 20000),    # 可视化精修
    "step_9": (150000, 80000),   # 论文起稿
    "step_10": (50000, 20000),   # Gate 1 数值检查
    "step_11": (80000, 40000),   # 建设性审稿
    "step_12": (100000, 60000),  # 修订
    "step_13": (120000, 50000),  # Gate 2 评委打分
    "step_14": (40000, 10000),   # 摘要
    "step_15": (60000, 30000),   # 引用审计+润色
    "step_16": (20000, 5000),    # 编译打包
}

TOTAL_ESTIMATE = (1180000, 555000)  # 总计约 1.18M input, 0.56M output

def load_config(config_path: Path) -> Dict:
    """加载模型配置"""
    if not config_path.exists():
        return {"_default": {}}

    with open(config_path, 'r', encoding='utf-8') as f:
        return json.load(f)

def estimate_step_cost(step: str, model_id: str, input_tokens: int, output_tokens: int) -> Tuple[float, str]:
    """估算单个步骤的成本"""
    pricing = MODEL_PRICING.get(model_id, {"input": 0.0, "output": 0.0, "note": "未知定价"})

    input_cost = (input_tokens / 1_000_000) * pricing["input"]
    output_cost = (output_tokens / 1_000_000) * pricing["output"]
    total_cost = input_cost + output_cost

    note = pricing.get("note", "")
    return total_cost, note

def estimate_project_cost(config: Dict, default_model: str = "claude") -> Dict:
    """估算整个项目的成本"""
    step_configs = config.get("_default", {})

    results = {
        "total_cost": 0.0,
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "steps": {}
    }

    for step, (input_tokens, output_tokens) in STEP_TOKEN_ESTIMATES.items():
        # 确定该步骤使用的模型
        step_config = step_configs.get(step, {})
        model_id = step_config.get("primary", default_model)

        cost, note = estimate_step_cost(step, model_id, input_tokens, output_tokens)

        results["steps"][step] = {
            "model": model_id,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost": cost,
            "note": note
        }

        results["total_cost"] += cost
        results["total_input_tokens"] += input_tokens
        results["total_output_tokens"] += output_tokens

    return results

def print_estimate(results: Dict):
    """打印成本估算结果"""
    print("\n" + "=" * 80)
    print("模型成本预估报告")
    print("=" * 80)

    print(f"\n总输入 tokens: {results['total_input_tokens']:,}")
    print(f"总输出 tokens: {results['total_output_tokens']:,}")
    print(f"总计 tokens: {results['total_input_tokens'] + results['total_output_tokens']:,}")

    print(f"\n总成本: ${results['total_cost']:.2f} USD")

    # 按成本排序步骤
    sorted_steps = sorted(results['steps'].items(), key=lambda x: x[1]['cost'], reverse=True)

    print("\n成本最高的 5 个步骤:")
    print("-" * 80)
    for i, (step, info) in enumerate(sorted_steps[:5], 1):
        print(f"{i}. {step} ({info['model']}): ${info['cost']:.2f}")
        print(f"   Input: {info['input_tokens']:,}, Output: {info['output_tokens']:,}")

    print("\n完整步骤成本明细:")
    print("-" * 80)
    print(f"{'步骤':<12} {'模型':<20} {'Input Tokens':>15} {'Output Tokens':>15} {'成本 (USD)':>12}")
    print("-" * 80)

    for step in sorted(results['steps'].keys(), key=lambda x: int(x.split('_')[1])):
        info = results['steps'][step]
        print(f"{step:<12} {info['model']:<20} {info['input_tokens']:>15,} {info['output_tokens']:>15,} ${info['cost']:>11.2f}")

    print("-" * 80)
    print(f"{'总计':<12} {'':<20} {results['total_input_tokens']:>15,} {results['total_output_tokens']:>15,} ${results['total_cost']:>11.2f}")

    # 订阅制模型提示
    subscription_models = [step for step, info in results['steps'].items()
                          if info['cost'] == 0.0 and info['model'] in ['claude', 'codex-gpt55', 'agy-gemini']]

    if subscription_models:
        print(f"\n注意: {len(subscription_models)} 个步骤使用订阅制模型(成本未计入上述总额)")
        print("      实际成本取决于订阅费用(如 Claude Pro ~$20/月)")

def main():
    parser = argparse.ArgumentParser(description="模型成本预估")
    parser.add_argument("--config", type=Path, help="model_config.json 路径")
    parser.add_argument("--preset", help="使用预设方案(default/balanced/high-quality/cost-optimized)")
    parser.add_argument("--default-model", default="claude", help="默认模型(未配置步骤使用)")
    args = parser.parse_args()

    if args.config:
        if not args.config.exists():
            print(f"错误: 配置文件不存在: {args.config}", file=sys.stderr)
            sys.exit(1)
        config = load_config(args.config)
        print(f"使用配置文件: {args.config}")
    elif args.preset:
        # 导入预设(简化版,实际应从 model_selection_wizard.py 导入)
        presets = {
            "default": {},
            "balanced": {
                "step_7": {"primary": "deepseek-chat"},
                "step_11": {"primary": "deepseek-chat"},
                "step_13": {"primary": "deepseek-reasoner"}
            },
            "high-quality": {
                "step_1": {"primary": "agy-gemini"},
                "step_4": {"primary": "codex-gpt55"},
                "step_5": {"primary": "codex-gpt55"},
                "step_7": {"primary": "gemini-api"},
                "step_11": {"primary": "deepseek-chat"},
                "step_13": {"primary": "deepseek-reasoner"}
            },
            "cost-optimized": {
                "step_13": {"primary": "deepseek-chat"}
            }
        }

        if args.preset not in presets:
            print(f"错误: 未知的预设: {args.preset}", file=sys.stderr)
            sys.exit(1)

        config = {"_default": presets[args.preset]}
        print(f"使用预设方案: {args.preset}")
    else:
        print("错误: 必须指定 --config 或 --preset", file=sys.stderr)
        parser.print_help()
        sys.exit(1)

    results = estimate_project_cost(config, args.default_model)
    print_estimate(results)

    print("\n说明:")
    print("  - 估算基于 CUMCM 2024B 的历史数据,实际消耗可能因问题复杂度而变化")
    print("  - 不包括重试、reopen 循环的额外成本")
    print("  - 订阅制模型(Claude/Codex/Antigravity)的成本取决于订阅方式")
    print("  - 详细定价信息请查阅各 API 提供商官网")

if __name__ == "__main__":
    main()
