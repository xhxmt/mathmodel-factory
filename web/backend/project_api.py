from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from fastapi.responses import FileResponse

from .access_control import filter_visible_projects, require_admin, require_project_access
from .auth import get_current_user
from .auth_store import AuthStore, ProjectNameConflict
from .config import Settings
from .consultation_service import write_consultation_answer
from .diagnostics_service import build_project_diagnostics, summarize_project_diagnostics
from .modeling_direction_service import (
    build_modeling_directions,
    write_modeling_direction_selection,
)
from .project_actions import run_action
from .selection_service import SelectionError, read_selection_request, write_selection_decision
from .schemas import (
    ConsultationAnswer,
    ConsultationRequest,
    ModelingDirectionSelection,
    ModelConfigPayload,
    ModelRegistryPayload,
    NewProjectRequest,
    ProjectAction,
    ProjectRequestCreate,
    ProjectRequestDecision,
    ProjectRequestResponse,
    ProjectStatus,
    SelectionDecisionRequest,
    UserInfo,
)
from .state_store import read_runtime_status
from .upload_service import ArchiveTraversalError, extract_archive, find_problem_file


BEIJING_TZ = timezone(timedelta(hours=8))
BASE_NAME_RE = re.compile(r"[A-Za-z0-9_-]+")
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}
PDF_EXTS = {".pdf"}
TEXT_EXTS = {
    ".md",
    ".txt",
    ".log",
    ".tex",
    ".bib",
    ".csv",
    ".json",
    ".py",
    ".jl",
    ".m",
    ".r",
    ".yaml",
    ".yml",
    ".toml",
    ".cfg",
    ".ini",
}
MAX_TEXT_BYTES = 1_500_000
MAX_ARTIFACTS = 500
SKIP_SUFFIX = {".aux", ".bbl", ".blg", ".toc", ".out", ".synctex.gz", ".pid", ".lock", ".pyc"}
SKIP_PARTS = {".git", "__pycache__", ".pytest_cache", "source.mineru", "runner_snapshots"}
VALID_BACKENDS = {"claude", "codex", "agy", "openai", "gemini", "deepseek"}
AGENTIC_BACKENDS = {"claude", "codex", "agy"}

ARTIFACT_GROUPS = {
    "problem": [
        "problem/problem_brief.md",
        "problem/data_inventory.md",
        "problem/feasibility_constraints.md",
        "problem/candidate_methods.md",
        "problem/terminology_table.md",
        "problem/method_retrieval.md",
    ],
    "method": [
        "research_brief.md",
        "viability_gate.md",
        "method_retrieval.md",
        "viable_streams.md",
        "m1_spec.md",
        "m2_spec.md",
        "m3_spec.md",
        "m1_critique.md",
        "m2_critique.md",
        "m3_critique.md",
        "method_decision.md",
        "chosen_method.md",
    ],
    "model": ["model.md", "symbol_table.md", "assumption_ledger.md"],
    "solve": ["solve_log.md", "sensitivity_report.md", "visualization_log.md"],
    "evaluation": [
        "evaluation.md",
        "code_review.md",
        "review_comments.md",
        "revision_summary.md",
        "judge_evaluation.md",
        "audit_issue_ledger.md",
        "citation_audit.md",
        "derobotification.md",
    ],
    "paper": ["abstract_draft.md", "references.bib"],
    "diagnostics": [
        "entry_gate.md",
        "reviewer_entry_map.md",
        "anchor_figure_plan.md",
        "human_review.md",
        "logs/runner.log",
        "diagnostics/status.json",
        "diagnostics/events.jsonl",
    ],
}

STEP_ARTIFACTS = {
    0: [
        "problem/problem_brief.md",
        "problem/data_inventory.md",
        "problem/feasibility_constraints.md",
        "problem/candidate_methods.md",
        "problem/terminology_table.md",
    ],
    1: ["research_brief.md", "viability_gate.md", "method_retrieval.md"],
    2: [
        "viable_streams.md",
        "m1_spec.md",
        "m2_spec.md",
        "m3_spec.md",
        "m1_critique.md",
        "m2_critique.md",
        "m3_critique.md",
    ],
    3: ["method_decision.md", "chosen_method.md"],
    4: ["model.md", "symbol_table.md", "assumption_ledger.md"],
    5: ["solve_log.md"],
    6: ["sensitivity_report.md"],
    7: ["evaluation.md"],
    8: ["visualization_log.md"],
    9: ["abstract_draft.md"],
    10: ["code_review.md"],
    11: ["review_comments.md"],
    12: ["revision_summary.md"],
    13: ["judge_evaluation.md"],
    14: ["abstract_draft.md"],
    15: ["citation_audit.md", "derobotification.md", "references.bib"],
    16: [],
}

