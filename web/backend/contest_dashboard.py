from __future__ import annotations

import json
import statistics
import time
from pathlib import Path
from typing import Any

from factory_core.contest import phase_for_step
from factory_core.delivery.release import resolve_current_release
from factory_core.storage import SQLiteStateStore


def _json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _check(
    check_id: str,
    label: str,
    status: str,
    detail: str,
    *,
    path: str = "",
    blocking: bool = True,
) -> dict[str, Any]:
    return {
        "id": check_id,
        "label": label,
        "status": status,
        "detail": detail,
        "path": path,
        "blocking": blocking,
    }


def _project_file_exists(project: Path, relative: str) -> bool:
    try:
        candidate = (project / relative).resolve()
        candidate.relative_to(project)
    except (OSError, ValueError):
        return False
    return candidate.is_file()


def _audit(project: Path, profile: str) -> dict[str, Any]:
    latest = (
        project / ".factory" / "audits" / "latest.json"
        if profile == "final"
        else project / ".factory" / "audits" / "profiles" / profile / "latest.json"
    )
    value = _json(latest)
    return {
        "profile": profile,
        "available": bool(value),
        "status": str(value.get("status") or "PENDING"),
        "decision": str(value.get("decision") or ""),
        "snapshot_id": str(value.get("snapshot_id") or ""),
        "created_at": str(value.get("created_at") or ""),
        "delivery_allowed": bool(value.get("delivery_allowed")),
        "path": str(latest.relative_to(project)) if latest.is_file() else "",
    }


def _step_durations(store: SQLiteStateStore) -> list[dict[str, int]]:
    starts: dict[int, int] = {}
    durations: list[dict[str, int]] = []
    for event in store.events():
        if event.step is None:
            continue
        step = int(event.step)
        if event.type == "STEP_STARTED":
            starts[step] = int(event.created_at)
        elif event.type == "STEP_SUCCEEDED" and step in starts:
            duration = max(0, int(event.created_at) - starts[step])
            durations.append(
                {"step": step, "duration_seconds": duration, "finished_at": int(event.created_at)}
            )
    return durations


def _timing(
    store: SQLiteStateStore,
    *,
    current_step: int,
    now_epoch: int,
) -> dict[str, Any]:
    policy = store.contest_policy()
    if policy is None:
        return {
            "configured": False,
            "risk_level": "unconfigured",
            "mode": "legacy",
            "mode_label": "未配置比赛时钟",
            "recommendation": "按 Legacy 项目处理，不推断剩余比赛时间。",
        }
    durations = _step_durations(store)
    recent = [item["duration_seconds"] for item in durations[-3:] if item["duration_seconds"] > 0]
    average = int(statistics.mean(recent)) if recent else 3_600
    confidence = "observed" if recent else "default"
    phase = phase_for_step(min(max(current_step, 0), 16))
    phase_events = [
        event.created_at
        for event in store.events()
        if event.type == "STEP_STARTED" and event.step in phase.steps
    ]
    phase_started = min(phase_events) if phase_events else now_epoch
    remaining_content_steps = max(0, 16 - max(current_step, 0))
    projected_content_finish = now_epoch + remaining_content_steps * average
    content_freeze = int(policy["content_freeze_at"])
    delivery_freeze = int(policy["delivery_freeze_at"])
    deadline = int(policy["contest_deadline_at"])
    content_slack = content_freeze - projected_content_finish
    if now_epoch >= deadline:
        risk, mode, label = "expired", "closed", "比赛截止"
        recommendation = "停止所有发布操作，核对官方提交状态。"
    elif now_epoch >= delivery_freeze:
        risk, mode, label = "critical", "delivery_freeze", "交付冻结"
        recommendation = "只允许最终交付；回退上游必须人工授权。"
    elif now_epoch >= content_freeze:
        risk, mode, label = "critical", "audit_only", "最终审计期"
        recommendation = "停止内容修改，只运行 Final Audit、编译和打包。"
    elif content_slack < 0:
        risk, mode, label = "critical", "repair_only", "预计超时"
        recommendation = "立即停止探索新模型，压缩到阻塞修复与论文收敛。"
    elif content_slack < 2 * 3_600:
        risk, mode, label = "warning", "repair_only", "安全余量不足"
        recommendation = "停止探索新模型，只处理高优先级修复。"
    elif phase.id >= 5 or content_slack < 6 * 3_600:
        risk, mode, label = "guarded", "converge", "收敛模式"
        recommendation = "冻结建模主线，集中完成验证、论文和审计。"
    else:
        risk, mode, label = "safe", "explore", "可继续探索"
        recommendation = "时间余量允许在当前主线内继续有限探索。"
    return {
        "configured": True,
        "started_at": int(policy["contest_started_at"]),
        "deadline_at": deadline,
        "content_freeze_at": content_freeze,
        "delivery_freeze_at": delivery_freeze,
        "elapsed_seconds": max(0, now_epoch - int(policy["contest_started_at"])),
        "remaining_seconds": max(0, deadline - now_epoch),
        "phase_id": phase.id,
        "phase_name": phase.name,
        "phase_started_at": phase_started,
        "phase_elapsed_seconds": max(0, now_epoch - phase_started),
        "recent_step_average_seconds": average,
        "forecast_confidence": confidence,
        "projected_content_finish_at": projected_content_finish,
        "content_slack_seconds": content_slack,
        "risk_level": risk,
        "mode": mode,
        "mode_label": label,
        "recommendation": recommendation,
        "recent_steps": durations[-3:],
    }


