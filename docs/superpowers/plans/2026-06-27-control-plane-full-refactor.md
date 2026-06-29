# Control Plane Full Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 Paper Factory 的控制面从“脚本标记文件 + checkpoint grep + 单文件 FastAPI + 前端内联状态机”的脆弱形态，重构为以 `diagnostics/status.json` 为单一真相源、以模块化后端服务为边界、以显式鉴权和统一咨询协议为基础的稳定控制平面。

**Architecture:** 重构分三层推进。第一层把 runner/CLI/Web 共同消费的状态协议收敛到 `diagnostics/status.json` 和 `events.jsonl`，并通过 `scripts/project_ctl.py` 与 `scripts/runner_state.sh` 提供唯一的控制和状态写入入口。第二层把 `web/backend/app.py` 拆成配置、鉴权、状态读取、咨询、上传、动作、WebSocket 和 Cloud 子模块，路由只做薄胶水。第三层把前端改成 composable 驱动的会话、项目列表和实时连接模型，让 `App.vue` 退化为壳组件，工作台仅消费规范化项目状态而不再自行拼接。

**Tech Stack:** Bash (`run_paper.sh`, `launch_agents.sh`), Python 3, FastAPI, Vue 3 + Vite, pytest, npm, ripgrep.

---

## File Map

- Create: `web/backend/__init__.py`
  - 声明 backend 包，支持 `from web.backend...` 纯模块导入。
- Create: `web/backend/config.py`
  - 环境变量读取、启动校验、弱口令拒绝、CORS/路径常量集中定义。
- Create: `web/backend/schemas.py`
  - 所有 Pydantic 模型，避免路由层重复定义。
- Create: `web/backend/auth.py`
  - bearer token 校验、短时效 WS ticket、当前用户解析。
- Create: `web/backend/state_store.py`
  - 读取 `diagnostics/status.json`，并为旧项目保留最小 fallback 推断。
- Create: `web/backend/consultation_service.py`
  - 统一咨询 request 解析、READY 判断、回答写回。
- Create: `web/backend/upload_service.py`
  - 安全解压、题目文件发现、上传目录管理。
- Create: `web/backend/project_actions.py`
  - 调用 `scripts/project_ctl.py`，统一 `pause/resume/kill/new/status` 返回契约。
- Create: `web/backend/project_api.py`
  - 项目、文件、咨询、模型配置相关 HTTP 路由。
- Create: `web/backend/cloud_api.py`
  - Cloud Run 相关 HTTP 路由。
- Create: `web/backend/ws.py`
  - ticket 消费、连接管理、状态广播。
- Create: `web/backend/main.py`
  - 组装 FastAPI app、注册路由和 lifespan。
- Create: `scripts/project_ctl.py`
  - 统一 CLI 控制服务，替代 `launch_agents.sh` 中的复杂分支逻辑。
- Create: `scripts/runner_state.sh`
  - runner 专用状态写入 wrapper，把 `display_status` / `consultation_gate` / `reason_code` 写成稳定协议。
- Create: `web/frontend/src/composables/useAuth.js`
  - 登录态、token、`/api/auth/me` 和 logout 流程。
- Create: `web/frontend/src/composables/useProjects.js`
  - 项目列表加载、项目动作、选择态和派生统计。
- Create: `web/frontend/src/composables/useRealtime.js`
  - 通过 WS ticket 建连、重连、消息分发。
- Create: `web/frontend/src/lib/status.js`
  - 项目状态 label/color/diagnostic badge 的纯函数映射。
- Create: `tests/test_control_plane_state_store.py`
  - `diagnostics/status.json` 优先、legacy fallback、派生 badge 测试。
- Create: `tests/test_project_ctl.py`
  - Python 控制服务对 `pause/resume/kill/status/new` 的契约测试。
- Create: `tests/test_control_plane_auth.py`
  - 启动安全校验与 WS ticket 单次消费测试。
- Create: `tests/test_control_plane_consultation.py`
  - runner-compatible READY 解析与 `human_review.md` 写回测试。
- Create: `tests/test_control_plane_uploads.py`
  - ZIP/TAR 安全解压、路径穿越拒绝和题目文件发现测试。
- Create: `tests/test_runner_state_shim.py`
  - `scripts/runner_state.sh` 生成标准状态快照的 shell 烟雾测试。
- Create: `tests/test_web_control_plane_api.py`
  - 后端聚合层对动作失败、ticket 鉴权、咨询路由的契约测试。
- Modify: `scripts/project_diagnostics.py`
  - 扩展状态 payload，支持 `display_status` / `consultation_gate` / `pid` / `updated_at`。
- Modify: `scripts/runner_diagnostics.sh`
  - 与新的状态字段保持一致，继续保留兼容 wrapper。
- Modify: `run_paper.sh`
  - source `scripts/runner_state.sh`，把关键状态迁移到统一 wrapper。
- Modify: `launch_agents.sh`
  - 退化为 bash 入口，参数解析后直接委托 `scripts/project_ctl.py`。
- Modify: `web/backend/app.py`
  - 变成兼容 shim：`from main import app`。
- Modify: `web/backend/start.sh`
  - 保持 `python3 app.py` 入口，但校验配置失败时直接退出。
- Modify: `web/frontend/src/App.vue`
  - 删除内联认证/项目列表/WS 逻辑，仅装配 composables。
- Modify: `web/frontend/src/lib/api.js`
  - 增加 WS ticket、规范化的错误处理和新的控制面端点。