DEFAULT_MODEL_REGISTRY = [
    {
        "id": "claude",
        "label": "Claude (默认 CLI)",
        "backend": "claude",
        "model": "",
        "effort": "max",
        "enabled": True,
        "builtin": True,
    },
    {
        "id": "codex-gpt55",
        "label": "Codex · GPT-5.5 (xhigh)",
        "backend": "codex",
        "model": "gpt-5.5",
        "effort": "xhigh",
        "enabled": True,
        "builtin": True,
    },
    {
        "id": "agy-gemini",
        "label": "Antigravity · Gemini 3.1 Pro",
        "backend": "agy",
        "model": "gemini-3.1-pro-preview",
        "enabled": True,
        "builtin": True,
    },
    {
        "id": "deepseek-chat",
        "label": "DeepSeek Chat (评委/API)",
        "backend": "openai",
        "model": "deepseek-chat",
        "base_url": "https://api.deepseek.com",
        "key_env": "DEEPSEEK_API_KEY",
        "enabled": True,
        "builtin": False,
    },
    {
        "id": "deepseek-reasoner",
        "label": "DeepSeek Reasoner (评委/API)",
        "backend": "openai",
        "model": "deepseek-reasoner",
        "base_url": "https://api.deepseek.com",
        "key_env": "DEEPSEEK_API_KEY",
        "enabled": True,
        "builtin": False,
    },
    {
        "id": "gemini-api",
        "label": "Gemini 2.5 Pro (评委/API)",
        "backend": "gemini",
        "model": "gemini-2.5-pro",
        "key_env": "GEMINI_API_KEY",
        "enabled": True,
        "builtin": False,
    },
]


