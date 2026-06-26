#!/usr/bin/env python3
"""
Paper Factory Web Dashboard - Backend API
提供实时项目监控、日志流和人工介入接口
"""
import os
import json
import time
import asyncio
from pathlib import Path
from typing import List, Dict, Optional
from datetime import datetime, timezone, timedelta
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Depends, status, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, field_validator
import subprocess
import re
import jwt
import bcrypt
import secrets
from dotenv import load_dotenv

# Load environment variables from .env file
env_path = Path(__file__).parent.parent / ".env"
load_dotenv(env_path)

# 北京时间时区 (UTC+8)
BEIJING_TZ = timezone(timedelta(hours=8))

# ============================================================================
# Configuration
# ============================================================================
FACTORY_ROOT = Path(__file__).parent.parent.parent.resolve()
ONGOING_DIR = FACTORY_ROOT / "ongoing"
COMPLETE_DIR = FACTORY_ROOT / "complete"
RUN_STATE_DIR = FACTORY_ROOT / "run_state"
LOGS_DIR = FACTORY_ROOT / "logs"
LAUNCH_SCRIPT = FACTORY_ROOT / "launch_agents.sh"
UPLOAD_DIR = FACTORY_ROOT / "uploads"
PAPERS_DIR = FACTORY_ROOT / "papers"

# Project base_name must be a single safe path segment (no traversal/separators).
# Reused by NewProjectRequest validation and _resolve_project (path-traversal guard).
_BASE_NAME_RE = re.compile(r"[A-Za-z0-9_-]+")

# Model registry + per-step model assignment (read by run_paper.sh; see its
# "Model registry & per-step model dispatch" section). Kept under web/ next to
# notes.json so the runner and dashboard share one location.
MODEL_REGISTRY_FILE = FACTORY_ROOT / "web" / "model_registry.json"
MODEL_CONFIG_FILE = FACTORY_ROOT / "web" / "model_config.json"

# Backends the runner knows how to drive. claude/codex/agy are AGENTIC (full
# file + tool + solver access); openai/gemini/deepseek are non-agentic HTTP
# APIs best suited to the judge / review / evaluation steps (7, 11, 13).
VALID_BACKENDS = {"claude", "codex", "agy", "openai", "gemini", "deepseek"}
AGENTIC_BACKENDS = {"claude", "codex", "agy"}

# Ensure upload directory exists
UPLOAD_DIR.mkdir(exist_ok=True)

# Max upload file size: 100MB
MAX_UPLOAD_SIZE = 100 * 1024 * 1024

# Authentication configuration
JWT_SECRET = os.getenv("JWT_SECRET") or os.getenv("JWT_SECRET_KEY")
if not JWT_SECRET:
    raise RuntimeError(
        "JWT_SECRET 未配置:请在 web/.env 设置 JWT_SECRET(生成命令: openssl rand -hex 32)。"
        " 未配置时旧实现每次启动随机生成密钥,会导致所有已签发 token 在重启后失效。"
    )
JWT_ALGORITHM = "HS256"
JWT_EXPIRATION_HOURS = 24

# Simple user database (in production, use a proper database)
# Password is hashed with SHA256
USERS_DB = {
    "admin": {
        # bcrypt hash computed once at startup from ADMIN_PASSWORD (salted, slow KDF).
        "password_hash": bcrypt.hashpw(
            os.getenv("ADMIN_PASSWORD", "admin123").encode(), bcrypt.gensalt()
        ),
        "username": "admin",
        "role": "admin"
    }
}

security = HTTPBearer()

# Lifespan context manager for startup/shutdown
async def monitor_projects_task():
    """Monitor projects for changes and broadcast updates"""
    last_state = {}

    while True:
        try:
            projects = list_all_projects()
            current_state = {p.base_name: p.dict() for p in projects}

            # Check for changes
            for base_name, state in current_state.items():
                if base_name not in last_state or last_state[base_name] != state:
                    await manager.broadcast({
                        "type": "project_updated",
                        "project": base_name,
                        "status": state
                    })

            last_state = current_state
            await asyncio.sleep(3)
        except Exception as e:
            print(f"Monitor error: {e}")
            await asyncio.sleep(5)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: start background monitoring
    monitor_task = asyncio.create_task(monitor_projects_task())
    yield
    # Shutdown: cancel background task
    monitor_task.cancel()
    try:
        await monitor_task
    except asyncio.CancelledError:
        pass

app = FastAPI(title="Paper Factory Dashboard", version="1.0.0", lifespan=lifespan)

# CORS configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://localhost:3000",
        "http://127.0.0.1:5173",
        "https://tfisher.de",
        "https://www.tfisher.de",
    ],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

# ============================================================================
# Models
# ============================================================================
class LoginRequest(BaseModel):
    username: str
    password: str

class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    username: str

class UserInfo(BaseModel):
    username: str
    role: str

class NewProjectRequest(BaseModel):
    base_name: str
    problem_path: str
    no_start: bool = False
    consult: bool = False

    @field_validator("base_name")
    @classmethod
    def _check_base_name(cls, v: str) -> str:
        if not _BASE_NAME_RE.fullmatch(v):
            raise ValueError("base_name 仅允许字母、数字、下划线、连字符")
        return v

class ProjectStatus(BaseModel):
    base_name: str
    status: str  # running, paused, completed, failed
    current_step: int
    total_steps: int = 16
    progress_percent: float
    last_updated: str
    is_running: bool
    pid: Optional[int] = None
    consultation_pending: bool = False
    consultation_gate: Optional[str] = None

class ConsultationRequest(BaseModel):
    gate: str
    step: int
    title: str
    content: str
    project: str
    created: str
    background: Optional[str] = None
    impact: Optional[str] = None
    key_files: Optional[List[str]] = None
    suggestions: Optional[str] = None

class ConsultationAnswer(BaseModel):
    answer: str

class ProjectAction(BaseModel):
    action: str  # resume, pause, kill

class ModelEntry(BaseModel):
    id: str
    label: str
    backend: str               # claude | codex | agy | openai | gemini | deepseek
    model: str = ""
    effort: str = ""           # codex/claude reasoning effort (optional)
    base_url: str = ""         # openai backend only
    key_env: str = ""          # openai/gemini backend: env var holding the key
    enabled: bool = True
    builtin: bool = False