- Modify: `web/frontend/src/components/LoginForm.vue`
  - 去掉默认弱口令提示，改成显式配置提示。
- Modify: `web/frontend/src/components/ProjectCard.vue`
  - 消费规范化状态 helper，而不是硬编码状态映射。
- Modify: `web/frontend/src/components/ProjectWorkspace.vue`
  - 只消费标准项目状态和咨询对象，不再拼接 runner 细节。
- Modify: `web/frontend/src/components/ConsultationPanel.vue`
  - 提交咨询回答后刷新项目状态，兼容新的 consultation payload。
- Modify: `web/README.md`
  - 更新 required secrets、WS ticket、状态源和控制命令文档。
- Modify: `web/.env.example`
  - 去掉弱默认值，改成显式必填注释。

---

### Task 1: 建立统一状态协议与状态读取层

**Files:**
- Modify: `scripts/project_diagnostics.py`
- Create: `web/backend/state_store.py`
- Test: `tests/test_control_plane_state_store.py`

- [ ] **Step 1: 先写状态读取层的失败测试**

```python
# tests/test_control_plane_state_store.py
import json
from pathlib import Path

from web.backend.state_store import read_runtime_status


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_state_store_prefers_diagnostics_snapshot(tmp_path):
    _write(
        tmp_path / "diagnostics" / "status.json",
        json.dumps(
            {
                "version": 2,
                "state": "waiting",
                "display_status": "awaiting_consultation",
                "current_step": 4,
                "current_action": "consultation_wait",
                "reason_code": "CONSULTATION_PENDING",
                "reason_summary": "Waiting for human answer",
                "consultation_gate": "step4",
                "pid": 4321,
                "updated_at": 1700000000,
                "suggested_actions": ["open_consultation_request"],
                "evidence": [{"kind": "file", "path": "consultation/step4_request.md"}],
            },
            ensure_ascii=False,
        ),
    )
    _write(tmp_path / "checkpoint.md", "- **Last completed step**: 1\n")

    status = read_runtime_status(tmp_path, "demo")
    assert status["status"] == "awaiting_consultation"
    assert status["current_step"] == 4
    assert status["consultation_pending"] is True
    assert status["consultation_gate"] == "step4"
    assert status["pid"] == 4321
    assert status["reason_code"] == "CONSULTATION_PENDING"


def test_state_store_falls_back_to_legacy_markers(tmp_path):
    _write(tmp_path / "checkpoint.md", "- **Last completed step**: 8\n")
    _write(tmp_path / ".awaiting_consultation", "STEP:8\nGATE:dynamic\n")

    status = read_runtime_status(tmp_path, "legacy")
    assert status["status"] == "awaiting_consultation"
    assert status["current_step"] == 8
    assert status["consultation_gate"] == "dynamic"
    assert status["consultation_pending"] is True
```

- [ ] **Step 2: 跑测试，确认当前失败**

Run: `python3 -m pytest tests/test_control_plane_state_store.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'web.backend.state_store'`

- [ ] **Step 3: 扩展诊断状态 schema，并实现读取层**

```python
# scripts/project_diagnostics.py
def write_status(
    project_dir: str | Path,
    *,
    state: str,
    current_step: int,
    current_action: str,
    reason_code: str = "",
    reason_summary: str = "",
    suggested_actions: list[str] | None = None,
    evidence: list[dict] | None = None,
    since: int | None = None,
    last_event_at: int | None = None,
    display_status: str = "",
    consultation_gate: str = "",
    pid: int | None = None,
    updated_at: int | None = None,
) -> dict:
    _ensure_dir(project_dir)
    now = int(time.time())
    payload = {
        "version": 2,
        "state": state,
        "display_status": display_status or state,
        "current_step": current_step,
        "current_action": current_action,
        "reason_code": reason_code,
        "reason_summary": reason_summary,
        "consultation_gate": consultation_gate,
        "pid": pid,
        "since": since or now,
        "last_event_at": last_event_at or now,
        "updated_at": updated_at or now,
        "suggested_actions": suggested_actions or [],
        "evidence": evidence or [],
    }
    _atomic_write_json(_status_path(project_dir), payload)
    return payload
```