def _fmt_beijing(epoch: int | float | str | None) -> str:
    if epoch in (None, ""):
        return ""
    return datetime.fromtimestamp(int(epoch), tz=BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S")


def _resolve_project(settings: Settings, base_name: str) -> Path:
    if not BASE_NAME_RE.fullmatch(base_name):
        raise HTTPException(status_code=400, detail="Invalid base_name")
    for root in (settings.ongoing_dir, settings.complete_dir):
        candidate = root / base_name
        if candidate.is_dir():
            return candidate
    raise HTTPException(status_code=404, detail=f"Project {base_name} not found")


def _runtime_to_project_status(runtime: dict[str, Any]) -> ProjectStatus:
    diag_summary = summarize_project_diagnostics({"status": runtime})
    return ProjectStatus(
        base_name=runtime["base_name"],
        status=runtime["status"],
        display_status=runtime.get("display_status", ""),
        current_step=runtime["current_step"],
        total_steps=runtime.get("total_steps", 16),
        progress_percent=runtime.get("progress_percent", 0.0),
        last_updated=_fmt_beijing(runtime.get("last_updated")),
        is_running=runtime.get("is_running", False),
        pid=runtime.get("pid"),
        consultation_pending=runtime.get("consultation_pending", False),
        consultation_gate=runtime.get("consultation_gate"),
        selection_pending=runtime.get("selection_pending", False),
        selection_gate=runtime.get("selection_gate"),
        selection_deadline=runtime.get("selection_deadline"),
        reason_code=runtime.get("reason_code", ""),
        reason_summary=runtime.get("reason_summary", ""),
        suggested_actions=list(runtime.get("suggested_actions", [])),
        evidence=list(runtime.get("evidence", [])),
        diagnostic_reason_code=diag_summary["diagnostic_reason_code"],
        diagnostic_badge=diag_summary["diagnostic_badge"],
        diagnostic_priority=diag_summary["diagnostic_priority"],
    )


def list_all_projects(settings: Settings) -> list[ProjectStatus]:
    projects: list[ProjectStatus] = []
    for root in (settings.ongoing_dir, settings.complete_dir):
        if not root.is_dir():
            continue
        for project_path in sorted(root.iterdir()):
            if project_path.is_dir() and (project_path / "checkpoint.md").is_file():
                projects.append(
                    _runtime_to_project_status(
                        read_runtime_status(project_path, project_path.name)
                    )
                )
    projects.sort(key=lambda project: project.last_updated, reverse=True)
    return projects


def _read_json(path: Path, default: Any) -> Any:
    try:
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default
    return default


def _write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_model_registry(settings: Settings) -> list[dict[str, Any]]:
    data = _read_json(settings.model_registry_file, None)
    if not isinstance(data, dict) or "models" not in data:
        _write_json_atomic(settings.model_registry_file, {"models": DEFAULT_MODEL_REGISTRY})
        return DEFAULT_MODEL_REGISTRY
    return data["models"]


def load_model_config(settings: Settings) -> dict[str, Any]:
    data = _read_json(settings.model_config_file, {})
    return data if isinstance(data, dict) else {}


def _valid_model_step_key(step_key: str) -> bool:
    return bool(re.fullmatch(r"step_(?:\d+|8_5)", step_key))


def _file_type(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in IMAGE_EXTS:
        return "image"
    if ext in PDF_EXTS:
        return "pdf"
    if ext == ".md":
        return "markdown"
    if ext == ".csv":
        return "csv"
    if ext == ".json":
        return "json"
    if ext in {".py", ".jl", ".m", ".r"}:
        return "code"
    if ext in TEXT_EXTS:
        return "text"
    return "other"


def _meta(project: Path, path: Path, group: str) -> dict[str, Any]:
    st = path.stat()
    return {
        "path": str(path.relative_to(project)),
        "name": path.name,
        "type": _file_type(path),
        "group": group,
        "size": st.st_size,
        "mtime": datetime.fromtimestamp(st.st_mtime, tz=BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S"),
    }


def _safe_path(project: Path, rel: str) -> Path:
    root = project.resolve()
    target = (root / rel).resolve()
    if target != root and root not in target.parents:
        raise HTTPException(status_code=400, detail="Invalid path")
    if any(part in SKIP_PARTS for part in target.parts):
        raise HTTPException(status_code=403, detail="Path not allowed")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return target


def _safe_upload_filename(filename: str) -> str:
    name = Path(filename.replace("\\", "/")).name
    if not name or name in {".", ".."}:
        raise ValueError("Invalid filename")
    return name


def _find_paper(settings: Settings, project: Path, base_name: str) -> Path | None:
    packaged = settings.papers_dir / f"{base_name}_paper.pdf"
    if packaged.is_file():
        return packaged
    for candidate in sorted(project.glob("*_paper.pdf")):
        return candidate
    return None


def list_artifacts(project: Path) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(path: Path, group: str) -> None:
        if len(items) >= MAX_ARTIFACTS or not path.is_file():
            return
        if path.suffix.lower() in SKIP_SUFFIX:
            return
        if any(part in SKIP_PARTS for part in path.parts):
            return
        key = str(path)
        if key in seen:
            return
        seen.add(key)
        items.append(_meta(project, path, group))

    for group, rels in ARTIFACT_GROUPS.items():
        for rel in rels:
            add(project / rel, group)

    for candidate in sorted(project.glob("*_paper.tex")):
        add(candidate, "paper")
    for candidate in sorted(project.glob("*_paper.pdf")):
        add(candidate, "paper")

    for folder, group, exts in (
        ("figures", "figures", IMAGE_EXTS | PDF_EXTS),
        ("results", "results", {".csv", ".json", ".md", ".txt", ".png"}),
        ("models", "code", {".py", ".jl", ".m", ".r", ".md", ".json", ".csv"}),
    ):
        root = project / folder
        if not root.is_dir():
            continue
        iterator = root.iterdir() if folder == "figures" else root.rglob("*")
        for candidate in sorted(iterator):
            if candidate.is_file() and candidate.suffix.lower() in exts:
                add(candidate, group)

    return items


def _read_editorial_gate(project: Path) -> dict[str, Any]:
    files = ["reviewer_entry_map.md", "anchor_figure_plan.md", "entry_gate.md"]
    artifacts = []
    for rel in files:
        candidate = project / rel
        if candidate.is_file():
            artifacts.append(_meta(project, candidate, "step"))

    verdict = None
    entry_gate = project / "entry_gate.md"
    if entry_gate.is_file():
        match = re.search(r"(?m)^VERDICT:\s*(PASS|REVISE)\s*$", entry_gate.read_text(encoding="utf-8", errors="ignore"))
        if match:
            verdict = match.group(1)

    return {
        "key": "8_5",
        "verdict": verdict,
        "ready": verdict == "PASS",
        "artifacts": artifacts,
        "present": len(artifacts) == 3,
    }


def get_steps(settings: Settings, project: Path, base_name: str) -> dict[str, Any]:
    runtime = read_runtime_status(project, base_name)
    current_step = runtime["current_step"]

    steps = []
    for idx in range(17):
        artifacts = []
        for rel in STEP_ARTIFACTS.get(idx, []):
            candidate = project / rel
            if candidate.is_file():
                artifacts.append(_meta(project, candidate, "step"))
        steps.append({"index": idx, "artifacts": artifacts})

    verdict = None
    judge_evaluation = project / "judge_evaluation.md"
    if judge_evaluation.is_file():
        match = re.search(r"VERDICT:\s*([A-Z_]+)", judge_evaluation.read_text(encoding="utf-8", errors="ignore"))
        if match:
            verdict = match.group(1)

    open_issues = None
    issue_ledger = project / "audit_issue_ledger.md"
    if issue_ledger.is_file():
        text = issue_ledger.read_text(encoding="utf-8", errors="ignore")
        open_issues = len(
            re.findall(r"(?im)^\s*[-|*].*\b(open|blocking|unresolved|未解决|阻塞|待处理)\b", text)
        )

    return {
        "current_step": current_step,
        "steps": steps,
        "editorial_gate": _read_editorial_gate(project),
        "verdict": verdict,
        "open_issues": open_issues,
        "paper_available": _find_paper(settings, project, base_name) is not None,
    }


def _check_consultation_pending(project_path: Path) -> tuple[bool, str | None]:
    consult_dir = project_path / "consultation"
    if not consult_dir.is_dir():
        return False, None

    human_review = project_path / "human_review.md"
    review_text = human_review.read_text(encoding="utf-8", errors="replace") if human_review.is_file() else ""
    for req_file in sorted(consult_dir.glob("*_request.md")):
        gate = req_file.stem.replace("_request", "")
        if f"CONSULT {gate}" in review_text and "STATUS: READY" in review_text:
            continue
        return True, gate
    return False, None


def get_consultation_request(project_path: Path) -> ConsultationRequest | None:
    pending, gate = _check_consultation_pending(project_path)
    if not pending or not gate:
        return None

    req_file = project_path / "consultation" / f"{gate}_request.md"
    if not req_file.is_file():
        return None

    content = req_file.read_text(encoding="utf-8", errors="replace")
    title_match = re.search(r"^#\s+咨询请求[：:]\s*(.+)$", content, re.MULTILINE)
    step_match = re.search(r"^-\s+step:\s*(\d+)", content, re.MULTILINE)
    created_match = re.search(r"^-\s+created:\s*(.+)$", content, re.MULTILINE)

    def extract_section(marker: str) -> str:
        pattern = re.escape(marker) + r"\s*\n(.*?)(?=\n##|\Z)"
        match = re.search(pattern, content, re.DOTALL)
        return match.group(1).strip() if match else ""

    main_content = extract_section("## 🤔 需要你（借助 GPT Pro / Gemini Deep Think）决定的事")
    if not main_content:
        parts = content.split("## 需要你（借助 GPT Pro / Gemini Deep Think）决定的事")
        if len(parts) > 1:
            main_content = parts[1].split("## 回填方式")[0].strip()

    background = extract_section("## 📋 项目背景")
    impact = extract_section("## 🎯 决策影响")
    suggestions = extract_section("## 💡 回答建议")
    key_files_section = extract_section("## 📁 关键文件引用")
    key_files = []
    for line in key_files_section.splitlines():
        file_match = re.search(r"`([^`]+)`", line)
        if file_match:
            key_files.append(file_match.group(1))

    return ConsultationRequest(
        gate=gate,
        step=int(step_match.group(1)) if step_match else 0,
        title=title_match.group(1).strip() if title_match else "Unknown",
        content=main_content,
        project=project_path.name,
        created=created_match.group(1).strip() if created_match else "Unknown",
        background=background or None,
        impact=impact or None,
        key_files=key_files or None,
        suggestions=suggestions or None,
    )


def _project_request_response(record) -> ProjectRequestResponse:
    return ProjectRequestResponse(
        id=record.id,
        requester=record.requester,
        base_name=record.base_name,
        problem_path=record.problem_path,
        no_start=record.no_start,
        consult=record.consult,
        status=record.status,
        created_at=record.created_at,
        decided_at=record.decided_at,
        decided_by=record.decided_by,
        decision_note=record.decision_note,
        launched_at=record.launched_at,
        launched_base_name=record.launched_base_name,
        launch_output=record.launch_output,
        failure_reason=record.failure_reason,
    )


def existing_project_names(settings: Settings) -> set[str]:
    names: set[str] = set()
    for root in (settings.ongoing_dir, settings.complete_dir):
        if root.is_dir():
            names.update(path.name for path in root.iterdir() if path.is_dir())
    return names


def run_project_launcher(settings: Settings, request: ProjectRequestCreate | NewProjectRequest):
    problem_path = Path(request.problem_path)
    cmd = ["/usr/bin/bash", str(settings.launch_script), "new"]
    if request.no_start:
        cmd.append("--no-start")
    if request.consult:
        cmd.append("--consult")
    cmd.extend([request.base_name, str(problem_path.resolve())])
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=30,
        cwd=settings.factory_root,
        env={**os.environ, "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"},
        check=False,
    )


def create_project_router(settings: Settings, ticket_store, manager) -> APIRouter:
    router = APIRouter()
    store = AuthStore(settings.resolved_auth_db_file)
    store.initialize()

    @router.post("/api/upload/problem")
    async def upload_problem_file(
        file: UploadFile = File(...),
        current_user: UserInfo = Depends(get_current_user(settings)),
    ):
        del current_user
        if not file.filename:
            raise HTTPException(status_code=400, detail="Missing filename")

        uploads_dir = settings.uploads_dir
        uploads_dir.mkdir(parents=True, exist_ok=True)
        content = await file.read()
        if len(content) > settings.max_upload_size:
            raise HTTPException(
                status_code=400,
                detail=f"文件过大。最大支持 {settings.max_upload_size // (1024 * 1024)} MB",
            )

        timestamp = datetime.now(BEIJING_TZ).strftime("%Y%m%d_%H%M%S")
        try:
            filename = _safe_upload_filename(file.filename)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        lower = filename.lower()
        ext = Path(filename).suffix.lower()
        is_archive = ext in {".zip", ".tar", ".tgz"} or lower.endswith((".tar.gz", ".tar.bz2", ".tar.xz"))
        is_problem_file = ext in {".pdf", ".md"}
        if not (is_archive or is_problem_file):
            raise HTTPException(
                status_code=400,
                detail="不支持的文件格式：支持 PDF、Markdown 或压缩包（.zip, .tar.gz, .tar.bz2, .tar.xz）",
            )

        if is_archive:
            extract_dir = uploads_dir / f"{timestamp}_{Path(filename).stem}"
            extract_dir.mkdir(parents=True, exist_ok=True)
            archive_path = extract_dir / filename
            archive_path.write_bytes(content)
            try:
                extract_archive(archive_path, extract_dir)
                archive_path.unlink(missing_ok=True)
                problem_file = find_problem_file(extract_dir)
                if not problem_file:
                    shutil.rmtree(extract_dir, ignore_errors=True)
                    raise HTTPException(status_code=400, detail="压缩包中未找到题目文件（PDF 或 Markdown）")
                return {
                    "status": "ok",
                    "message": "压缩包上传并解压成功",
                    "file_path": str(problem_file),
                    "filename": problem_file.name,
                    "size": len(content),
                    "extracted_dir": str(extract_dir),
                    "archive_name": filename,
                }
            except ArchiveTraversalError as exc:
                shutil.rmtree(extract_dir, ignore_errors=True)
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        else:
            safe_name = f"{timestamp}_{filename}"
            file_path = uploads_dir / safe_name
            file_path.write_bytes(content)
            return {
                "status": "ok",
                "message": "文件上传成功",
                "file_path": str(file_path),
                "filename": safe_name,
                "size": len(content),
            }

    @router.post("/api/projects/new")
    async def create_new_project(
        request: NewProjectRequest,
        current_user: UserInfo = Depends(get_current_user(settings)),
    ):
        if current_user.role != "admin":
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="PROJECT_APPROVAL_REQUIRED")
        problem_path = Path(request.problem_path)
        if not problem_path.exists():
            raise HTTPException(status_code=400, detail=f"Problem file not found: {request.problem_path}")

        result = run_project_launcher(settings, request)
        if result.returncode != 0:
            raise HTTPException(status_code=500, detail=f"Failed to create project: {result.stderr}")

        await manager.broadcast({"type": "project_created", "project": request.base_name, "user": "admin"})
        return {
            "status": "ok",
            "message": "Project created successfully",
            "base_name": request.base_name,
            "output": result.stdout,
        }

    @router.get("/api/project-requests", response_model=list[ProjectRequestResponse])
    async def list_project_requests(current_user: UserInfo = Depends(get_current_user(settings))):
        if current_user.role == "admin":
            records = store.list_project_requests()
        else:
            records = store.list_project_requests(current_user.username)
        return [_project_request_response(record) for record in records]

    @router.post("/api/project-requests", response_model=ProjectRequestResponse)
    async def create_project_request(
        request: ProjectRequestCreate,
        current_user: UserInfo = Depends(get_current_user(settings)),
    ):
        if current_user.role == "admin":
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="ADMIN_USE_DIRECT_CREATE")
        problem_path = Path(request.problem_path)
        if not problem_path.exists():
            raise HTTPException(status_code=400, detail=f"Problem file not found: {request.problem_path}")
        try:
            record = store.create_project_request(
                requester=current_user.username,
                base_name=request.base_name,
                problem_path=str(problem_path),
                no_start=request.no_start,
                consult=request.consult,
                existing_project_names=existing_project_names(settings),
            )
        except ProjectNameConflict as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="PROJECT_NAME_EXISTS") from exc
        await manager.broadcast(
            {
                "type": "project_request_created",
                "request_id": record.id,
                "requester": current_user.username,
            }
        )
        return _project_request_response(record)

    @router.post("/api/admin/project-requests/{request_id}/approve", response_model=ProjectRequestResponse)
    async def approve_project_request(
        request_id: int,
        decision: ProjectRequestDecision,
        current_user: UserInfo = Depends(get_current_user(settings)),
    ):
        del decision
        require_admin(current_user)
        record = store.require_project_request(request_id)
        if record.status != "pending":
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="PROJECT_REQUEST_NOT_PENDING")
        launcher_payload = ProjectRequestCreate(
            base_name=record.base_name,
            problem_path=record.problem_path,
            no_start=record.no_start,
            consult=record.consult,
        )
        result = run_project_launcher(settings, launcher_payload)
        if result.returncode != 0:
            failed = store.mark_project_request_failed(
                request_id,
                actor=current_user.username,
                failure_reason=result.stderr or result.stdout or "project launch failed",
            )
            await manager.broadcast({"type": "project_request_failed", "request_id": request_id})
            raise HTTPException(status_code=500, detail=failed.failure_reason)
        approved = store.approve_project_request(
            request_id,
            actor=current_user.username,
            launched_base_name=record.base_name,
            launch_output=result.stdout,
        )
        store.grant_project_owner(record.base_name, record.requester, actor=current_user.username)
        await manager.broadcast(
            {
                "type": "project_created",
                "project": record.base_name,
                "user": record.requester,
            }
        )
        return _project_request_response(approved)

    @router.post("/api/admin/project-requests/{request_id}/reject", response_model=ProjectRequestResponse)
    async def reject_project_request(
        request_id: int,
        decision: ProjectRequestDecision,
        current_user: UserInfo = Depends(get_current_user(settings)),
    ):
        require_admin(current_user)
        record = store.require_project_request(request_id)
        if record.status != "pending":
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="PROJECT_REQUEST_NOT_PENDING")
        rejected = store.reject_project_request(request_id, actor=current_user.username, reason=decision.note)
        await manager.broadcast({"type": "project_request_rejected", "request_id": request_id})
        return _project_request_response(rejected)

    @router.get("/api/projects", response_model=list[ProjectStatus])
    async def get_projects(current_user: UserInfo = Depends(get_current_user(settings))):
        return filter_visible_projects(settings, current_user, list_all_projects(settings))

    @router.get("/api/projects/{base_name}/status", response_model=ProjectStatus)
    async def get_single_project_status(
        base_name: str,
        current_user: UserInfo = Depends(get_current_user(settings)),
    ):
        require_project_access(settings, current_user, base_name)
        return _runtime_to_project_status(read_runtime_status(_resolve_project(settings, base_name), base_name))

    @router.get("/api/projects/{base_name}/diagnostics")
    async def get_project_diagnostics(
        base_name: str,
        current_user: UserInfo = Depends(get_current_user(settings)),
    ):
        require_project_access(settings, current_user, base_name)
        project = _resolve_project(settings, base_name)
        status_payload = read_runtime_status(project, base_name)
        return build_project_diagnostics(
            project,
            base_name,
            is_running=bool(status_payload.get("is_running")),
            consultation_pending=bool(status_payload.get("consultation_pending")),
            consultation_gate=status_payload.get("consultation_gate"),
        )

    @router.get("/api/projects/{base_name}/checkpoint")
    async def get_checkpoint(base_name: str, current_user: UserInfo = Depends(get_current_user(settings))):
        require_project_access(settings, current_user, base_name)
        checkpoint_file = _resolve_project(settings, base_name) / "checkpoint.md"
        if not checkpoint_file.is_file():
            raise HTTPException(status_code=404, detail="Checkpoint file not found")
        return {"content": checkpoint_file.read_text(encoding="utf-8", errors="replace")}

    @router.get("/api/projects/{base_name}/logs")
    async def get_recent_logs(
        base_name: str,
        lines: int = 100,
        current_user: UserInfo = Depends(get_current_user(settings)),
    ):
        require_project_access(settings, current_user, base_name)
        project = _resolve_project(settings, base_name)
        logs_dir = project / "logs"
        if not logs_dir.is_dir():
            return {"logs": []}

        all_logs = []
        for pattern in ("step_*.log", "step_*_claude_*.log", "step_*_codex_*.log", "step_*_agy_*.log"):
            all_logs.extend(logs_dir.glob(pattern))
        runner_log = logs_dir / "runner.log"
        if runner_log.is_file():
            all_logs.append(runner_log)
        all_logs = sorted(all_logs, key=lambda path: path.stat().st_mtime, reverse=True)
        all_logs = [path for path in all_logs if path.stat().st_size > 0] or all_logs

        if not all_logs:
            return {"logs": []}

        recent_log = all_logs[0]
        result = subprocess.run(
            ["tail", "-n", str(lines), str(recent_log)],
            capture_output=True,
            text=True,
            env={**os.environ, "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"},
            check=False,
        )
        return {"logs": result.stdout.split("\n"), "file": recent_log.name}

    @router.get("/api/projects/{base_name}/steps")
    async def get_project_steps(base_name: str, current_user: UserInfo = Depends(get_current_user(settings))):
        require_project_access(settings, current_user, base_name)
        project = _resolve_project(settings, base_name)
        return get_steps(settings, project, base_name)

    @router.get("/api/projects/{base_name}/files")
    async def get_files(base_name: str, current_user: UserInfo = Depends(get_current_user(settings))):
        require_project_access(settings, current_user, base_name)
        return {"files": list_artifacts(_resolve_project(settings, base_name))}

    @router.get("/api/projects/{base_name}/file")
    async def get_file_content(
        base_name: str,
        path: str,
        current_user: UserInfo = Depends(get_current_user(settings)),
    ):
        require_project_access(settings, current_user, base_name)
        project = _resolve_project(settings, base_name)
        target = _safe_path(project, path)
        if target.suffix.lower() not in TEXT_EXTS:
            raise HTTPException(status_code=415, detail="Not a previewable text file")
        if target.stat().st_size > MAX_TEXT_BYTES:
            raise HTTPException(status_code=413, detail="File too large to preview")
        return {"path": path, "type": _file_type(target), "content": target.read_text(encoding="utf-8", errors="replace")}

    @router.get("/api/projects/{base_name}/raw")
    async def get_raw_file(base_name: str, path: str, current_user: UserInfo = Depends(get_current_user(settings))):
        require_project_access(settings, current_user, base_name)
        project = _resolve_project(settings, base_name)
        target = _safe_path(project, path)
        if target.suffix.lower() not in (IMAGE_EXTS | PDF_EXTS | TEXT_EXTS):
            raise HTTPException(status_code=415, detail="Unsupported file type")
        return FileResponse(str(target))

    @router.get("/api/projects/{base_name}/paper")
    async def get_paper(
        base_name: str,
        download: bool = False,
        current_user: UserInfo = Depends(get_current_user(settings)),
    ):
        require_project_access(settings, current_user, base_name)
        project = _resolve_project(settings, base_name)
        paper = _find_paper(settings, project, base_name)
        if not paper:
            raise HTTPException(status_code=404, detail="Paper PDF not found")
        return FileResponse(str(paper), media_type="application/pdf", filename=paper.name if download else None)

    @router.get("/api/projects/{base_name}/consultation")
    async def get_consultation(base_name: str, current_user: UserInfo = Depends(get_current_user(settings))):
        require_project_access(settings, current_user, base_name)
        request = get_consultation_request(_resolve_project(settings, base_name))
        if not request:
            raise HTTPException(status_code=404, detail="No pending consultation request")
        return request

    @router.post("/api/projects/{base_name}/consultation/answer")
    async def submit_consultation_answer(
        base_name: str,
        answer: ConsultationAnswer,
        current_user: UserInfo = Depends(get_current_user(settings)),
    ):
        require_project_access(settings, current_user, base_name)
        project = _resolve_project(settings, base_name)
        request = get_consultation_request(project)
        if not request:
            raise HTTPException(status_code=404, detail="No pending consultation request")

        write_consultation_answer(
            project_path=project,
            gate=request.gate,
            step=request.step,
            title=request.title,
            answer=answer.answer,
            timestamp=datetime.now(BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S"),
        )
        await manager.broadcast({"type": "consultation_answered", "project": base_name, "gate": request.gate})
        return {"status": "ok", "message": "Consultation answer submitted"}

    @router.get("/api/projects/{base_name}/modeling-directions")
    async def get_modeling_directions(base_name: str, current_user: UserInfo = Depends(get_current_user(settings))):
        require_project_access(settings, current_user, base_name)
        project = _resolve_project(settings, base_name)
        return build_modeling_directions(project, settings.factory_root)

    @router.post("/api/projects/{base_name}/modeling-directions/selection")
    async def select_modeling_direction(
        base_name: str,
        selection: ModelingDirectionSelection,
        current_user: UserInfo = Depends(get_current_user(settings)),
    ):
        require_project_access(settings, current_user, base_name)
        project = _resolve_project(settings, base_name)
        payload = build_modeling_directions(project, settings.factory_root)
        if not payload.get("available"):
            raise HTTPException(status_code=409, detail=payload.get("message") or "Modeling directions are not available")

        direction_id = selection.direction_id.strip()
        direction = next((item for item in payload.get("directions", []) if item.get("id") == direction_id), None)
        if not direction:
            raise HTTPException(status_code=400, detail=f"Unknown modeling direction: {selection.direction_id}")

        write_modeling_direction_selection(
            project,
            direction,
            timestamp=datetime.now(BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S"),
        )
        await manager.broadcast({"type": "project_action", "project": base_name, "action": "select_modeling_direction"})
        return {"status": "ok", "message": "Modeling direction selected", "direction": direction}

    @router.get("/api/projects/{base_name}/selection")
    async def get_selection(base_name: str, current_user: UserInfo = Depends(get_current_user(settings))):
        require_project_access(settings, current_user, base_name)
        project = _resolve_project(settings, base_name)
        payload = read_selection_request(project, "step3")
        if not payload.get("gate") and not payload.get("options"):
            raise HTTPException(status_code=404, detail="No selection request")
        return payload

    @router.post("/api/projects/{base_name}/selection/decision")
    async def submit_selection_decision(
        base_name: str,
        decision: SelectionDecisionRequest,
        current_user: UserInfo = Depends(get_current_user(settings)),
    ):
        require_project_access(settings, current_user, base_name)
        project = _resolve_project(settings, base_name)
        try:
            saved = write_selection_decision(
                project,
                gate=decision.gate,
                selected_option_id=decision.selected_option_id.strip(),
                selected_aux_id=decision.selected_aux_id.strip(),
                source="human",
                reason=decision.reason.strip() or f"Selected by {current_user.username}",
            )
        except SelectionError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await manager.broadcast({"type": "project_action", "project": base_name, "action": "select_option"})
        result = run_action(settings.factory_root, "resume", base_name)
        if not result.ok:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=result.stderr or result.stdout or "project resume failed",
            )
        return {"status": "ok", "decision": saved}

    @router.post("/api/projects/{base_name}/action")
    async def project_action(
        base_name: str,
        action: ProjectAction,
        current_user: UserInfo = Depends(get_current_user(settings)),
    ):
        require_project_access(settings, current_user, base_name)
        result = run_action(settings.factory_root, action.action, base_name)
        if not result.ok:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=result.stderr or result.stdout or "project action failed",
            )
        await manager.broadcast({"type": "project_action", "project": base_name, "action": action.action})
        return {"status": "ok", "action": action.action, "output": result.stdout}

    @router.get("/api/models")
    async def get_models(current_user: UserInfo = Depends(get_current_user(settings))):
        del current_user
        return {
            "registry": load_model_registry(settings),
            "config": load_model_config(settings),
            "agentic_backends": sorted(AGENTIC_BACKENDS),
            "valid_backends": sorted(VALID_BACKENDS),
        }

    @router.put("/api/models/registry")
    async def put_model_registry(
        payload: ModelRegistryPayload,
        current_user: UserInfo = Depends(get_current_user(settings)),
    ):
        require_admin(current_user)
        seen = set()
        for model in payload.models:
            model_id = model.id.strip()
            if not model_id or not re.fullmatch(r"[a-zA-Z0-9_.-]+", model_id):
                raise HTTPException(status_code=400, detail=f"非法模型 id：{model.id!r}（仅限字母数字 . _ -）")
            if model_id in seen:
                raise HTTPException(status_code=400, detail=f"模型 id 重复：{model_id}")
            seen.add(model_id)
            if model.backend not in VALID_BACKENDS:
                raise HTTPException(status_code=400, detail=f"未知后端 {model.backend!r}")
            if model.backend not in ("claude",) and not model.model.strip():
                raise HTTPException(status_code=400, detail=f"模型 {model_id} 缺少 model 名称")
        _write_json_atomic(settings.model_registry_file, {"models": [model.dict() for model in payload.models]})
        await manager.broadcast({"type": "models_updated", "what": "registry"})
        return {"status": "ok", "count": len(payload.models)}

    @router.put("/api/models/config")
    async def put_model_config(
        payload: ModelConfigPayload,
        current_user: UserInfo = Depends(get_current_user(settings)),
    ):
        scope = payload.scope.strip()
        if not scope:
            raise HTTPException(status_code=400, detail="scope 不能为空")
        if current_user.role != "admin":
            if scope == "_default":
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="ADMIN_REQUIRED")
            require_project_access(settings, current_user, scope)

        config = load_model_config(settings)
        known_ids = {model["id"] for model in load_model_registry(settings)}
        cleaned = {}
        for step_key, assignment in payload.steps.items():
            if not _valid_model_step_key(step_key):
                raise HTTPException(status_code=400, detail=f"非法步骤键：{step_key}")
            primary = assignment.primary.strip()
            fallback = assignment.fallback.strip()
            if primary and primary not in known_ids:
                raise HTTPException(status_code=400, detail=f"未知模型 id：{primary}")
            if fallback and fallback not in known_ids:
                raise HTTPException(status_code=400, detail=f"未知模型 id：{fallback}")
            if not primary:
                continue
            entry = {"primary": primary}
            if fallback:
                entry["fallback"] = fallback
            cleaned[step_key] = entry

        if cleaned:
            config[scope] = cleaned
        else:
            config.pop(scope, None)

        _write_json_atomic(settings.model_config_file, config)
        await manager.broadcast({"type": "models_updated", "what": "config", "scope": scope})
        return {"status": "ok", "scope": scope, "steps": len(cleaned)}

    return router