def _canonical_evidence(project: Path) -> dict[str, Any]:
    path = project / "results" / "canonical_results.json"
    value = _json(path)
    items: list[dict[str, Any]] = []
    ignored = {"schema_version", "project", "primary_method", "auxiliary_method", "generated_at", "random_seed"}
    for key, payload in value.items():
        if key in ignored or not isinstance(payload, dict):
            continue
        headline_key = next(
            (candidate for candidate in ("objective", "objective_value", "value", "duration", "result", "best_value") if candidate in payload),
            "",
        )
        headline = payload.get(headline_key) if headline_key else None
        items.append(
            {
                "id": str(key),
                "status": str(payload.get("status") or ""),
                "headline_label": headline_key,
                "headline": headline if isinstance(headline, (str, int, float, bool)) else None,
                "source": str(payload.get("source") or payload.get("source_file") or ""),
            }
        )
    return {
        "available": bool(value),
        "path": "results/canonical_results.json" if path.is_file() else "",
        "primary_method": str(value.get("primary_method") or ""),
        "auxiliary_method": str(value.get("auxiliary_method") or ""),
        "items": items[:12],
    }


def _solver_evidence(store: SQLiteStateStore, project: Path) -> dict[str, Any]:
    try:
        jobs = store.solver_jobs()
    except (OSError, RuntimeError):
        jobs = []
    receipts = project / ".factory" / "solver_receipts"
    ready = 0
    for job in jobs:
        job_id = str(job.get("job_id") or "")
        if (
            receipts.joinpath(f"{job_id}.submitted.json").is_file()
            and receipts.joinpath(f"{job_id}.completed.json").is_file()
        ):
            ready += 1
    counts: dict[str, int] = {}
    for job in jobs:
        status = str(job.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return {
        "total": len(jobs),
        "receipt_ready": ready,
        "status_counts": counts,
        "failed": sum(counts.get(key, 0) for key in ("failed", "timeout", "cancelled")),
    }


def build_contest_dashboard(
    project: str | Path,
    papers_root: str | Path,
    *,
    now_epoch: int | None = None,
) -> dict[str, Any]:
    project = Path(project).resolve()
    papers_root = Path(papers_root).resolve()
    now = int(time.time()) if now_epoch is None else int(now_epoch)
    store = SQLiteStateStore(project)
    state = store.load() if store.exists else None
    current_step = (
        state.active_step
        if state is not None and state.active_step is not None
        else max(0, state.last_completed_step) if state is not None else 0
    )
    timing = _timing(store, current_step=current_step, now_epoch=now) if state else {
        "configured": False,
        "risk_level": "unconfigured",
        "mode": "legacy",
        "mode_label": "Legacy 项目",
        "recommendation": "该项目没有 Native SQLite 状态。",
    }
    audits = [_audit(project, profile) for profile in ("model", "results", "paper", "final")]
    audit_by_profile = {item["profile"]: item for item in audits}
    final_audit = audit_by_profile["final"]
    canonical = _canonical_evidence(project)
    solver = _solver_evidence(store, project) if state else {"total": 0, "receipt_ready": 0, "status_counts": {}, "failed": 0}

    deliverables = _json(project / "problem" / "deliverables.json")
    attachments = []
    for item in deliverables.get("attachments", []) if isinstance(deliverables.get("attachments"), list) else []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("file") or "")
        if name:
            attachments.append(
                {"file": name, "present": _project_file_exists(project, name)}
            )
    missing_attachments = [item["file"] for item in attachments if not item["present"]]
    pdf = project / f"{project.name}_paper.pdf"
    final_checks = _json(project / "judge_outputs" / "final_paper_checks.json")
    hard_checks = [
        item for item in final_checks.get("checks", [])
        if isinstance(item, dict) and item.get("severity") == "hard"
    ]
    hard_checks_pass = bool(hard_checks) and all(bool(item.get("passed")) for item in hard_checks)
    visual = _json(project / "judge_outputs" / "visual_gate.json")
    visual_pass = bool(visual) and int(visual.get("blocking_findings", 1)) == 0
    aggregate = _json(project / "judge_outputs" / "aggregate.json")
    role_statuses = aggregate.get("role_statuses") if isinstance(aggregate.get("role_statuses"), dict) else {}
    roles_pass = bool(role_statuses) and all(role_statuses.get(role) == "PASS" for role in ("math", "execution", "paper"))
    content_decision = store.decision("content_freeze") if state else None
    override_decision = store.decision("delivery_freeze_override") if state else None
    release = resolve_current_release(papers_root, project.name)
    legacy_release_accepted = bool(
        not timing.get("configured")
        and release is not None
        and final_audit["delivery_allowed"]
    )

    checks = [
        _check("canonical", "规范结果", "pass" if canonical["available"] else "pending", "已绑定 canonical results" if canonical["available"] else "尚未生成规范结果", path=canonical["path"]),
        _check("paper_pdf", "最新论文 PDF", "pass" if pdf.is_file() else "pending", "已生成最新 PDF" if pdf.is_file() else "尚未编译最终 PDF", path=pdf.name if pdf.is_file() else ""),
        _check(
            "attachments",
            "必交附件",
            "pending" if not deliverables else "fail" if missing_attachments else "pass",
            "等待 deliverables 合同"
            if not deliverables
            else f"缺少 {', '.join(missing_attachments)}"
            if missing_attachments
            else "全部附件齐全"
            if attachments
            else "题目未要求额外交付附件",
            path="problem/deliverables.json" if deliverables else "",
        ),
        _check(
            "content_freeze",
            "内容冻结",
            "pass" if content_decision or legacy_release_accepted else "pending",
            "已人工确认主结论、摘要和核心图表"
            if content_decision
            else "Legacy 项目：已由有效 Final Audit 与原子 release 覆盖"
            if legacy_release_accepted
            else "等待人工确认",
            path="selection/content_freeze_request.md"
            if project.joinpath("selection/content_freeze_request.md").is_file()
            else "",
        ),
        _check("paper_checks", "确定性论文检查", "pass" if hard_checks_pass else "pending" if not final_checks else "fail", "全部 hard checks 通过" if hard_checks_pass else "等待 Final Audit" if not final_checks else "存在 hard check 失败", path="judge_outputs/final_paper_checks.json" if final_checks else ""),
        _check("visual", "PDF 视觉与页数", "pass" if visual_pass else "pending" if not visual else "fail", "无阻塞视觉问题" if visual_pass else "等待视觉检查" if not visual else f"{visual.get('blocking_findings', 0)} 个阻塞问题", path="judge_outputs/visual_gate.json" if visual else ""),
        _check("judges", "三角色 Judge", "pass" if roles_pass else "pending" if not aggregate else "fail", "Math / Execution / Paper 全部 PASS" if roles_pass else "等待三角色 Judge" if not aggregate else "存在非 PASS 角色", path="judge_outputs/aggregate.json" if aggregate else ""),
        _check("final_audit", "Final Audit", "pass" if final_audit["delivery_allowed"] else "pending" if not final_audit["available"] else "fail", f"{final_audit['status']} · {final_audit['snapshot_id'][:12]}" if final_audit["available"] else "等待最终快照审计", path=final_audit["path"]),
        _check("release", "原子发布", "pass" if release else "pending", f"当前 release {release.release_id[:12]}" if release else "尚未发布 current release"),
    ]
    blocking = [item for item in checks if item["blocking"] and item["status"] == "fail"]
    pending = [item for item in checks if item["blocking"] and item["status"] == "pending"]
    delivery_status = (
        "ready"
        if release and final_audit["delivery_allowed"] and not blocking and not pending
        else "blocked"
        if blocking
        else "pending"
    )

    actions: list[dict[str, Any]] = []
    pending_action = state.pending_action if state else None
    if pending_action:
        gate = str(pending_action.get("gate") or "")
        actions.append({
            "id": f"gate:{gate or 'human'}",
            "severity": "critical",
            "title": "需要人工决策",
            "summary": gate.replace("_", " ") or "待处理人工节点",
            "tab": "selection" if str(pending_action.get("type") or "").endswith("selection") else "consultation",
        })
    if timing.get("risk_level") in {"warning", "critical", "expired"}:
        actions.append({"id": "contest-clock", "severity": timing["risk_level"], "title": timing["mode_label"], "summary": timing["recommendation"], "tab": "overview"})
    if solver.get("failed"):
        actions.append({"id": "solver-failures", "severity": "warning", "title": "求解任务异常", "summary": f"{solver['failed']} 个任务失败、超时或取消", "tab": "solver"})
    if blocking:
        actions.append({"id": "delivery-blockers", "severity": "critical", "title": "交付存在阻塞", "summary": f"{len(blocking)} 项硬检查失败", "tab": "delivery"})
    elif current_step >= 15 and pending:
        actions.append({"id": "delivery-pending", "severity": "warning", "title": "交付尚未就绪", "summary": f"{len(pending)} 项检查待完成", "tab": "delivery"})
    if state and state.status.value == "failed":
        actions.append({"id": "workflow-failed", "severity": "critical", "title": "工作流失败", "summary": "打开诊断查看失败原因和恢复建议", "tab": "diagnostics"})

    return {
        "schema_version": "contest-dashboard-v1",
        "base_name": project.name,
        "current_step": current_step,
        "timing": timing,
        "gates": {
            "step3": store.decision("step3") if state else None,
            "content_freeze": content_decision,
            "delivery_freeze_override": override_decision,
        },
        "audits": audits,
        "evidence": {"canonical": canonical, "solver": solver, "role_statuses": role_statuses},
        "delivery": {
            "status": delivery_status,
            "ready": delivery_status == "ready",
            "checks": checks,
            "blocking_count": len(blocking),
            "pending_count": len(pending),
            "release": {
                "available": release is not None,
                "release_id": release.release_id if release else "",
                "paper_url": f"/api/projects/{project.name}/paper" if release else "",
                "submission_available": bool(release and release.submission_zip.is_file()),
            },
            "attachments": attachments,
        },
        "actions": actions,
    }