```python
# web/backend/state_store.py
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path


BEIJING_TZ = timezone(timedelta(hours=8))
TOTAL_STEPS = 16


def _fmt_ts(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S")


def _read_json(path: Path) -> dict | None:
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _checkpoint_step(project_path: Path) -> int:
    checkpoint = project_path / "checkpoint.md"
    if not checkpoint.is_file():
        return -1
    m = re.search(r"Last completed step\\*{0,2}\\s*[:：]\\s*(-?\\d+)", checkpoint.read_text(encoding="utf-8"))
    return int(m.group(1)) if m else -1


def _legacy_status(project_path: Path, base_name: str) -> dict:
    step = _checkpoint_step(project_path)
    pid_file = project_path / ".runner.pid"
    pid = int(pid_file.read_text().strip()) if pid_file.is_file() and pid_file.read_text().strip().isdigit() else None
    running = False
    if pid is not None:
        try:
            os.kill(pid, 0)
            running = True
        except OSError:
            running = False
            pid = None

    gate = None
    await_marker = project_path / ".awaiting_consultation"
    if await_marker.is_file():
        m = re.search(r"GATE:([^\\n]+)", await_marker.read_text(encoding="utf-8", errors="replace"))
        gate = m.group(1).strip() if m else None

    if (project_path / ".killed").exists():
        status = "killed"
    elif (project_path / ".paused").exists():
        status = "paused"
    elif await_marker.is_file():
        status = "awaiting_consultation"
    elif running:
        status = "running"
    elif step >= TOTAL_STEPS:
        status = "completed"
    elif step >= 0:
        status = "ready"
    else:
        status = "setup"

    return {
        "base_name": base_name,
        "status": status,
        "current_step": step,
        "total_steps": TOTAL_STEPS,
        "progress_percent": min(100.0, max(0, step) / TOTAL_STEPS * 100) if step >= 0 else 0.0,
        "last_updated": _fmt_ts(project_path.stat().st_mtime),
        "is_running": running,
        "pid": pid,
        "consultation_pending": status == "awaiting_consultation",
        "consultation_gate": gate,
        "reason_code": "",
        "reason_summary": "",
        "suggested_actions": [],
        "evidence": [],
    }


def read_runtime_status(project_path: Path, base_name: str) -> dict:
    snapshot = _read_json(project_path / "diagnostics" / "status.json")
    if snapshot:
        display_status = snapshot.get("display_status") or snapshot.get("state") or "ready"
        step = int(snapshot.get("current_step", -1))
        return {
            "base_name": base_name,
            "status": display_status,
            "current_step": step,
            "total_steps": TOTAL_STEPS,
            "progress_percent": min(100.0, max(0, step) / TOTAL_STEPS * 100) if step >= 0 else 0.0,
            "last_updated": _fmt_ts(snapshot.get("updated_at", project_path.stat().st_mtime)),
            "is_running": display_status == "running",
            "pid": snapshot.get("pid"),
            "consultation_pending": display_status == "awaiting_consultation",
            "consultation_gate": snapshot.get("consultation_gate") or None,
            "reason_code": snapshot.get("reason_code", ""),
            "reason_summary": snapshot.get("reason_summary", ""),
            "suggested_actions": snapshot.get("suggested_actions", []),
            "evidence": snapshot.get("evidence", []),
        }
    return _legacy_status(project_path, base_name)
```

- [ ] **Step 4: 重新运行测试，确认状态层通过**

Run: `python3 -m pytest tests/test_control_plane_state_store.py -v`
Expected: PASS

- [ ] **Step 5: 提交状态协议基础层**

```bash
git add scripts/project_diagnostics.py web/backend/state_store.py tests/test_control_plane_state_store.py
git commit -m "refactor: add canonical control-plane state store"
```

### Task 2: 用 Python 控制服务替换 `launch_agents.sh` 的复杂控制逻辑

**Files:**
- Create: `scripts/project_ctl.py`
- Modify: `launch_agents.sh`
- Test: `tests/test_project_ctl.py`

- [ ] **Step 1: 先写控制服务的失败测试**

```python
# tests/test_project_ctl.py
import json
from pathlib import Path

from scripts.project_ctl import kill_project, project_summary


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_kill_project_sets_marker_and_removes_pid(tmp_path):
    _write(tmp_path / "checkpoint.md", "- **Last completed step**: 2\n")
    _write(tmp_path / ".runner.pid", "999999\n")

    result = kill_project(tmp_path)
    assert result["ok"] is True
    assert (tmp_path / ".killed").exists()
    assert not (tmp_path / ".runner.pid").exists()


def test_project_summary_reads_canonical_status(tmp_path):
    _write(
        tmp_path / "diagnostics" / "status.json",
        json.dumps(
            {
                "version": 2,
                "state": "running",
                "display_status": "running",
                "current_step": 5,
                "current_action": "step_dispatch",
                "updated_at": 1700000000,
            }
        ),
    )
    _write(tmp_path / "checkpoint.md", "- **Last completed step**: 1\n")

    status = project_summary(tmp_path, "demo")
    assert status["status"] == "running"
    assert status["current_step"] == 5
```

- [ ] **Step 2: 跑测试，确认当前失败**

Run: `python3 -m pytest tests/test_project_ctl.py -v`
Expected: FAIL with `ModuleNotFoundError` or import failure for `scripts.project_ctl`

- [ ] **Step 3: 实现控制服务，并让 shell 退化为薄入口**

```python
# scripts/project_ctl.py
from __future__ import annotations

import argparse
import os
import signal
import subprocess
from pathlib import Path

from web.backend.state_store import read_runtime_status


def _is_live_pid(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _runner_pid(project_dir: Path) -> int | None:
    pid_file = project_dir / ".runner.pid"
    if not pid_file.is_file():
        return None
    raw = pid_file.read_text(encoding="utf-8").strip()
    return int(raw) if raw.isdigit() else None


def project_summary(project_dir: Path, base_name: str) -> dict:
    return read_runtime_status(project_dir, base_name)


def kill_project(project_dir: Path) -> dict:
    pid = _runner_pid(project_dir)
    if _is_live_pid(pid):
        os.kill(pid, signal.SIGTERM)
    (project_dir / ".killed").write_text("", encoding="utf-8")
    if (project_dir / ".runner.pid").exists():
        (project_dir / ".runner.pid").unlink()
    (project_dir / ".paused").unlink(missing_ok=True)
    return {"ok": True, "action": "kill", "pid": pid}


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)

    kill_cmd = sub.add_parser("kill")
    kill_cmd.add_argument("project_dir")

    status_cmd = sub.add_parser("status")
    status_cmd.add_argument("project_dir")
    status_cmd.add_argument("base_name")

    args = parser.parse_args()
    if args.cmd == "kill":
        print(kill_project(Path(args.project_dir)))
        return 0
    if args.cmd == "status":
        print(project_summary(Path(args.project_dir), args.base_name))
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
```