class ModelRegistryPayload(BaseModel):
    models: List[ModelEntry]

class StepAssignment(BaseModel):
    primary: str = ""
    fallback: str = ""

class ModelConfigPayload(BaseModel):
    scope: str                 # project base_name, or "_default"
    steps: Dict[str, StepAssignment]   # keyed "step_<n>"

# ============================================================================
# Active WebSocket connections for real-time updates
# ============================================================================
class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except:
                pass

manager = ConnectionManager()

# ============================================================================
# Authentication Helper Functions
# ============================================================================
def create_access_token(username: str, role: str) -> str:
    """Create JWT access token"""
    expiration = datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRATION_HOURS)
    payload = {
        "sub": username,
        "role": role,
        "exp": expiration
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)

def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)) -> dict:
    """Verify JWT token and return payload"""
    try:
        token = credentials.credentials
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired"
        )
    except jwt.InvalidTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token"
        )

def get_current_user(payload: dict = Depends(verify_token)) -> UserInfo:
    """Get current authenticated user"""
    return UserInfo(username=payload["sub"], role=payload["role"])

def verify_password(password: str, password_hash: bytes) -> bool:
    """Verify a plaintext password against its bcrypt hash."""
    try:
        return bcrypt.checkpw(password.encode(), password_hash)
    except (ValueError, TypeError):
        return False

# ============================================================================
# Helper Functions
# ============================================================================
def read_checkpoint(project_path: Path) -> Dict:
    """Parse checkpoint.md to extract step info"""
    checkpoint_file = project_path / "checkpoint.md"
    if not checkpoint_file.exists():
        return {"step": -1, "status": "unknown"}

    content = checkpoint_file.read_text()
    # Tolerate markdown bold and full-width colon, e.g. "- **Last completed step**: 16"
    step_match = re.search(r'Last completed step\*{0,2}\s*[:：]\s*(-?\d+)', content)
    step = int(step_match.group(1)) if step_match else -1

    return {"step": step, "status": "ok"}

def get_runner_pid(project_path: Path) -> Optional[int]:
    """Check if project has a running process"""
    pid_file = project_path / ".runner.pid"
    if not pid_file.exists():
        return None

    try:
        pid = int(pid_file.read_text().strip())
        # Check if process is alive
        os.kill(pid, 0)
        return pid
    except (ValueError, ProcessLookupError, OSError):
        return None

def check_consultation_pending(project_path: Path) -> tuple[bool, Optional[str]]:
    """Check if project has pending consultation request"""
    consult_dir = project_path / "consultation"
    if not consult_dir.exists():
        return False, None

    # Check for request files
    for req_file in consult_dir.glob("*_request.md"):
        gate = req_file.stem.replace("_request", "")
        # Check if not yet answered in human_review.md
        human_review = project_path / "human_review.md"
        if human_review.exists():
            content = human_review.read_text()
            if f"CONSULT {gate}" in content and "STATUS: READY" not in content:
                return True, gate
        else:
            return True, gate

    return False, None

def get_project_status(project_path: Path, base_name: str) -> ProjectStatus:
    """Get comprehensive project status"""
    checkpoint = read_checkpoint(project_path)
    current_step = checkpoint["step"]
    pid = get_runner_pid(project_path)
    is_running = pid is not None

    # Check pause/kill markers
    is_paused = (project_path / ".paused").exists()
    is_killed = (project_path / ".killed").exists()

    consult_pending, consult_gate = check_consultation_pending(project_path)

    # Determine status
    if is_killed:
        status = "killed"
    elif is_paused:
        status = "paused"
    elif consult_pending:
        status = "awaiting_consultation"
    elif is_running:
        status = "running"
    elif current_step >= 16:
        status = "completed"
    elif current_step >= 0:
        status = "ready"
    else:
        status = "setup"

    # Get last modification time (转换为北京时间)
    last_updated = datetime.fromtimestamp(
        project_path.stat().st_mtime, tz=BEIJING_TZ
    ).strftime("%Y-%m-%d %H:%M:%S")

    progress = min(100.0, max(0, current_step) / 16 * 100) if current_step >= 0 else 0

    return ProjectStatus(
        base_name=base_name,
        status=status,
        current_step=current_step,
        progress_percent=round(progress, 1),
        last_updated=last_updated,
        is_running=is_running,
        pid=pid,
        consultation_pending=consult_pending,
        consultation_gate=consult_gate
    )

def list_all_projects() -> List[ProjectStatus]:
    """List all ongoing and completed projects"""
    projects = []

    # Ongoing projects
    if ONGOING_DIR.exists():
        for project_path in ONGOING_DIR.iterdir():
            if project_path.is_dir() and (project_path / "checkpoint.md").exists():
                base_name = project_path.name
                projects.append(get_project_status(project_path, base_name))

    # Completed projects
    if COMPLETE_DIR.exists():
        for project_path in COMPLETE_DIR.iterdir():
            if project_path.is_dir() and (project_path / "checkpoint.md").exists():
                base_name = project_path.name
                status = get_project_status(project_path, base_name)
                status.status = "completed"
                projects.append(status)

    # Sort by last updated (newest first)
    projects.sort(key=lambda p: p.last_updated, reverse=True)
    return projects

