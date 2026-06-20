#!/usr/bin/env python3
"""
Create demo projects for testing the dashboard
用于测试的模拟项目生成器
"""
import os
from pathlib import Path
from datetime import datetime

FACTORY_ROOT = Path(__file__).parent.parent.resolve()
ONGOING_DIR = FACTORY_ROOT / "ongoing"
COMPLETE_DIR = FACTORY_ROOT / "complete"

def create_demo_project(base_name, status, step, consultation=False):
    """Create a demo project directory with mock files"""

    # Determine directory
    if status == "completed":
        project_dir = COMPLETE_DIR / base_name
    else:
        project_dir = ONGOING_DIR / base_name

    project_dir.mkdir(parents=True, exist_ok=True)

    # Create checkpoint.md
    checkpoint = project_dir / "checkpoint.md"
    checkpoint.write_text(f"""# Project Checkpoint

Project: {base_name}
Status: {status}
Last completed step: {step}
Created: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

## Progress
- Setup complete
- Step {step} finished
- Total progress: {(step + 1) / 16 * 100:.1f}%
""")

    # Create logs directory with sample log
    logs_dir = project_dir / "logs"
    logs_dir.mkdir(exist_ok=True)

    log_file = logs_dir / f"step_{step}_demo.log"
    log_file.write_text(f"""[INFO] Starting step {step}
[INFO] Loading data...
[INFO] Processing...
[INFO] Step {step} completed successfully
""")

    # Create consultation request if needed
    if consultation:
        consult_dir = project_dir / "consultation"
        consult_dir.mkdir(exist_ok=True)

        req_file = consult_dir / "preflight_request.md"
        req_file.write_text(f"""# 咨询请求：启动前 seed

- gate: preflight
- step: {step}
- project: {base_name}
- created: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

## 需要你（借助 GPT Pro / Gemini Deep Think）决定的事

经过初步解析，这是一道关于【主题】的建模问题。候选方法包括：
1. 方法 A - 优化模型
2. 方法 B - 统计模型
3. 方法 C - 机器学习

请你使用 GPT Pro / Gemini Deep Think 分析：
- 哪个方法最适合？
- 需要考虑哪些约束？
- 建议的建模路线？

## 回填方式
1. 把结论写进 human_review.md
2. 标记 STATUS: READY
3. 项目会自动继续
""")

    # Create problem directory for modeling mode
    problem_dir = project_dir / "problem"
    problem_dir.mkdir(exist_ok=True)

    problem_brief = problem_dir / "problem_brief.md"
    problem_brief.write_text(f"""# Problem Brief

Project: {base_name}
Type: Mathematical Modeling Competition

## Background
This is a demo problem for testing the dashboard.

## Requirements
- Build a mathematical model
- Solve the problem
- Write a paper
""")

    print(f"✓ Created demo project: {base_name} (step {step}, {status})")

def main():
    print("Creating demo projects for dashboard testing...\n")

    # Create various demo projects
    create_demo_project("demo_running_step3", "running", 3, consultation=False)
    create_demo_project("demo_consultation_step4", "awaiting_consultation", 4, consultation=True)
    create_demo_project("demo_paused_step7", "paused", 7, consultation=False)
    create_demo_project("demo_completed", "completed", 16, consultation=False)
    create_demo_project("demo_early_stage", "running", 1, consultation=False)

    # Add pause marker
    paused_marker = ONGOING_DIR / "demo_paused_step7" / ".paused"
    paused_marker.touch()

    print("\n" + "=" * 50)
    print("Demo projects created successfully!")
    print("=" * 50)
    print("\nYou can now start the dashboard:")
    print("  cd web && ./start_dashboard.sh")
    print("\nTo clean up demo projects:")
    print("  rm -rf ongoing/demo_* complete/demo_*")
    print()

if __name__ == "__main__":
    main()