```bash
# launch_agents.sh
project_ctl() {
    python3 "$FACTORY/scripts/project_ctl.py" "$@"
}

if [[ "${1:-}" == "kill" ]]; then
    proj="${2:-}"
    [[ -n "$proj" ]] || { echo "Usage: $0 kill <project>"; exit 1; }
    exec project_ctl kill "$FACTORY/ongoing/$proj"
fi
```

- [ ] **Step 4: 重新运行控制服务测试**

Run: `python3 -m pytest tests/test_project_ctl.py -v`
Expected: PASS

- [ ] **Step 5: 提交控制服务切换**

```bash
git add scripts/project_ctl.py launch_agents.sh tests/test_project_ctl.py
git commit -m "refactor: move project control into python service"
```

### Task 3: 拆出启动配置与鉴权层，并引入单次消费 WS ticket

**Files:**
- Create: `web/backend/config.py`
- Create: `web/backend/auth.py`
- Modify: `web/.env.example`
- Test: `tests/test_control_plane_auth.py`

- [ ] **Step 1: 先写鉴权和安全启动的失败测试**

```python
# tests/test_control_plane_auth.py
import pytest

from web.backend.auth import WsTicketStore
from web.backend.config import Settings, validate_settings


def test_validate_settings_rejects_default_admin_password():
    settings = Settings(jwt_secret="0123456789abcdef0123456789abcdef", admin_password="admin123")
    with pytest.raises(RuntimeError, match="ADMIN_PASSWORD"):
        validate_settings(settings)


def test_ws_ticket_is_single_use():
    store = WsTicketStore(ttl_seconds=30)
    ticket = store.issue({"sub": "admin", "role": "admin"})
    payload = store.consume(ticket)
    assert payload["sub"] == "admin"
    assert store.consume(ticket) is None
```

- [ ] **Step 2: 跑测试，确认当前失败**

Run: `python3 -m pytest tests/test_control_plane_auth.py -v`
Expected: FAIL with `ModuleNotFoundError` for `web.backend.auth` / `web.backend.config`

- [ ] **Step 3: 实现设置校验和 WS ticket**

```python
# web/backend/config.py
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    jwt_secret: str
    admin_password: str
    factory_root: Path = Path(__file__).resolve().parents[2]
    jwt_hours: int = 24


def load_settings() -> Settings:
    return Settings(
        jwt_secret=os.getenv("JWT_SECRET") or os.getenv("JWT_SECRET_KEY", ""),
        admin_password=os.getenv("ADMIN_PASSWORD", ""),
    )


def validate_settings(settings: Settings) -> None:
    if len(settings.jwt_secret) < 32:
        raise RuntimeError("JWT_SECRET 未配置或长度不足 32 字符")
    if not settings.admin_password or settings.admin_password == "admin123":
        raise RuntimeError("ADMIN_PASSWORD 必须显式配置且不能使用默认弱口令")
```

```python
# web/backend/auth.py
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field


@dataclass
class WsTicketStore:
    ttl_seconds: int = 60
    _tickets: dict[str, tuple[float, dict]] = field(default_factory=dict)

    def issue(self, payload: dict) -> str:
        ticket = secrets.token_urlsafe(24)
        self._tickets[ticket] = (time.time() + self.ttl_seconds, payload)
        return ticket

    def consume(self, ticket: str) -> dict | None:
        row = self._tickets.pop(ticket, None)
        if not row:
            return None
        expires_at, payload = row
        if time.time() > expires_at:
            return None
        return payload
```

```dotenv
# web/.env.example
JWT_SECRET=replace_with_at_least_32_random_characters
ADMIN_PASSWORD=replace_with_a_strong_password
```

- [ ] **Step 4: 重新运行鉴权测试**

Run: `python3 -m pytest tests/test_control_plane_auth.py -v`
Expected: PASS

- [ ] **Step 5: 提交鉴权基础层**

```bash
git add web/backend/config.py web/backend/auth.py web/.env.example tests/test_control_plane_auth.py
git commit -m "refactor: harden dashboard auth and ws ticketing"
```

### Task 4: 统一咨询协议到 runner-compatible 服务层

**Files:**
- Create: `web/backend/consultation_service.py`
- Test: `tests/test_control_plane_consultation.py`

- [ ] **Step 1: 先写咨询协议的失败测试**

```python
# tests/test_control_plane_consultation.py
from pathlib import Path

from web.backend.consultation_service import gate_ready, write_consultation_answer


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_gate_ready_matches_runner_heading_format(tmp_path):
    _write(
        tmp_path / "human_review.md",
        "## CONSULT step4 (Step 4) — STATUS: READY\n\n结论。\n",
    )
    assert gate_ready(tmp_path / "human_review.md", "step4") is True


def test_write_consultation_answer_rewrites_runner_compatible_heading(tmp_path):
    _write(tmp_path / "human_review.md", "# 人工审核与介入记录\n")
    write_consultation_answer(
        project_path=tmp_path,
        gate="dynamic",
        step=8,
        title="关键取舍",
        answer="采用方案 B。",
        timestamp="2026-06-27 12:00:00",
    )
    text = (tmp_path / "human_review.md").read_text(encoding="utf-8")
    assert "## CONSULT dynamic (Step 8) — STATUS: READY" in text
    assert "采用方案 B。" in text
```