def get_consultation_request(project_path: Path) -> Optional[ConsultationRequest]:
    """Get pending consultation request details"""
    consult_pending, gate = check_consultation_pending(project_path)
    if not consult_pending or not gate:
        return None

    req_file = project_path / "consultation" / f"{gate}_request.md"
    if not req_file.exists():
        return None

    content = req_file.read_text()

    # Parse markdown structure
    title_match = re.search(r'^#\s+咨询请求[：:]\s*(.+)$', content, re.MULTILINE)
    step_match = re.search(r'^-\s+step:\s*(\d+)', content, re.MULTILINE)
    created_match = re.search(r'^-\s+created:\s*(.+)$', content, re.MULTILINE)

    # Extract sections
    def extract_section(marker_start: str, marker_end: str = None) -> str:
        """Extract content between two markdown headers"""
        pattern = re.escape(marker_start) + r'\s*\n(.*?)(?=\n##|\Z)'
        match = re.search(pattern, content, re.DOTALL)
        if match:
            return match.group(1).strip()
        return ""

    # Extract main consultation question
    main_content = extract_section("## 🤔 需要你（借助 GPT Pro / Gemini Deep Think）决定的事")
    if not main_content:
        # Fallback to old format
        content_parts = content.split("## 需要你（借助 GPT Pro / Gemini Deep Think）决定的事")
        main_content = content_parts[1].split("## 回填方式")[0].strip() if len(content_parts) > 1 else ""

    # Extract additional context
    background = extract_section("## 📋 项目背景")
    impact = extract_section("## 🎯 决策影响")
    suggestions = extract_section("## 💡 回答建议")

    # Extract key files list
    key_files_section = extract_section("## 📁 关键文件引用")
    key_files = []
    if key_files_section:
        for line in key_files_section.split('\n'):
            if line.strip().startswith('-') and '`' in line:
                # Extract filename from markdown list item like "- `problem/problem_brief.md` — 问题解析"
                file_match = re.search(r'`([^`]+)`', line)
                if file_match:
                    key_files.append(file_match.group(1))

    return ConsultationRequest(
        gate=gate,
        step=int(step_match.group(1)) if step_match else 0,
        title=title_match.group(1).strip() if title_match else "Unknown",
        content=main_content,
        project=project_path.name,
        created=created_match.group(1).strip() if created_match else "Unknown",
        background=background if background else None,
        impact=impact if impact else None,
        key_files=key_files if key_files else None,
        suggestions=suggestions if suggestions else None
    )

# ============================================================================
# Artifact / file browsing helpers (read-only)
# ============================================================================
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}
PDF_EXTS = {".pdf"}
TEXT_EXTS = {".md", ".txt", ".log", ".tex", ".bib", ".csv", ".json",
             ".py", ".jl", ".m", ".r", ".yaml", ".yml", ".toml", ".cfg", ".ini"}
MAX_TEXT_BYTES = 1_500_000
MAX_ARTIFACTS = 500

# Never expose these (noise / large / sensitive)
_SKIP_SUFFIX = {".aux", ".bbl", ".blg", ".toc", ".out", ".synctex.gz",
                ".pid", ".lock", ".pyc"}
_SKIP_PARTS = {".git", "__pycache__", ".pytest_cache", "source.mineru",
               "runner_snapshots"}

# Curated artifact groups: relative paths surfaced in the browser
_ARTIFACT_GROUPS = {
    "problem": [
        "problem/problem_brief.md", "problem/data_inventory.md",
        "problem/feasibility_constraints.md", "problem/candidate_methods.md",
        "problem/terminology_table.md", "problem/method_retrieval.md",
    ],
    "method": [
        "research_brief.md", "viability_gate.md", "method_retrieval.md",
        "viable_streams.md",
        "m1_spec.md", "m2_spec.md", "m3_spec.md",
        "m1_critique.md", "m2_critique.md", "m3_critique.md",
        "method_decision.md", "chosen_method.md",
    ],
    "model": ["model.md", "symbol_table.md", "assumption_ledger.md"],
    "solve": ["solve_log.md", "sensitivity_report.md", "visualization_log.md"],
    "evaluation": [
        "evaluation.md", "code_review.md", "review_comments.md",
        "revision_summary.md", "judge_evaluation.md", "audit_issue_ledger.md",
        "citation_audit.md", "derobotification.md",
    ],
    "paper": ["abstract_draft.md", "references.bib"],
}

