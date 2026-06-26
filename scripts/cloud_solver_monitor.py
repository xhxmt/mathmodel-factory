#!/usr/bin/env python3
"""
Cloud Solver 监控和健康检查工具

监控 Cloud Run Solver 的健康状态,并在检测到问题时自动回退到本地执行。

功能:
1. 定期健康检查(HTTP /health 端点)
2. 检测连续失败并触发回退
3. 记录健康状态历史
4. 生成监控报告

使用:
  ./scripts/cloud_solver_monitor.py --check         # 单次健康检查
  ./scripts/cloud_solver_monitor.py --watch         # 持续监控(30s 间隔)
  ./scripts/cloud_solver_monitor.py --status        # 显示状态和历史
  ./scripts/cloud_solver_monitor.py --reset-fallback  # 重置回退状态
"""

import sys
import json
import time
import argparse
import requests
import subprocess
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, Optional

FACTORY_ROOT = Path(__file__).parent.parent
HEALTH_CHECK_FILE = FACTORY_ROOT / "run_state" / "cloud_solver_health.json"
FALLBACK_MARKER = FACTORY_ROOT / "run_state" / "cloud_solver_fallback.marker"

# 健康检查配置
HEALTH_CHECK_TIMEOUT = 10  # 秒
CONSECUTIVE_FAILURES_THRESHOLD = 3  # 连续失败次数触发回退
FALLBACK_COOLDOWN = 3600  # 回退后恢复检查的冷却时间(秒)

def get_cloud_solver_url() -> Optional[str]:
    """从环境变量或 .env 获取 Cloud Run URL"""
    env_file = FACTORY_ROOT / ".env"
    if not env_file.exists():
        return None

    with open(env_file, 'r') as f:
        for line in f:
            if line.startswith('CLOUD_RUN_URL='):
                url = line.split('=', 1)[1].strip().strip('"').strip("'")
                return url

    return None

def load_health_history() -> Dict:
    """加载健康检查历史"""
    if not HEALTH_CHECK_FILE.exists():
        return {
            "version": "1.0",
            "checks": [],
            "consecutive_failures": 0,
            "last_success": None,
            "fallback_active": False,
            "fallback_since": None
        }

    try:
        with open(HEALTH_CHECK_FILE, 'r') as f:
            return json.load(f)
    except Exception as e:
        print(f"⚠️  无法加载健康历史: {e}", file=sys.stderr)
        return {
            "version": "1.0",
            "checks": [],
            "consecutive_failures": 0,
            "last_success": None,
            "fallback_active": False,
            "fallback_since": None
        }

def save_health_history(history: Dict):
    """保存健康检查历史"""
    HEALTH_CHECK_FILE.parent.mkdir(parents=True, exist_ok=True)

    # 只保留最近 100 次检查记录
    if len(history.get("checks", [])) > 100:
        history["checks"] = history["checks"][-100:]

    with open(HEALTH_CHECK_FILE, 'w') as f:
        json.dump(history, f, indent=2)

def check_health(url: str) -> Dict:
    """执行健康检查"""
    result = {
        "timestamp": datetime.now().isoformat(),
        "healthy": False,
        "response_time_ms": None,
        "status_code": None,
        "error": None
    }

    try:
        start = time.time()
        response = requests.get(f"{url}/health", timeout=HEALTH_CHECK_TIMEOUT)
        response_time = (time.time() - start) * 1000

        result["response_time_ms"] = round(response_time, 2)
        result["status_code"] = response.status_code

        if response.status_code == 200:
            data = response.json()
            result["healthy"] = data.get("status") == "healthy"
            if not result["healthy"]:
                result["error"] = f"Service reports unhealthy: {data}"
        else:
            result["error"] = f"HTTP {response.status_code}"

    except requests.Timeout:
        result["error"] = f"Timeout after {HEALTH_CHECK_TIMEOUT}s"
    except requests.ConnectionError:
        result["error"] = "Connection failed"
    except Exception as e:
        result["error"] = str(e)

    return result

def update_fallback_state(history: Dict):
    """根据健康检查历史更新回退状态"""
    consecutive_failures = history["consecutive_failures"]

    # 检查是否需要激活回退
    if not history["fallback_active"] and consecutive_failures >= CONSECUTIVE_FAILURES_THRESHOLD:
        history["fallback_active"] = True
        history["fallback_since"] = datetime.now().isoformat()
        FALLBACK_MARKER.touch()
        print(f"\n⚠️  连续 {consecutive_failures} 次健康检查失败,激活本地回退模式", file=sys.stderr)
        print(f"   Cloud Solver 调用将自动回退到本地执行", file=sys.stderr)
        return

    # 检查是否可以解除回退(连续成功 + 冷却时间已过)
    if history["fallback_active"] and consecutive_failures == 0:
        fallback_since = datetime.fromisoformat(history["fallback_since"])
        cooldown_remaining = (fallback_since + timedelta(seconds=FALLBACK_COOLDOWN)) - datetime.now()

        if cooldown_remaining.total_seconds() <= 0:
            history["fallback_active"] = False
            history["fallback_since"] = None
            if FALLBACK_MARKER.exists():
                FALLBACK_MARKER.unlink()
            print(f"\n✅ Cloud Solver 恢复健康,解除本地回退模式", file=sys.stderr)
        else:
            remaining_min = int(cooldown_remaining.total_seconds() / 60)
            print(f"\n⏳ Cloud Solver 健康,但仍在冷却期(剩余 {remaining_min} 分钟)", file=sys.stderr)

