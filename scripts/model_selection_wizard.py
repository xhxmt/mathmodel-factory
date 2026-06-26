#!/usr/bin/env python3
"""
交互式模型选择向导

在创建新项目时引导用户为关键步骤选择合适的模型,并生成 model_config.json。
支持预设方案(默认/平衡/高质量)和自定义配置。

使用:
  ./scripts/model_selection_wizard.py <project_path>
  ./scripts/model_selection_wizard.py <project_path> --preset balanced
  ./scripts/model_selection_wizard.py <project_path> --preset high-quality --non-interactive
"""

import sys
import json
import argparse
from pathlib import Path
from typing import Dict, List

# 预设方案
PRESETS = {
    "default": {
        "name": "默认方案(全 Claude)",
        "description": "所有步骤使用 Claude CLI 默认模型,零配置开箱即用",
        "config": {}  # 空配置表示使用全局默认
    },
    "balanced": {
        "name": "平衡方案(推荐)",
        "description": "关键评审步骤使用 DeepSeek API,其他使用 Claude",
        "config": {
            "step_7": {"primary": "deepseek-chat"},
            "step_11": {"primary": "deepseek-chat"},
            "step_13": {"primary": "deepseek-reasoner", "fallback": "deepseek-chat"}
        },
        "requirements": ["DEEPSEEK_API_KEY"]
    },
    "high-quality": {
        "name": "高质量方案",
        "description": "混合使用多个强力模型,追求最佳质量",
        "config": {
            "step_1": {"primary": "agy-gemini"},
            "step_4": {"primary": "codex-gpt55"},
            "step_5": {"primary": "codex-gpt55"},
            "step_7": {"primary": "gemini-api", "fallback": "deepseek-chat"},
            "step_11": {"primary": "deepseek-chat"},
            "step_13": {"primary": "deepseek-reasoner", "fallback": "gemini-api"}
        },
        "requirements": ["GEMINI_API_KEY", "DEEPSEEK_API_KEY"]
    },
    "cost-optimized": {
        "name": "成本优化方案",
        "description": "仅评委打分使用外部 API,降低成本",
        "config": {
            "step_13": {"primary": "deepseek-chat"}
        },
        "requirements": ["DEEPSEEK_API_KEY"]
    }
}

# 关键步骤说明
KEY_STEPS = {
    "step_1": {"name": "背景研究", "default": "codex-gpt55", "alternatives": ["agy-gemini", "claude"]},
    "step_4": {"name": "模型构建", "default": "codex-gpt55", "alternatives": ["claude", "agy-gemini"]},
    "step_5": {"name": "求解编排", "default": "codex-gpt55", "alternatives": ["claude"]},
    "step_7": {"name": "模型评价", "default": "deepseek-chat", "alternatives": ["gemini-api", "claude"]},
    "step_11": {"name": "建设性审稿", "default": "deepseek-chat", "alternatives": ["claude", "codex-gpt55"]},
    "step_13": {"name": "评委打分", "default": "deepseek-reasoner", "alternatives": ["deepseek-chat", "gemini-api", "claude"]}
}

def load_model_registry(factory_root: Path) -> Dict:
    """加载模型注册表"""
    registry_path = factory_root / "web" / "model_registry.json"
    if not registry_path.exists():
        print(f"警告: 模型注册表不存在 {registry_path}", file=sys.stderr)
        return {"models": []}

    with open(registry_path, 'r', encoding='utf-8') as f:
        return json.load(f)

def check_requirements(requirements: List[str], factory_root: Path) -> bool:
    """检查必需的环境变量"""
    env_file = factory_root / ".env"
    if not env_file.exists():
        return False

    env_content = env_file.read_text(encoding='utf-8')
    missing = []
    for req in requirements:
        if f"{req}=" not in env_content:
            missing.append(req)

    if missing:
        print(f"\n⚠️  缺少必需的环境变量: {', '.join(missing)}")
        print(f"   请在 {env_file} 中配置:")
        for var in missing:
            print(f"     {var}=your-api-key-here")
        return False

    return True

def print_presets():
    """打印所有预设方案"""
    print("\n可用的预设方案:")
    print("=" * 60)
    for key, preset in PRESETS.items():
        print(f"\n[{key}] {preset['name']}")
        print(f"  {preset['description']}")
        if preset.get('requirements'):
            print(f"  需要: {', '.join(preset['requirements'])}")
        if preset['config']:
            print(f"  配置步骤: {', '.join(preset['config'].keys())}")
        else:
            print(f"  配置步骤: (使用全局默认)")