# Per-step expected artifacts; existence ⇒ evidence available for that step.
STEP_ARTIFACTS = {
    0: ["problem/problem_brief.md", "problem/data_inventory.md",
        "problem/feasibility_constraints.md", "problem/candidate_methods.md",
        "problem/terminology_table.md"],
    1: ["research_brief.md", "viability_gate.md", "method_retrieval.md"],
    2: ["viable_streams.md", "m1_spec.md", "m2_spec.md", "m3_spec.md",
        "m1_critique.md", "m2_critique.md", "m3_critique.md"],
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


def _valid_model_step_key(step_key: str) -> bool:
    return bool(re.fullmatch(r"step_(?:\d+|8_5)", step_key))


def _file_type(p: Path) -> str:
    ext = p.suffix.lower()
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


def _resolve_project(base_name: str) -> Path:
    """Resolve a project to its ongoing or completed directory."""
    if not _BASE_NAME_RE.fullmatch(base_name):
        raise HTTPException(status_code=400, detail="Invalid base_name")
    for root in (ONGOING_DIR, COMPLETE_DIR):
        cand = root / base_name
        if cand.is_dir():
            return cand
    raise HTTPException(status_code=404, detail=f"Project {base_name} not found")


def _safe_path(project: Path, rel: str) -> Path:
    """Resolve rel within project, refusing traversal outside it."""
    project = project.resolve()
    target = (project / rel).resolve()
    if target != project and project not in target.parents:
        raise HTTPException(status_code=400, detail="Invalid path")
    if any(part in _SKIP_PARTS for part in target.parts):
        raise HTTPException(status_code=403, detail="Path not allowed")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return target


def _meta(project: Path, p: Path, group: str) -> dict:
    st = p.stat()
    return {
        "path": str(p.relative_to(project)),
        "name": p.name,
        "type": _file_type(p),
        "group": group,
        "size": st.st_size,
        "mtime": datetime.fromtimestamp(st.st_mtime, tz=BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S"),
    }


def _find_paper(project: Path, base_name: str) -> Optional[Path]:
    """Locate the produced paper PDF (packaged copy preferred)."""
    packaged = PAPERS_DIR / f"{base_name}_paper.pdf"
    if packaged.is_file():
        return packaged
    for p in sorted(project.glob("*_paper.pdf")):
        return p
    return None


def list_artifacts(project: Path) -> List[dict]:
    """Curated, grouped list of project artifacts for the browser."""
    out: List[dict] = []
    seen = set()

    def add(p: Path, group: str):
        if len(out) >= MAX_ARTIFACTS or not p.is_file():
            return
        if p.suffix.lower() in _SKIP_SUFFIX:
            return
        if any(part in _SKIP_PARTS for part in p.parts):
            return
        key = str(p)
        if key in seen:
            return
        seen.add(key)
        out.append(_meta(project, p, group))

    for group, rels in _ARTIFACT_GROUPS.items():
        for rel in rels:
            add(project / rel, group)

    for p in sorted(project.glob("*_paper.tex")):
        add(p, "paper")
    for p in sorted(project.glob("*_paper.pdf")):
        add(p, "paper")

    figdir = project / "figures"
    if figdir.is_dir():
        for p in sorted(figdir.iterdir()):
            if p.is_file() and p.suffix.lower() in (IMAGE_EXTS | PDF_EXTS):
                add(p, "figures")

    resdir = project / "results"
    if resdir.is_dir():
        for p in sorted(resdir.rglob("*")):
            if p.is_file() and p.suffix.lower() in {".csv", ".json", ".md", ".txt", ".png"}:
                add(p, "results")

    moddir = project / "models"
    if moddir.is_dir():
        for p in sorted(moddir.rglob("*")):
            if p.is_file() and p.suffix.lower() in {".py", ".jl", ".m", ".r", ".md", ".json", ".csv"}:
                add(p, "code")

    return out


def _read_editorial_gate(project: Path) -> dict:
    files = ["reviewer_entry_map.md", "anchor_figure_plan.md", "entry_gate.md"]
    artifacts = []
    for rel in files:
        p = project / rel
        if p.is_file():
            artifacts.append(_meta(project, p, "step"))

    verdict = None
    entry_gate = project / "entry_gate.md"
    if entry_gate.is_file():
        m = re.search(
            r"(?m)^VERDICT:\s*(PASS|REVISE)\s*$",
            entry_gate.read_text(errors="ignore"),
        )
        if m:
            verdict = m.group(1)

    return {
        "key": "8_5",
        "verdict": verdict,
        "ready": verdict == "PASS",
        "artifacts": artifacts,
        "present": len(artifacts) == 3,
    }


def get_steps(project: Path, base_name: str) -> dict:
    """Per-step status derived from checkpoint + artifacts, plus gate signals."""
    cp = read_checkpoint(project)
    current = cp["step"]

    steps = []
    for i in range(0, 17):
        arts = []
        for rel in STEP_ARTIFACTS.get(i, []):
            p = project / rel
            if p.is_file():
                arts.append(_meta(project, p, "step"))
        steps.append({"index": i, "artifacts": arts})

    verdict = None
    je = project / "judge_evaluation.md"
    if je.is_file():
        m = re.search(r'VERDICT:\s*([A-Z_]+)', je.read_text(errors="ignore"))
        if m:
            verdict = m.group(1)

    open_issues = None
    ail = project / "audit_issue_ledger.md"
    if ail.is_file():
        txt = ail.read_text(errors="ignore")
        open_issues = len(re.findall(
            r'(?im)^\s*[-|*].*\b(open|blocking|unresolved|未解决|阻塞|待处理)\b', txt))

    paper = _find_paper(project, base_name)
    editorial_gate = _read_editorial_gate(project)

    return {
        "current_step": current,
        "steps": steps,
        "editorial_gate": editorial_gate,
        "verdict": verdict,
        "open_issues": open_issues,
        "paper_available": paper is not None,
    }


# ============================================================================
# API Endpoints
# ============================================================================
@app.get("/")
async def root():
    return {"status": "Paper Factory Dashboard API", "version": "1.0.0"}

@app.post("/api/auth/login", response_model=LoginResponse)
async def login(request: LoginRequest):
    """Authenticate user and return JWT token"""
    user = USERS_DB.get(request.username)

    if not user or not verify_password(request.password, user["password_hash"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password"
        )

    access_token = create_access_token(user["username"], user["role"])

    return LoginResponse(
        access_token=access_token,
        username=user["username"]
    )

@app.get("/api/auth/me", response_model=UserInfo)
async def get_me(current_user: UserInfo = Depends(get_current_user)):
    """Get current user information"""
    return current_user

@app.post("/api/auth/logout")
async def logout(current_user: UserInfo = Depends(get_current_user)):
    """Logout (client-side token removal)"""
    return {"status": "ok", "message": "Logged out successfully"}

@app.post("/api/upload/problem")
async def upload_problem_file(
    file: UploadFile = File(...),
    current_user: UserInfo = Depends(get_current_user)
):
    """Upload a problem file (PDF, Markdown, or compressed archive with problem file)"""
    import tarfile
    import zipfile
    import shutil

    try:
        # Validate file extension
        allowed_extensions = {'.pdf', '.md', '.PDF', '.MD', '.zip', '.tar', '.gz', '.bz2', '.xz', '.tgz', '.tar.gz', '.tar.bz2', '.tar.xz'}
        file_ext = Path(file.filename).suffix.lower()
        filename_lower = file.filename.lower()

        # Check for compound extensions like .tar.gz
        is_archive = (
            file_ext in {'.zip', '.tar', '.tgz'} or
            filename_lower.endswith(('.tar.gz', '.tar.bz2', '.tar.xz'))
        )
        is_problem_file = file_ext in {'.pdf', '.md'}

        if not (is_archive or is_problem_file):
            raise HTTPException(
                status_code=400,
                detail=f"不支持的文件格式：{file_ext}。支持 PDF、Markdown 或压缩包（.zip, .tar.gz, .tar.bz2, .tar.xz）"
            )

        # Read file content
        content = await file.read()

        # Check file size
        if len(content) > MAX_UPLOAD_SIZE:
            raise HTTPException(
                status_code=400,
                detail=f"文件过大。最大支持 {MAX_UPLOAD_SIZE // (1024*1024)} MB"
            )

        # Generate unique directory/filename with timestamp
        timestamp = datetime.now(BEIJING_TZ).strftime("%Y%m%d_%H%M%S")

        if is_archive:
            # Extract archive to a dedicated directory
            extract_dir = UPLOAD_DIR / f"{timestamp}_{Path(file.filename).stem}"
            extract_dir.mkdir(parents=True, exist_ok=True)

            # Save archive temporarily
            archive_path = extract_dir / file.filename
            with open(archive_path, "wb") as f:
                f.write(content)

            # Extract based on type
            try:
                if file_ext == '.zip' or filename_lower.endswith('.zip'):
                    with zipfile.ZipFile(archive_path, 'r') as zip_ref:
                        zip_ref.extractall(extract_dir)
                elif file_ext == '.tar' or filename_lower.endswith(('.tar', '.tgz', '.tar.gz', '.tar.bz2', '.tar.xz')):
                    with tarfile.open(archive_path, 'r:*') as tar_ref:
                        tar_ref.extractall(extract_dir)
                else:
                    raise ValueError(f"Unsupported archive format: {file.filename}")

                # Remove the archive file after extraction
                archive_path.unlink()

                # Find problem file (PDF or MD) in extracted contents
                problem_file = None
                for root, dirs, files in os.walk(extract_dir):
                    for fname in files:
                        if fname.lower().endswith(('.pdf', '.md')):
                            candidate = Path(root) / fname
                            # Prefer files with "problem", "题目", "question" in name
                            if any(keyword in fname.lower() for keyword in ['problem', '题目', 'question', '题', 'problem']):
                                problem_file = candidate
                                break
                            # Otherwise, use first PDF/MD found
                            if not problem_file:
                                problem_file = candidate
                    if problem_file:
                        break

                if not problem_file:
                    # Clean up and report error
                    shutil.rmtree(extract_dir)
                    raise HTTPException(
                        status_code=400,
                        detail="压缩包中未找到题目文件（PDF 或 Markdown）"
                    )

                return {
                    "status": "ok",
                    "message": "压缩包上传并解压成功",
                    "file_path": str(problem_file),
                    "filename": problem_file.name,
                    "size": len(content),
                    "extracted_dir": str(extract_dir),
                    "archive_name": file.filename
                }

            except (zipfile.BadZipFile, tarfile.TarError) as e:
                # Clean up on extraction failure
                if extract_dir.exists():
                    shutil.rmtree(extract_dir)
                raise HTTPException(
                    status_code=400,
                    detail=f"压缩包解压失败：{str(e)}"
                )
        else:
            # Handle single problem file (existing logic)
            safe_filename = f"{timestamp}_{file.filename}"
            file_path = UPLOAD_DIR / safe_filename

            # Save file
            with open(file_path, "wb") as f:
                f.write(content)

            return {
                "status": "ok",
                "message": "文件上传成功",
                "file_path": str(file_path),
                "filename": safe_filename,
                "size": len(content)
            }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"文件上传失败: {str(e)}")

@app.post("/api/projects/new")
async def create_new_project(
    request: NewProjectRequest,
    current_user: UserInfo = Depends(get_current_user)
):
    """Create a new modeling project"""
    try:
        # Validate problem file exists
        problem_path = Path(request.problem_path)
        if not problem_path.exists():
            raise HTTPException(
                status_code=400,
                detail=f"Problem file not found: {request.problem_path}"
            )

        # Build command - use bash directly to avoid env issues
        cmd = ["/usr/bin/bash", str(LAUNCH_SCRIPT), "new"]
        if request.no_start:
            cmd.append("--no-start")
        if request.consult:
            cmd.append("--consult")
        cmd.extend([request.base_name, str(problem_path.resolve())])

        # Execute command with full PATH environment
        env = os.environ.copy()
        env["PATH"] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
            cwd=FACTORY_ROOT,
            env=env
        )

        if result.returncode != 0:
            raise HTTPException(
                status_code=500,
                detail=f"Failed to create project: {result.stderr}"
            )

        await manager.broadcast({
            "type": "project_created",
            "project": request.base_name,
            "user": current_user.username
        })

        return {
            "status": "ok",
            "message": "Project created successfully",
            "base_name": request.base_name,
            "output": result.stdout
        }
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=500, detail="Project creation timed out")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/projects", response_model=List[ProjectStatus])
async def get_projects(current_user: UserInfo = Depends(get_current_user)):
    """Get list of all projects with their status"""
    return list_all_projects()