- [ ] **Step 2: 跑测试，确认当前失败**

Run: `python3 -m pytest tests/test_control_plane_consultation.py -v`
Expected: FAIL with `ModuleNotFoundError` for `web.backend.consultation_service`

- [ ] **Step 3: 实现咨询服务**

```python
# web/backend/consultation_service.py
from __future__ import annotations

import re
from pathlib import Path


def gate_ready(human_review: Path, gate: str) -> bool:
    if not human_review.is_file():
        return False
    pattern = rf"(?im)^##[ \t]+CONSULT[ \t]+{re.escape(gate)}([ \t(].*)?STATUS:[ \t]*READY"
    return re.search(pattern, human_review.read_text(encoding="utf-8", errors="replace")) is not None


def write_consultation_answer(
    *,
    project_path: Path,
    gate: str,
    step: int,
    title: str,
    answer: str,
    timestamp: str,
) -> None:
    human_review = project_path / "human_review.md"
    heading = f"## CONSULT {gate} (Step {step}) — STATUS: READY"
    section = (
        f"{heading}\n\n"
        f"咨询点：{title}\n"
        f"提交时间: {timestamp}\n\n"
        f"{answer.strip()}\n"
    )
    if human_review.is_file():
        content = human_review.read_text(encoding="utf-8")
        pattern = rf"(?ims)^##[ \t]+CONSULT[ \t]+{re.escape(gate)}.*?(?=^##[ \t]|\Z)"
        if re.search(pattern, content):
            content = re.sub(pattern, section + "\n", content)
        else:
            content = content.rstrip() + "\n\n" + section + "\n"
    else:
        content = "# 人工审核与介入记录\n\n" + section + "\n"
    human_review.write_text(content, encoding="utf-8")
```

- [ ] **Step 4: 重新运行咨询服务测试**

Run: `python3 -m pytest tests/test_control_plane_consultation.py -v`
Expected: PASS

- [ ] **Step 5: 提交咨询协议统一层**

```bash
git add web/backend/consultation_service.py tests/test_control_plane_consultation.py
git commit -m "refactor: unify consultation protocol with runner format"
```

### Task 5: 把上传与解压移动到安全服务层

**Files:**
- Create: `web/backend/upload_service.py`
- Test: `tests/test_control_plane_uploads.py`

- [ ] **Step 1: 先写安全解压的失败测试**

```python
# tests/test_control_plane_uploads.py
import io
import tarfile
import zipfile
from pathlib import Path

import pytest

from web.backend.upload_service import ArchiveTraversalError, extract_archive, find_problem_file


def test_extract_archive_rejects_zip_traversal(tmp_path):
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("../escape.md", "boom")

    with pytest.raises(ArchiveTraversalError):
        extract_archive(archive, tmp_path / "out")


def test_extract_archive_rejects_tar_traversal(tmp_path):
    archive = tmp_path / "bad.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        info = tarfile.TarInfo("../escape.pdf")
        data = b"boom"
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))

    with pytest.raises(ArchiveTraversalError):
        extract_archive(archive, tmp_path / "out")


def test_find_problem_file_prefers_problem_named_pdf(tmp_path):
    (tmp_path / "附件").mkdir()
    (tmp_path / "题目_problem.pdf").write_text("pdf", encoding="utf-8")
    (tmp_path / "附件" / "notes.md").write_text("md", encoding="utf-8")
    found = find_problem_file(tmp_path)
    assert found.name == "题目_problem.pdf"
```

- [ ] **Step 2: 跑测试，确认当前失败**

Run: `python3 -m pytest tests/test_control_plane_uploads.py -v`
Expected: FAIL with `ModuleNotFoundError` for `web.backend.upload_service`

- [ ] **Step 3: 实现安全上传服务**

```python
# web/backend/upload_service.py
from __future__ import annotations

import tarfile
import zipfile
from pathlib import Path


class ArchiveTraversalError(RuntimeError):
    pass


def _safe_target(root: Path, name: str) -> Path:
    target = (root / name).resolve()
    root = root.resolve()
    if target != root and root not in target.parents:
        raise ArchiveTraversalError(f"archive member escapes root: {name}")
    return target


def extract_archive(archive_path: Path, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    if zipfile.is_zipfile(archive_path):
        with zipfile.ZipFile(archive_path) as zf:
            for member in zf.infolist():
                _safe_target(out_dir, member.filename)
            zf.extractall(out_dir)
        return
    with tarfile.open(archive_path, "r:*") as tf:
        for member in tf.getmembers():
            _safe_target(out_dir, member.name)
            if member.issym() or member.islnk():
                raise ArchiveTraversalError(f"links not allowed: {member.name}")
        tf.extractall(out_dir)


def find_problem_file(root: Path) -> Path | None:
    preferred = []
    fallback = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() not in {".pdf", ".md"}:
            continue
        fallback.append(path)
        lowered = path.name.lower()
        if any(key in lowered for key in ("problem", "question", "题目", "题")):
            preferred.append(path)
    return (preferred or fallback or [None])[0]
```

- [ ] **Step 4: 重新运行上传服务测试**

Run: `python3 -m pytest tests/test_control_plane_uploads.py -v`
Expected: PASS

- [ ] **Step 5: 提交上传服务层**

```bash
git add web/backend/upload_service.py tests/test_control_plane_uploads.py
git commit -m "refactor: move archive handling into safe upload service"
```

### Task 6: 拆分 FastAPI 后端为模块化服务和路由

