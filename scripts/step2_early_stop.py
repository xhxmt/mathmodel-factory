#!/usr/bin/env python3
"""Step 2 早停检测器 — 检测demo solve是否快速失败，决定是否提前终止该流。

Usage:
    python3 step2_early_stop.py <project_dir> <stream_id>

检查 demo solve 的早期迹象：
  - 前5分钟内是否有明确的不收敛信号（NaN/Inf/unbounded）
  - 是否在极短时间内报告infeasible
  - Python/Julia/MATLAB脚本是否在前N秒crash

返回 JSON:
    {
        "should_stop": true/false,
        "reason": "...",
        "elapsed_seconds": N,
        "confidence": 0.0-1.0
    }

Exit code: 0=继续运行, 1=建议终止, 2=无法判断
"""

import os
import re
import sys
import json
import time
from pathlib import Path


_EARLY_FAIL_PATTERNS = [
    (r'nan', 'NAN_EARLY', 1.0),
    (r'inf(?!easible)', 'INF_EARLY', 1.0),
    (r'unbounded', 'UNBOUNDED', 0.9),
    (r'infeasible.*detected\s+at\s+root', 'INFEAS_AT_ROOT', 0.95),
    (r'error.*matrix.*singular', 'SINGULAR_MATRIX', 0.9),
    (r'error.*no\s+feasible\s+solution', 'NO_FEASIBLE', 0.95),
    (r'traceback.*most recent call', 'PYTHON_CRASH', 0.7),  # 需要后续确认
    (r'error.*at\s+line\s+\d+', 'RUNTIME_ERROR', 0.7),
]

_QUICK_INFEASIBLE_THRESHOLD = 30  # 30秒内报infeasible算早停信号


def _read_file(path):
    """读取文件，容错编码。"""
    if not os.path.exists(path):
        return "", 0

    mtime = os.path.getmtime(path)
    for enc in ('utf-8', 'latin-1', 'cp1252'):
        try:
            with open(path, 'r', encoding=enc, errors='replace') as f:
                return f.read(), mtime
        except (IOError, UnicodeDecodeError):
            continue
    return "", mtime


def find_demo_artifacts(project_dir, stream_id):
    """找到demo相关的日志和脚本。

    返回: {
        'demo_script': path,
        'demo_log': path,
        'demo_json': path,
        'start_time': timestamp or None
    }
    """
    artifacts = {
        'demo_script': None,
        'demo_log': None,
        'demo_json': None,
        'start_time': None,
    }

    # 典型位置：models/m{N}_*/demo.{py,jl,m}
    models_dir = os.path.join(project_dir, 'models')
    if not os.path.isdir(models_dir):
        return artifacts

    # 找到对应的stream目录
    for subdir in os.listdir(models_dir):
        if not subdir.startswith(stream_id.lower() + '_'):
            continue
        stream_dir = os.path.join(models_dir, subdir)
        if not os.path.isdir(stream_dir):
            continue

        # 脚本
        for ext in ('py', 'jl', 'm', 'R'):
            script = os.path.join(stream_dir, f'demo.{ext}')
            if os.path.isfile(script):
                artifacts['demo_script'] = script
                artifacts['start_time'] = os.path.getmtime(script)
                break

        # 日志
        log_path = os.path.join(stream_dir, 'demo.log')
        if os.path.isfile(log_path):
            artifacts['demo_log'] = log_path
        else:
            # 检查 logs/demo.log
            logs_subdir = os.path.join(stream_dir, 'logs')
            if os.path.isdir(logs_subdir):
                log_alt = os.path.join(logs_subdir, 'demo.log')
                if os.path.isfile(log_alt):
                    artifacts['demo_log'] = log_alt

        # JSON结果
        json_path = os.path.join(project_dir, f'{stream_id.lower()}_demo_result.json')
        if os.path.isfile(json_path):
            artifacts['demo_json'] = json_path

        break

    return artifacts


def analyze_early_failure(artifacts, time_limit_seconds=300):
    """分析demo运行是否应该早停。

    time_limit_seconds: 只检查前N秒的日志（默认5分钟）

    返回: {
        'should_stop': bool,
        'reason': str,
        'elapsed_seconds': float,
        'confidence': float,
    }
    """
    result = {
        'should_stop': False,
        'reason': '',
        'elapsed_seconds': 0,
        'confidence': 0.0,
    }

    log_path = artifacts['demo_log']
    if not log_path or not os.path.exists(log_path):
        result['reason'] = 'no_log_yet'
        result['confidence'] = 0.0
        return result

    start_time = artifacts.get('start_time')
    if not start_time:
        start_time = os.path.getmtime(log_path)

    current_time = time.time()
    elapsed = current_time - start_time
    result['elapsed_seconds'] = round(elapsed, 1)

    # 如果已经超过时间窗口，放弃早停检测
    if elapsed > time_limit_seconds:
        result['reason'] = 'past_early_window'
        result['confidence'] = 0.0
        return result

    # 读取日志
    log_text, log_mtime = _read_file(log_path)
    if not log_text:
        result['reason'] = 'log_empty'
        result['confidence'] = 0.0
        return result

    log_text_lower = log_text.lower()

    # 检查早停模式
    max_confidence = 0.0
    best_reason = None

    for pattern, label, confidence in _EARLY_FAIL_PATTERNS:
        if re.search(pattern, log_text_lower):
            if confidence > max_confidence:
                max_confidence = confidence
                best_reason = label

    # 快速infeasible检测
    if elapsed < _QUICK_INFEASIBLE_THRESHOLD and 'infeasible' in log_text_lower:
        if max_confidence < 0.95:
            max_confidence = 0.95
            best_reason = 'QUICK_INFEASIBLE'

    # 脚本crash检测（Python traceback）
    if 'traceback' in log_text_lower and 'most recent call last' in log_text_lower:
        # 检查是否是严重错误（非警告）
        if any(kw in log_text_lower for kw in ['importerror', 'syntaxerror', 'nameerror', 'typeerror']):
            if max_confidence < 0.85:
                max_confidence = 0.85
                best_reason = 'PYTHON_IMPORT_ERROR'

    # 判断
    if max_confidence >= 0.85:
        result['should_stop'] = True
        result['reason'] = best_reason
        result['confidence'] = max_confidence
    elif max_confidence > 0.5:
        result['should_stop'] = False
        result['reason'] = f'suspicious_{best_reason}'
        result['confidence'] = max_confidence
    else:
        result['reason'] = 'no_early_signal'
        result['confidence'] = 0.0

    return result


def main():
    if len(sys.argv) < 3:
        print(json.dumps({
            'error': 'Usage: step2_early_stop.py <project_dir> <stream_id>'
        }), file=sys.stderr)
        sys.exit(2)

    project_dir = sys.argv[1]
    stream_id = sys.argv[2]

    if not os.path.isdir(project_dir):
        print(json.dumps({
            'error': f'{project_dir} not found'
        }), file=sys.stderr)
        sys.exit(2)

    artifacts = find_demo_artifacts(project_dir, stream_id)
    result = analyze_early_failure(artifacts)

    print(json.dumps(result, ensure_ascii=False, indent=2))

    # Exit code: 0=继续, 1=建议终止, 2=无法判断
    if result['should_stop']:
        sys.exit(1)
    elif result['confidence'] > 0:
        sys.exit(0)
    else:
        sys.exit(2)


if __name__ == '__main__':
    main()