@app.get("/api/projects/{base_name}/status", response_model=ProjectStatus)
async def get_single_project_status(
    base_name: str,
    current_user: UserInfo = Depends(get_current_user)
):
    """Get detailed status for a specific project"""
    project_path = ONGOING_DIR / base_name
    if not project_path.exists():
        project_path = COMPLETE_DIR / base_name

    if not project_path.exists():
        raise HTTPException(status_code=404, detail=f"Project {base_name} not found")

    return get_project_status(project_path, base_name)

@app.get("/api/projects/{base_name}/checkpoint")
async def get_checkpoint(
    base_name: str,
    current_user: UserInfo = Depends(get_current_user)
):
    """Get checkpoint.md content"""
    project_path = ONGOING_DIR / base_name
    if not project_path.exists():
        project_path = COMPLETE_DIR / base_name

    checkpoint_file = project_path / "checkpoint.md"
    if not checkpoint_file.exists():
        raise HTTPException(status_code=404, detail="Checkpoint file not found")

    return {"content": checkpoint_file.read_text()}

@app.get("/api/projects/{base_name}/logs")
async def get_recent_logs(
    base_name: str,
    lines: int = 100,
    current_user: UserInfo = Depends(get_current_user)
):
    """Get recent log entries for a project"""
    project_path = ONGOING_DIR / base_name
    if not project_path.exists():
        project_path = COMPLETE_DIR / base_name

    logs_dir = project_path / "logs"
    if not logs_dir.exists():
        return {"logs": []}

    # Find most recent log file (including claude/codex/agy variants)
    log_patterns = [
        "step_*.log",
        "step_*_claude_*.log",
        "step_*_codex_*.log",
        "step_*_agy_*.log"
    ]

    all_logs = []
    for pattern in log_patterns:
        all_logs.extend(logs_dir.glob(pattern))

    # Sort by modification time, newest first
    log_files = sorted(all_logs, key=lambda f: f.stat().st_mtime, reverse=True)

    # Filter out empty files and prefer files with actual content
    non_empty_logs = [f for f in log_files if f.stat().st_size > 0]
    if non_empty_logs:
        log_files = non_empty_logs

    if not log_files:
        # Fallback to runner.log if no step logs available
        runner_log = logs_dir / "runner.log"
        if runner_log.exists() and runner_log.stat().st_size > 0:
            log_files = [runner_log]
        else:
            return {"logs": []}

    recent_log = log_files[0]
    try:
        env = os.environ.copy()
        env["PATH"] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
        result = subprocess.run(
            ["tail", "-n", str(lines), str(recent_log)],
            capture_output=True,
            text=True,
            env=env
        )
        return {
            "logs": result.stdout.split("\n"),
            "file": recent_log.name
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/projects/{base_name}/runner-log")
async def get_runner_log(
    base_name: str,
    lines: int = 100,
    current_user: UserInfo = Depends(get_current_user)
):
    """Get runner.log entries (main orchestration log)"""
    project_path = ONGOING_DIR / base_name
    if not project_path.exists():
        project_path = COMPLETE_DIR / base_name

    runner_log = project_path / "logs" / "runner.log"
    if not runner_log.exists():
        return {"logs": [], "file": "runner.log"}

    try:
        env = os.environ.copy()
        env["PATH"] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
        result = subprocess.run(
            ["tail", "-n", str(lines), str(runner_log)],
            capture_output=True,
            text=True,
            env=env
        )
        return {
            "logs": result.stdout.split("\n"),
            "file": "runner.log"
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/projects/{base_name}/issues")
async def get_project_issues(
    base_name: str,
    current_user: UserInfo = Depends(get_current_user)
):
    """Get audit issue ledger"""
    project_path = ONGOING_DIR / base_name
    if not project_path.exists():
        project_path = COMPLETE_DIR / base_name

    if not project_path.exists():
        raise HTTPException(status_code=404, detail=f"Project {base_name} not found")

    ail = project_path / "audit_issue_ledger.md"
    if not ail.exists():
        return {
            "exists": False,
            "issues": [],
            "blocking": 0,
            "major": 0,
            "minor": 0
        }

    try:
        content = ail.read_text()

        # Parse markdown table
        lines = content.split('\n')
        issues = []
        blocking_count = 0
        major_count = 0
        minor_count = 0

        # Find table rows (skip header and separator)
        in_table = False
        for line in lines:
            if line.startswith('| id |'):
                in_table = True
                continue
            if in_table and line.startswith('|---'):
                continue
            if in_table and line.startswith('|'):
                parts = [p.strip() for p in line.split('|')[1:-1]]
                if len(parts) >= 7:
                    issue = {
                        "id": parts[0],
                        "step": parts[1],
                        "severity": parts[2],
                        "status": parts[3],
                        "location": parts[4],
                        "issue": parts[5],
                        "required_fix": parts[6]
                    }
                    issues.append(issue)

                    # Count by severity
                    if parts[2] == "BLOCKING":
                        blocking_count += 1
                    elif parts[2] == "MAJOR":
                        major_count += 1
                    elif parts[2] == "MINOR":
                        minor_count += 1

        return {
            "exists": True,
            "content": content,
            "issues": issues,
            "blocking": blocking_count,
            "major": major_count,
            "minor": minor_count,
            "total": len(issues)
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/projects/{base_name}/consultation")
async def get_consultation(
    base_name: str,
    current_user: UserInfo = Depends(get_current_user)
):
    """Get pending consultation request"""
    project_path = ONGOING_DIR / base_name
    if not project_path.exists():
        raise HTTPException(status_code=404, detail=f"Project {base_name} not found")

    request = get_consultation_request(project_path)
    if not request:
        raise HTTPException(status_code=404, detail="No pending consultation request")

    return request

@app.post("/api/projects/{base_name}/consultation/answer")
async def submit_consultation_answer(
    base_name: str,
    answer: ConsultationAnswer,
    current_user: UserInfo = Depends(get_current_user)
):
    """Submit human consultation answer"""
    project_path = ONGOING_DIR / base_name
    if not project_path.exists():
        raise HTTPException(status_code=404, detail=f"Project {base_name} not found")

    # Get current consultation request
    request = get_consultation_request(project_path)
    if not request:
        raise HTTPException(status_code=404, detail="No pending consultation request")

    # Update human_review.md (使用北京时间)
    human_review = project_path / "human_review.md"
    timestamp = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S")

    consult_section = f"""
## CONSULT {request.gate} — {request.title}

STATUS: READY
提交时间: {timestamp}

{answer.answer.strip()}

---
"""

    if human_review.exists():
        content = human_review.read_text()
        # Check if section already exists
        if f"CONSULT {request.gate}" in content:
            # Update existing section
            pattern = rf'## CONSULT {re.escape(request.gate)}.*?(?=\n##|\Z)'
            content = re.sub(pattern, consult_section.strip(), content, flags=re.DOTALL)
        else:
            # Append new section
            content += "\n" + consult_section
        human_review.write_text(content)
    else:
        human_review.write_text(f"# 人工审核与介入记录\n{consult_section}")

    # Broadcast update
    await manager.broadcast({
        "type": "consultation_answered",
        "project": base_name,
        "gate": request.gate
    })

    return {"status": "ok", "message": "Consultation answer submitted"}

@app.post("/api/projects/{base_name}/action")
async def project_action(
    base_name: str,
    action: ProjectAction,
    current_user: UserInfo = Depends(get_current_user)
):
    """Execute project action (resume/pause/kill)"""
    try:
        env = os.environ.copy()
        env["PATH"] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
        cmd = ["/usr/bin/bash", str(LAUNCH_SCRIPT), action.action, base_name]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10, env=env)

        await manager.broadcast({
            "type": "project_action",
            "project": base_name,
            "action": action.action
        })

        return {
            "status": "ok",
            "action": action.action,
            "output": result.stdout
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/projects/{base_name}/files")
async def get_files(
    base_name: str,
    current_user: UserInfo = Depends(get_current_user)
):
    """List curated, grouped artifacts for a project."""
    project = _resolve_project(base_name)
    return {"files": list_artifacts(project)}


@app.get("/api/projects/{base_name}/file")
async def get_file_content(
    base_name: str,
    path: str,
    current_user: UserInfo = Depends(get_current_user)
):
    """Return text content of a single artifact (size-capped)."""
    project = _resolve_project(base_name)
    target = _safe_path(project, path)
    if target.suffix.lower() not in TEXT_EXTS:
        raise HTTPException(status_code=415, detail="Not a previewable text file")
    if target.stat().st_size > MAX_TEXT_BYTES:
        raise HTTPException(status_code=413, detail="File too large to preview")
    return {
        "path": path,
        "type": _file_type(target),
        "content": target.read_text(errors="replace"),
    }


@app.get("/api/projects/{base_name}/raw")
async def get_raw_file(
    base_name: str,
    path: str,
    current_user: UserInfo = Depends(get_current_user)
):
    """Serve a binary artifact (image / pdf) inline."""
    project = _resolve_project(base_name)
    target = _safe_path(project, path)
    if target.suffix.lower() not in (IMAGE_EXTS | PDF_EXTS | TEXT_EXTS):
        raise HTTPException(status_code=415, detail="Unsupported file type")
    return FileResponse(str(target))


@app.get("/api/projects/{base_name}/paper")
async def get_paper(
    base_name: str,
    download: bool = False,
    current_user: UserInfo = Depends(get_current_user)
):
    """Serve the produced paper PDF (inline by default)."""
    project = _resolve_project(base_name)
    paper = _find_paper(project, base_name)
    if not paper:
        raise HTTPException(status_code=404, detail="Paper PDF not found")
    return FileResponse(
        str(paper),
        media_type="application/pdf",
        filename=paper.name if download else None,
    )


@app.get("/api/projects/{base_name}/steps")
async def get_project_steps(
    base_name: str,
    current_user: UserInfo = Depends(get_current_user)
):
    """Per-step status + Gate-2 verdict + open-issue count."""
    project = _resolve_project(base_name)
    return get_steps(project, base_name)


# ============================================================================
# Model registry + per-step model assignment
# ============================================================================
# Seeded on first read if web/model_registry.json is absent. The three agentic
# CLI/SDK backends (claude/codex/agy) reflect the pipeline's built-in defaults;
# the API presets (DeepSeek/Gemini/Qwen) cover the "judge model" use case.
DEFAULT_MODEL_REGISTRY = [
    {"id": "claude", "label": "Claude (默认 CLI)", "backend": "claude",
     "model": "", "effort": "max", "enabled": True, "builtin": True},
    {"id": "codex-gpt55", "label": "Codex · GPT-5.5 (xhigh)", "backend": "codex",
     "model": "gpt-5.5", "effort": "xhigh", "enabled": True, "builtin": True},
    {"id": "agy-gemini", "label": "Antigravity · Gemini 3.1 Pro", "backend": "agy",
     "model": "gemini-3.1-pro-preview", "enabled": True, "builtin": True},
    {"id": "deepseek-chat", "label": "DeepSeek Chat (评委/API)", "backend": "openai",
     "model": "deepseek-chat", "base_url": "https://api.deepseek.com",
     "key_env": "DEEPSEEK_API_KEY", "enabled": True, "builtin": False},
    {"id": "deepseek-reasoner", "label": "DeepSeek Reasoner (评委/API)",
     "backend": "openai", "model": "deepseek-reasoner",
     "base_url": "https://api.deepseek.com", "key_env": "DEEPSEEK_API_KEY",
     "enabled": True, "builtin": False},
    {"id": "gemini-api", "label": "Gemini 2.5 Pro (评委/API)", "backend": "gemini",
     "model": "gemini-2.5-pro", "key_env": "GEMINI_API_KEY",
     "enabled": True, "builtin": False},
    {"id": "qwen-max", "label": "Qwen Max (评委/API · 需 DASHSCOPE_API_KEY)",
     "backend": "openai", "model": "qwen-max",
     "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
     "key_env": "DASHSCOPE_API_KEY", "enabled": False, "builtin": False},
]


def _read_json(path: Path, default):
    try:
        if path.is_file():
            return json.loads(path.read_text())
    except Exception as e:
        print(f"model config read error ({path.name}): {e}")
    return default


def _write_json_atomic(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    tmp.replace(path)


def load_model_registry() -> List[dict]:
    """Return the model list, seeding defaults to disk on first access."""
    data = _read_json(MODEL_REGISTRY_FILE, None)
    if not isinstance(data, dict) or "models" not in data:
        _write_json_atomic(MODEL_REGISTRY_FILE, {"models": DEFAULT_MODEL_REGISTRY})
        return DEFAULT_MODEL_REGISTRY
    return data["models"]


def load_model_config() -> dict:
    data = _read_json(MODEL_CONFIG_FILE, {})
    return data if isinstance(data, dict) else {}


@app.get("/api/models")
async def get_models(current_user: UserInfo = Depends(get_current_user)):
    """Full model registry + per-step assignment config (one call for the UI)."""
    return {
        "registry": load_model_registry(),
        "config": load_model_config(),
        "agentic_backends": sorted(AGENTIC_BACKENDS),
        "valid_backends": sorted(VALID_BACKENDS),
    }


@app.put("/api/models/registry")
async def put_model_registry(
    payload: ModelRegistryPayload,
    current_user: UserInfo = Depends(get_current_user),
):
    """Replace the model registry (add/edit/enable/delete handled client-side)."""
    seen = set()
    for m in payload.models:
        mid = m.id.strip()
        if not mid or not re.fullmatch(r"[a-zA-Z0-9_.-]+", mid):
            raise HTTPException(status_code=400, detail=f"非法模型 id：{m.id!r}（仅限字母数字 . _ -）")
        if mid in seen:
            raise HTTPException(status_code=400, detail=f"模型 id 重复：{mid}")
        seen.add(mid)
        if m.backend not in VALID_BACKENDS:
            raise HTTPException(status_code=400, detail=f"未知后端 {m.backend!r}（可选：{', '.join(sorted(VALID_BACKENDS))}）")
        if m.backend not in ("claude",) and not m.model.strip():
            raise HTTPException(status_code=400, detail=f"模型 {mid} 缺少 model 名称")
    _write_json_atomic(MODEL_REGISTRY_FILE, {"models": [m.dict() for m in payload.models]})
    await manager.broadcast({"type": "models_updated", "what": "registry"})
    return {"status": "ok", "count": len(payload.models)}


@app.put("/api/models/config")
async def put_model_config(
    payload: ModelConfigPayload,
    current_user: UserInfo = Depends(get_current_user),
):
    """Set the per-step assignment for one scope (a project base_name or "_default").

    Empty assignments are pruned so "no override" stays the byte-identical
    default path in run_paper.sh.
    """
    scope = payload.scope.strip()
    if not scope:
        raise HTTPException(status_code=400, detail="scope 不能为空")

    cfg = load_model_config()
    known_ids = {m["id"] for m in load_model_registry()}
    cleaned: Dict[str, dict] = {}
    for step_key, asg in payload.steps.items():
        if not _valid_model_step_key(step_key):
            raise HTTPException(status_code=400, detail=f"非法步骤键：{step_key}")
        primary = asg.primary.strip()
        fallback = asg.fallback.strip()
        if primary and primary not in known_ids:
            raise HTTPException(status_code=400, detail=f"未知模型 id：{primary}")
        if fallback and fallback not in known_ids:
            raise HTTPException(status_code=400, detail=f"未知模型 id：{fallback}")
        if not primary:
            continue  # no primary => no override for this step
        entry = {"primary": primary}
        if fallback:
            entry["fallback"] = fallback
        cleaned[step_key] = entry

    if cleaned:
        cfg[scope] = cleaned
    else:
        cfg.pop(scope, None)  # all cleared => remove the scope entirely
    _write_json_atomic(MODEL_CONFIG_FILE, cfg)
    await manager.broadcast({"type": "models_updated", "what": "config", "scope": scope})
    return {"status": "ok", "scope": scope, "steps": len(cleaned)}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket for real-time updates"""
    await manager.connect(websocket)
    try:
        while True:
            # Send periodic updates
            await asyncio.sleep(2)
            projects = list_all_projects()
            await websocket.send_json({
                "type": "status_update",
                "projects": [p.dict() for p in projects]
            })
    except WebSocketDisconnect:
        manager.disconnect(websocket)


# ============================================================================
# Cloud Run Acceleration API
# ============================================================================

class CloudStatus(BaseModel):
    available: bool
    region: Optional[str] = None
    project_id: Optional[str] = None
    service_name: Optional[str] = None
    max_instances: Optional[int] = None
    solvers: List[str] = []
    error: Optional[str] = None


@app.get("/api/cloud/status")
async def cloud_status(creds: HTTPAuthorizationCredentials = Depends(security)):
    """Check GCP Cloud Run solver service availability"""
    verify_token(creds.credentials)

    try:
        # Check if gcloud is installed
        result = subprocess.run(
            ["gcloud", "version"],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode != 0:
            return CloudStatus(available=False, error="gcloud CLI not installed")

        # Read GCP configuration
        project_id = os.getenv("GCP_PROJECT_ID", "level-night-476302-k0")
        region = os.getenv("GCP_REGION", "europe-west4")
        service_name = os.getenv("GCP_SOLVER_SERVICE", "solver-api")

        # Check if service exists
        result = subprocess.run(
            [
                "gcloud", "run", "services", "describe", service_name,
                f"--region={region}",
                f"--project={project_id}",
                "--format=json"
            ],
            capture_output=True,
            text=True,
            timeout=10
        )

        if result.returncode != 0:
            return CloudStatus(
                available=False,
                region=region,
                project_id=project_id,
                error=f"Service {service_name} not found"
            )

        # Parse service info
        service_info = json.loads(result.stdout)
        max_instances = 10  # Default, can parse from service_info if needed

        return CloudStatus(
            available=True,
            region=region,
            project_id=project_id,
            service_name=service_name,
            max_instances=max_instances,
            solvers=["python", "julia", "matlab", "R"]
        )

    except subprocess.TimeoutExpired:
        return CloudStatus(available=False, error="gcloud command timeout")
    except Exception as e:
        return CloudStatus(available=False, error=str(e))


@app.get("/api/cloud/config")
async def cloud_config(creds: HTTPAuthorizationCredentials = Depends(security)):
    """Get current cloud solver configuration"""
    verify_token(creds.credentials)

    return {
        "use_cloud": os.getenv("USE_CLOUD_SOLVER", "false"),
        "threshold_time": int(os.getenv("CLOUD_THRESHOLD_TIME", "300")),
        "solver_types": os.getenv("CLOUD_SOLVER_TYPES", "python,julia,matlab,R").split(","),
        "project_id": os.getenv("GCP_PROJECT_ID", ""),
        "region": os.getenv("GCP_REGION", "europe-west4"),
        "service_name": os.getenv("GCP_SOLVER_SERVICE", "solver-api"),
    }


@app.post("/api/projects/{base_name}/cloud/enable")
async def enable_cloud_solver(
    base_name: str,
    creds: HTTPAuthorizationCredentials = Depends(security)
):
    """Enable cloud solver for a specific project"""
    verify_token(creds.credentials)
    project_path = _resolve_project(base_name)

    # Create project-specific .env file or update existing
    env_file = project_path / ".env.cloud"
    env_file.write_text(f"""# Cloud Run solver configuration
USE_CLOUD_SOLVER=true
CLOUD_THRESHOLD_TIME=300
CLOUD_SOLVER_TYPES=python,julia,matlab,R
GCP_PROJECT_ID={os.getenv('GCP_PROJECT_ID', 'level-night-476302-k0')}
GCP_REGION={os.getenv('GCP_REGION', 'europe-west4')}
GCP_SOLVER_SERVICE={os.getenv('GCP_SOLVER_SERVICE', 'solver-api')}
""")

    return {"status": "enabled", "base_name": base_name}


@app.post("/api/projects/{base_name}/cloud/disable")
async def disable_cloud_solver(
    base_name: str,
    creds: HTTPAuthorizationCredentials = Depends(security)
):
    """Disable cloud solver for a specific project"""
    verify_token(creds.credentials)
    project_path = _resolve_project(base_name)

    env_file = project_path / ".env.cloud"
    if env_file.exists():
        env_file.unlink()

    return {"status": "disabled", "base_name": base_name}


# ============================================================================
# Main
# ============================================================================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