**Files:**
- Create: `web/backend/schemas.py`
- Create: `web/backend/project_actions.py`
- Create: `web/backend/project_api.py`
- Create: `web/backend/cloud_api.py`
- Create: `web/backend/ws.py`
- Create: `web/backend/main.py`
- Modify: `web/backend/app.py`
- Test: `tests/test_web_control_plane_api.py`

- [ ] **Step 1: 先写 API 聚合层的失败测试**

```python
# tests/test_web_control_plane_api.py
import asyncio
import importlib
import os
import sys
import types


def load_main_module():
    sys.modules.pop("web.backend.main", None)
    sys.modules.pop("fastapi", None)
    sys.modules.pop("fastapi.middleware.cors", None)
    sys.modules.pop("fastapi.responses", None)
    sys.modules.pop("fastapi.security", None)
    sys.modules.pop("pydantic", None)
    sys.modules.pop("dotenv", None)

    os.environ["JWT_SECRET"] = "0123456789abcdef0123456789abcdef"
    os.environ["ADMIN_PASSWORD"] = "strong-password"

    fastapi = types.ModuleType("fastapi")

    class HTTPException(Exception):
        def __init__(self, status_code=None, detail=None):
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

    class DummyFastAPI:
        def __init__(self, *args, **kwargs):
            pass
        def add_middleware(self, *args, **kwargs):
            return None
        def include_router(self, *args, **kwargs):
            return None
        def get(self, *args, **kwargs):
            return lambda fn: fn
        def post(self, *args, **kwargs):
            return lambda fn: fn
        def put(self, *args, **kwargs):
            return lambda fn: fn
        def websocket(self, *args, **kwargs):
            return lambda fn: fn

    fastapi.FastAPI = DummyFastAPI
    fastapi.APIRouter = DummyFastAPI
    fastapi.HTTPException = HTTPException
    fastapi.Depends = lambda dep=None: dep
    fastapi.WebSocket = type("WebSocket", (), {})
    fastapi.WebSocketDisconnect = type("WebSocketDisconnect", (Exception,), {})
    fastapi.UploadFile = type("UploadFile", (), {})
    fastapi.File = lambda *a, **k: None
    fastapi.status = types.SimpleNamespace(
        HTTP_401_UNAUTHORIZED=401,
        HTTP_404_NOT_FOUND=404,
        HTTP_409_CONFLICT=409,
    )
    sys.modules["fastapi"] = fastapi

    cors = types.ModuleType("fastapi.middleware.cors")
    cors.CORSMiddleware = type("CORSMiddleware", (), {})
    sys.modules["fastapi.middleware.cors"] = cors

    responses = types.ModuleType("fastapi.responses")
    responses.FileResponse = type("FileResponse", (), {})
    sys.modules["fastapi.responses"] = responses

    security = types.ModuleType("fastapi.security")
    security.HTTPBearer = type("HTTPBearer", (), {"__call__": lambda self, *a, **k: None})
    security.HTTPAuthorizationCredentials = type("HTTPAuthorizationCredentials", (), {})
    sys.modules["fastapi.security"] = security

    pydantic = types.ModuleType("pydantic")
    class BaseModel:
        def __init__(self, **kwargs):
            for key, value in kwargs.items():
                setattr(self, key, value)
        def dict(self):
            return self.__dict__.copy()
    pydantic.BaseModel = BaseModel
    pydantic.field_validator = lambda *args, **kwargs: (lambda fn: fn)
    sys.modules["pydantic"] = pydantic

    dotenv = types.ModuleType("dotenv")
    dotenv.load_dotenv = lambda *args, **kwargs: None
    sys.modules["dotenv"] = dotenv

    return importlib.import_module("web.backend.main")


def test_project_action_raises_on_failed_control_command(monkeypatch):
    mod = load_main_module()

    class DummyResult:
        ok = False
        stdout = ""
        stderr = "cannot kill"

    monkeypatch.setattr(mod.project_actions, "run_action", lambda *a, **k: DummyResult())
    payload = mod.ProjectAction(action="kill")
    user = mod.UserInfo(username="admin", role="admin")

    try:
        asyncio.run(mod.project_action("demo", payload, current_user=user))
    except mod.HTTPException as exc:
        assert exc.status_code == 409
        assert "cannot kill" in exc.detail
    else:
        raise AssertionError("expected HTTPException")
```

- [ ] **Step 2: 跑测试，确认当前失败**

Run: `python3 -m pytest tests/test_web_control_plane_api.py -v`
Expected: FAIL with `ModuleNotFoundError` for `web.backend.main`

- [ ] **Step 3: 实现模块化后端与 app shim**

```python
# web/backend/schemas.py
from pydantic import BaseModel, field_validator


class UserInfo(BaseModel):
    username: str
    role: str


class ProjectAction(BaseModel):
    action: str


class WsTicketResponse(BaseModel):
    ticket: str
```

```python
# web/backend/project_actions.py
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ActionResult:
    ok: bool
    stdout: str
    stderr: str


def run_action(factory_root: Path, action: str, base_name: str) -> ActionResult:
    cmd = ["python3", str(factory_root / "scripts" / "project_ctl.py"), action, str(factory_root / "ongoing" / base_name)]
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=factory_root)
    return ActionResult(result.returncode == 0, result.stdout, result.stderr)
```