def print_status(history: Dict, url: Optional[str]):
    """打印监控状态"""
    print("\n" + "=" * 60)
    print("Cloud Solver 监控状态")
    print("=" * 60)

    if not url:
        print("\n❌ Cloud Solver 未配置")
        print("   在 .env 中设置 CLOUD_RUN_URL")
        return

    print(f"\nCloud Run URL: {url}")

    if history["fallback_active"]:
        fallback_since = datetime.fromisoformat(history["fallback_since"])
        duration = datetime.now() - fallback_since
        print(f"\n⚠️  回退模式: 已激活(持续 {duration.seconds // 60} 分钟)")
        print(f"   原因: 连续 {CONSECUTIVE_FAILURES_THRESHOLD} 次健康检查失败")
        print(f"   所有 Cloud Solver 调用将回退到本地执行")
    else:
        print(f"\n✅ 回退模式: 未激活")

    print(f"\n连续失败次数: {history['consecutive_failures']}")

    if history["last_success"]:
        last_success = datetime.fromisoformat(history["last_success"])
        ago = datetime.now() - last_success
        print(f"上次成功: {last_success.strftime('%Y-%m-%d %H:%M:%S')} ({ago.seconds // 60} 分钟前)")
    else:
        print(f"上次成功: 从未成功")

    # 显示最近 10 次检查
    recent_checks = history.get("checks", [])[-10:]
    if recent_checks:
        print(f"\n最近 {len(recent_checks)} 次检查:")
        print("-" * 60)
        for check in reversed(recent_checks):
            timestamp = datetime.fromisoformat(check["timestamp"]).strftime("%H:%M:%S")
            status = "✅" if check["healthy"] else "❌"
            response_time = f"{check['response_time_ms']}ms" if check['response_time_ms'] else "N/A"
            error = f" ({check['error']})" if check.get('error') else ""
            print(f"{timestamp} {status} {response_time}{error}")

def watch_health(url: str, interval: int = 30):
    """持续监控健康状态"""
    print(f"开始监控 Cloud Solver 健康状态(间隔 {interval}s)")
    print(f"URL: {url}")
    print("按 Ctrl+C 停止监控\n")

    try:
        while True:
            history = load_health_history()
            result = check_health(url)

            # 更新历史
            history["checks"].append(result)

            if result["healthy"]:
                history["consecutive_failures"] = 0
                history["last_success"] = result["timestamp"]
                status_icon = "✅"
            else:
                history["consecutive_failures"] += 1
                status_icon = "❌"

            # 更新回退状态
            update_fallback_state(history)
            save_health_history(history)

            # 打印结果
            timestamp = datetime.fromisoformat(result["timestamp"]).strftime("%H:%M:%S")
            response_time = f"{result['response_time_ms']}ms" if result['response_time_ms'] else "N/A"
            error = f" - {result['error']}" if result.get('error') else ""

            print(f"{timestamp} {status_icon} {response_time}{error}")

            time.sleep(interval)

    except KeyboardInterrupt:
        print("\n\n监控已停止")

def main():
    parser = argparse.ArgumentParser(description="Cloud Solver 监控工具")
    parser.add_argument("--check", action="store_true", help="单次健康检查")
    parser.add_argument("--watch", action="store_true", help="持续监控")
    parser.add_argument("--status", action="store_true", help="显示状态和历史")
    parser.add_argument("--reset-fallback", action="store_true", help="重置回退状态")
    parser.add_argument("--interval", type=int, default=30, help="监控间隔(秒)")
    args = parser.parse_args()

    url = get_cloud_solver_url()

    if args.reset_fallback:
        history = load_health_history()
        history["fallback_active"] = False
        history["fallback_since"] = None
        history["consecutive_failures"] = 0
        save_health_history(history)
        if FALLBACK_MARKER.exists():
            FALLBACK_MARKER.unlink()
        print("✅ 回退状态已重置")
        sys.exit(0)

    if args.status:
        history = load_health_history()
        print_status(history, url)
        sys.exit(0)

    if not url:
        print("错误: Cloud Solver 未配置", file=sys.stderr)
        print("在 .env 中设置 CLOUD_RUN_URL", file=sys.stderr)
        sys.exit(1)

    if args.check:
        print(f"检查 Cloud Solver 健康状态: {url}")
        result = check_health(url)

        # 更新历史
        history = load_health_history()
        history["checks"].append(result)

        if result["healthy"]:
            history["consecutive_failures"] = 0
            history["last_success"] = result["timestamp"]
            print(f"\n✅ 健康检查通过")
            print(f"   响应时间: {result['response_time_ms']}ms")
        else:
            history["consecutive_failures"] += 1
            print(f"\n❌ 健康检查失败")
            print(f"   错误: {result['error']}")
            print(f"   连续失败: {history['consecutive_failures']} 次")

        update_fallback_state(history)
        save_health_history(history)

        sys.exit(0 if result["healthy"] else 1)

    if args.watch:
        watch_health(url, args.interval)
        sys.exit(0)

    parser.print_help()
    sys.exit(1)

if __name__ == "__main__":
    main()