def interactive_setup(factory_root: Path) -> Dict:
    """交互式模型选择"""
    print("\n" + "=" * 60)
    print("模型选择向导")
    print("=" * 60)

    print_presets()

    while True:
        choice = input("\n选择预设方案(default/balanced/high-quality/cost-optimized) [balanced]: ").strip().lower() or "balanced"
        if choice in PRESETS:
            break
        print(f"无效的选择: {choice}")

    preset = PRESETS[choice]
    print(f"\n已选择: {preset['name']}")

    # 检查环境要求
    if preset.get('requirements'):
        if not check_requirements(preset['requirements'], factory_root):
            print("\n继续使用此方案需要先配置环境变量。")
            cont = input("是否继续(配置稍后手动添加)? (y/N): ").strip().lower()
            if cont != 'y':
                print("已取消")
                sys.exit(0)

    # 询问是否自定义关键步骤
    customize = input("\n是否自定义关键步骤模型? (y/N): ").strip().lower()
    if customize == 'y':
        config = dict(preset['config'])
        registry = load_model_registry(factory_root)
        available_models = [m['id'] for m in registry.get('models', []) if m.get('enabled')]

        print("\n可用模型:")
        for i, model_id in enumerate(available_models, 1):
            model_info = next((m for m in registry['models'] if m['id'] == model_id), )
            print(f"  {i}. {model_id} — {model_info.get('label', 'Unknown')}")

        for step_key, step_info in KEY_STEPS.items():
            current = config.get(step_key, {}).get('primary', 'claude')
            print(f"\n{step_key} ({step_info['name']}) [当前: {current}]")
            new_model = input(f"  选择模型 (留空保持 {current}): ").strip()
            if new_model and new_model in available_models:
                config[step_key] = {"primary": new_model}

        return {"_default": config}
    else:
        return {"_default": preset['config']} if preset['config'] else {}

def main():
    parser = argparse.ArgumentParser(description="模型选择向导")
    parser.add_argument("project_path", type=Path, help="项目目录路径")
    parser.add_argument("--preset", choices=list(PRESETS.keys()), help="直接使用预设方案")
    parser.add_argument("--non-interactive", action="store_true", help="非交互模式(必须指定 --preset)")
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
        # 假设项目在 ongoing/<name>/ 下
        factory_root = project_path.parent.parent

    if not (factory_root / "run_paper.sh").exists():
        print(f"错误: 无法找到 factory 根目录(run_paper.sh 不存在于 {factory_root})", file=sys.stderr)
        sys.exit(1)

    # 交互或非交互模式
    if args.non_interactive:
        if not args.preset:
            print("错误: 非交互模式必须指定 --preset", file=sys.stderr)
            sys.exit(1)

        preset = PRESETS[args.preset]
        print(f"使用预设: {preset['name']}")

        if preset.get('requirements'):
            if not check_requirements(preset['requirements'], factory_root):
                print("错误: 缺少必需的环境变量", file=sys.stderr)
                sys.exit(1)

        config = {"_default": preset['config']} if preset['config'] else {}
    elif args.preset:
        preset = PRESETS[args.preset]
        print(f"使用预设: {preset['name']}")

        if preset.get('requirements'):
            check_requirements(preset['requirements'], factory_root)

        config = {"_default": preset['config']} if preset['config'] else {}
    else:
        config = interactive_setup(factory_root)

    # 保存配置
    config_path = project_path / "model_config.json"

    if not config or not config.get('_default'):
        print("\n未配置任何步骤,将使用全局默认模型")
        if config_path.exists():
            print(f"删除现有配置文件: {config_path}")
            config_path.unlink()
        sys.exit(0)

    with open(config_path, 'w', encoding='utf-8') as f:
        json.dump(config, f, indent=2, ensure_ascii=False)

    print(f"\n✅ 模型配置已保存: {config_path}")
    print(f"\n配置的步骤:")
    for step, model_info in config['_default'].items():
        primary = model_info.get('primary', 'claude')
        fallback = model_info.get('fallback', '')
        fallback_str = f" (fallback: {fallback})" if fallback else ""
        print(f"  {step}: {primary}{fallback_str}")

    print(f"\n其他步骤将使用全局默认模型(通常为 claude)")
    print(f"\n查看完整配置指南: docs/guides/model_selection_guide.md")

if __name__ == "__main__":
    main()