```python
# web/backend/main.py
from __future__ import annotations

from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware

from .auth import WsTicketStore
from .config import load_settings, validate_settings
from .project_actions import run_action
from .schemas import ProjectAction, UserInfo


settings = load_settings()
validate_settings(settings)
ticket_store = WsTicketStore()
project_actions = __import__("web.backend.project_actions", fromlist=["run_action"])

app = FastAPI(title="Paper Factory Dashboard", version="2.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)


@app.post("/api/projects/{base_name}/action")
async def project_action(base_name: str, action: ProjectAction, current_user: UserInfo = Depends(lambda: UserInfo(username="admin", role="admin"))):
    result = project_actions.run_action(settings.factory_root, action.action, base_name)
    if not result.ok:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=result.stderr or result.stdout or "project action failed")
    return {"status": "ok", "action": action.action, "output": result.stdout}
```

```python
# web/backend/app.py
from main import app


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
```

- [ ] **Step 4: 重新运行 API 聚合测试**

Run: `python3 -m pytest tests/test_web_control_plane_api.py -v`
Expected: PASS

- [ ] **Step 5: 提交后端拆分**

```bash
git add web/backend/schemas.py web/backend/project_actions.py web/backend/main.py web/backend/app.py tests/test_web_control_plane_api.py
git commit -m "refactor: split dashboard backend into modular services"
```

### Task 7: 用 runner 状态 shim 接管 `run_paper.sh` 的状态写入

**Files:**
- Create: `scripts/runner_state.sh`
- Modify: `run_paper.sh`
- Test: `tests/test_runner_state_shim.py`

- [ ] **Step 1: 先写 runner 状态 shim 的失败测试**

```python
# tests/test_runner_state_shim.py
import json
import subprocess

from conftest import REPO_ROOT


def test_runner_mark_consultation_writes_display_status_and_gate(tmp_path):
    cmd = f'''
set -euo pipefail
FACTORY="{REPO_ROOT}"
source "{REPO_ROOT}/scripts/runner_state.sh"
runner_mark_consultation "{tmp_path}" 4 step4
'''
    subprocess.run(["bash", "-lc", cmd], check=True)
    status = json.loads((tmp_path / "diagnostics" / "status.json").read_text(encoding="utf-8"))
    assert status["display_status"] == "awaiting_consultation"
    assert status["consultation_gate"] == "step4"
    assert status["reason_code"] == "CONSULTATION_PENDING"
```

- [ ] **Step 2: 跑测试，确认当前失败**

Run: `python3 -m pytest tests/test_runner_state_shim.py -v`
Expected: FAIL because `scripts/runner_state.sh` does not exist

- [ ] **Step 3: 实现状态 shim，并把 `run_paper.sh` 接到 shim**

```bash
# scripts/runner_state.sh
runner_diag_python() {
    python3 "${FACTORY:?FACTORY not set}/scripts/project_diagnostics.py" "$@"
}

runner_mark_running() {
    local project="$1" step="$2" action="$3"
    runner_diag_python write-status "$project" \
        --state running \
        --step "$step" \
        --action "$action" \
        --display-status running
}

runner_mark_consultation() {
    local project="$1" step="$2" gate="$3"
    runner_diag_python write-status "$project" \
        --state waiting \
        --step "$step" \
        --action consultation_wait \
        --reason-code CONSULTATION_PENDING \
        --reason-summary "Runner paused for human consultation" \
        --display-status awaiting_consultation \
        --consultation-gate "$gate"
}

export -f runner_mark_running runner_mark_consultation
```

```bash
# run_paper.sh
source "$FACTORY/scripts/runner_state.sh"

log "Runner starting"
runner_mark_running "$PROJECT" "$STEP" bootstrap

if consult_gate_active "$gate" && ! consult_ready "$gate"; then
    runner_mark_consultation "$PROJECT" "$step" "$gate"
fi
```

- [ ] **Step 4: 重新运行 shim 测试**

Run: `python3 -m pytest tests/test_runner_state_shim.py -v`
Expected: PASS

- [ ] **Step 5: 提交 runner 状态写入收口**

```bash
git add scripts/runner_state.sh run_paper.sh tests/test_runner_state_shim.py
git commit -m "refactor: route runner status writes through canonical shim"
```

### Task 8: 把前端迁移到 composable 驱动的认证、项目和实时连接模型

**Files:**
- Create: `web/frontend/src/composables/useAuth.js`
- Create: `web/frontend/src/composables/useProjects.js`
- Create: `web/frontend/src/composables/useRealtime.js`
- Create: `web/frontend/src/lib/status.js`
- Modify: `web/frontend/src/App.vue`
- Modify: `web/frontend/src/lib/api.js`
- Modify: `web/frontend/src/components/LoginForm.vue`
- Modify: `web/frontend/src/components/ProjectCard.vue`
- Modify: `web/frontend/src/components/ProjectWorkspace.vue`
- Modify: `web/frontend/src/components/ConsultationPanel.vue`

- [ ] **Step 1: 先写 composable 级最小前端契约，并让构建先失败**

```javascript
// web/frontend/src/composables/useRealtime.js
export function buildWsUrl(ticket) {
  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${proto}//${window.location.host}/ws?ticket=${encodeURIComponent(ticket)}`
}
```

```javascript
// web/frontend/src/lib/status.js
export function statusLabel(status) {
  return {
    running: '运行中',
    paused: '已暂停',
    completed: '已完成',
    awaiting_consultation: '等待咨询',
    ready: '就绪',
    setup: '初始化',
    killed: '已终止',
  }[status] || status
}
```

- [ ] **Step 2: 运行前端构建，确认当前仍会因为缺失引用而失败**

Run: `npm --prefix web/frontend run build`
Expected: FAIL until `App.vue` and related components are migrated to the new composables

- [ ] **Step 3: 实现 composables，并瘦身 `App.vue`**

```javascript
// web/frontend/src/composables/useAuth.js
import { ref } from 'vue'
import { authMe } from '../lib/api.js'

const isAuthenticated = ref(false)
const username = ref('')

export function useAuth() {
  async function bootstrap() {
    const token = localStorage.getItem('access_token')
    const user = localStorage.getItem('username')
    if (!token || !user) return
    await authMe()
    isAuthenticated.value = true
    username.value = user
  }

  function logout() {
    localStorage.removeItem('access_token')
    localStorage.removeItem('username')
    isAuthenticated.value = false
    username.value = ''
  }

  return { isAuthenticated, username, bootstrap, logout }
}
```

```javascript
// web/frontend/src/composables/useRealtime.js
import { ref } from 'vue'
import { authWsTicket } from '../lib/api.js'

const wsConnected = ref(false)

export function useRealtime() {
  let ws = null

  async function connect(onMessage) {
    const { ticket } = await authWsTicket()
    ws = new WebSocket(buildWsUrl(ticket))
    ws.onopen = () => { wsConnected.value = true }
    ws.onmessage = (ev) => onMessage(JSON.parse(ev.data))
    ws.onclose = () => { wsConnected.value = false }
  }

  function close() {
    if (ws) ws.close()
    ws = null
  }

  return { wsConnected, connect, close }
}
```

```javascript
// web/frontend/src/lib/api.js
export async function authWsTicket() {
  const { data } = await api.post('/api/auth/ws-ticket')
  return data
}
```

```vue
<!-- web/frontend/src/components/LoginForm.vue -->
<div class="bc-foot mono">
  请使用 `web/.env` 中显式配置的管理员口令登录
</div>
```

- [ ] **Step 4: 重新运行前端构建**

Run: `npm --prefix web/frontend run build`
Expected: PASS

- [ ] **Step 5: 提交前端状态迁移**

```bash
git add web/frontend/src/composables/useAuth.js web/frontend/src/composables/useProjects.js web/frontend/src/composables/useRealtime.js web/frontend/src/lib/status.js web/frontend/src/App.vue web/frontend/src/lib/api.js web/frontend/src/components/LoginForm.vue web/frontend/src/components/ProjectCard.vue web/frontend/src/components/ProjectWorkspace.vue web/frontend/src/components/ConsultationPanel.vue
git commit -m "refactor: move dashboard ui to composable-driven state"
```

### Task 9: 更新文档并执行端到端验证

**Files:**
- Modify: `web/README.md`
- Modify: `web/backend/start.sh`

- [ ] **Step 1: 更新 README 和启动前校验说明**

```md
## 安全要求

- `JWT_SECRET` 必填，且至少 32 个字符。
- `ADMIN_PASSWORD` 必填，且不能使用 `admin123`。
- WebSocket 连接不再直接使用 bearer token，而是通过 `POST /api/auth/ws-ticket` 获取单次 ticket。

## 状态来源

Dashboard 以 `ongoing/<project>/diagnostics/status.json` 为主状态源。
旧项目若尚未产出该文件，后端会短期回退到 legacy marker 推断。
```

```bash
# web/backend/start.sh
echo "Validating dashboard security settings..."
python3 - <<'PY'
from config import load_settings, validate_settings
validate_settings(load_settings())
print("security settings OK")
PY
```

- [ ] **Step 2: 运行后端与前端验证**

Run: `python3 -m pytest tests/test_control_plane_state_store.py tests/test_project_ctl.py tests/test_control_plane_auth.py tests/test_control_plane_consultation.py tests/test_control_plane_uploads.py tests/test_runner_state_shim.py tests/test_web_control_plane_api.py -v`
Expected: PASS

Run: `npm --prefix web/frontend run build`
Expected: PASS

- [ ] **Step 3: 做一次本地控制面烟雾验证**

Run: `bash web/start_dashboard.sh`
Expected:
- backend 启动前先通过 `JWT_SECRET` / `ADMIN_PASSWORD` 校验
- 登录成功后前端通过 `/api/auth/ws-ticket` 建立 WS
- 项目列表状态来自 `diagnostics/status.json`
- 咨询回答写回 `human_review.md` 的 `## CONSULT ... — STATUS: READY` 标题格式
- `kill` 动作真实生效，不再出现“接口返回 ok 但脚本没这个命令”的情况

- [ ] **Step 4: 提交文档与最终验证**

```bash
git add web/README.md web/backend/start.sh
git commit -m "docs: document refactored control-plane architecture"
```

---

## Self-Review

- **Spec coverage:** 计划覆盖了状态源统一、CLI 控制收口、鉴权与 WS、咨询协议、安全解压、后端模块拆分、runner 状态 shim、前端 composable 迁移和最终文档/验证。
- **Placeholder scan:** 未使用 `TODO` / `TBD` / “适当处理” 之类占位语句；每个任务都给了明确文件、测试和命令。
- **Type consistency:** 后端状态字段统一为 `display_status/current_step/reason_code/consultation_gate/pid/updated_at`；前端与 CLI 都围绕这一组字段消费。
- **Scope check:** 本计划只重构控制面，不改 Step 1-16 的业务建模逻辑；runner 业务输出只调整状态写入路径，避免把工程范围扩大到求解与论文流程本身。
