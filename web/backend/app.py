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
from datetime import datetime
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
import subprocess
import re

# ============================================================================
# Configuration
# ============================================================================
FACTORY_ROOT = Path(__file__).parent.parent.parent.resolve()
ONGOING_DIR = FACTORY_ROOT / "ongoing"
COMPLETE_DIR = FACTORY_ROOT / "complete"
RUN_STATE_DIR = FACTORY_ROOT / "run_state"
LOGS_DIR = FACTORY_ROOT / "logs"
LAUNCH_SCRIPT = FACTORY_ROOT / "launch_agents.sh"

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

# CORS for local development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:3000", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ============================================================================
# Models
# ============================================================================
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

class ConsultationAnswer(BaseModel):
    answer: str

class ProjectAction(BaseModel):
    action: str  # resume, pause, kill

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
# Helper Functions
# ============================================================================
def read_checkpoint(project_path: Path) -> Dict:
    """Parse checkpoint.md to extract step info"""
    checkpoint_file = project_path / "checkpoint.md"
    if not checkpoint_file.exists():
        return {"step": -1, "status": "unknown"}

    content = checkpoint_file.read_text()
    step_match = re.search(r'Last completed step:\s*(\d+)', content)
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

    # Get last modification time
    last_updated = datetime.fromtimestamp(
        project_path.stat().st_mtime
    ).strftime("%Y-%m-%d %H:%M:%S")

    progress = (current_step + 1) / 16 * 100 if current_step >= 0 else 0

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

    # Extract main content (after metadata section)
    content_parts = content.split("## 需要你（借助 GPT Pro / Gemini Deep Think）决定的事")
    main_content = content_parts[1].split("## 回填方式")[0].strip() if len(content_parts) > 1 else ""

    return ConsultationRequest(
        gate=gate,
        step=int(step_match.group(1)) if step_match else 0,
        title=title_match.group(1).strip() if title_match else "Unknown",
        content=main_content,
        project=project_path.name,
        created=created_match.group(1).strip() if created_match else "Unknown"
    )

# ============================================================================
# API Endpoints
# ============================================================================
@app.get("/")
async def root():
    return {"status": "Paper Factory Dashboard API", "version": "1.0.0"}

@app.get("/api/projects", response_model=List[ProjectStatus])
async def get_projects():
    """Get list of all projects with their status"""
    return list_all_projects()

@app.get("/api/projects/{base_name}/status", response_model=ProjectStatus)
async def get_single_project_status(base_name: str):
    """Get detailed status for a specific project"""
    project_path = ONGOING_DIR / base_name
    if not project_path.exists():
        project_path = COMPLETE_DIR / base_name

    if not project_path.exists():
        raise HTTPException(status_code=404, detail=f"Project {base_name} not found")

    return get_project_status(project_path, base_name)

@app.get("/api/projects/{base_name}/checkpoint")
async def get_checkpoint(base_name: str):
    """Get checkpoint.md content"""
    project_path = ONGOING_DIR / base_name
    if not project_path.exists():
        project_path = COMPLETE_DIR / base_name

    checkpoint_file = project_path / "checkpoint.md"
    if not checkpoint_file.exists():
        raise HTTPException(status_code=404, detail="Checkpoint file not found")

    return {"content": checkpoint_file.read_text()}

@app.get("/api/projects/{base_name}/logs")
async def get_recent_logs(base_name: str, lines: int = 100):
    """Get recent log entries for a project"""
    project_path = ONGOING_DIR / base_name
    if not project_path.exists():
        project_path = COMPLETE_DIR / base_name

    logs_dir = project_path / "logs"
    if not logs_dir.exists():
        return {"logs": []}

    # Find most recent log file
    log_files = sorted(logs_dir.glob("step_*.log"), key=lambda f: f.stat().st_mtime, reverse=True)
    if not log_files:
        return {"logs": []}

    recent_log = log_files[0]
    try:
        result = subprocess.run(
            ["tail", "-n", str(lines), str(recent_log)],
            capture_output=True,
            text=True
        )
        return {
            "logs": result.stdout.split("\n"),
            "file": recent_log.name
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/projects/{base_name}/consultation")
async def get_consultation(base_name: str):
    """Get pending consultation request"""
    project_path = ONGOING_DIR / base_name
    if not project_path.exists():
        raise HTTPException(status_code=404, detail=f"Project {base_name} not found")

    request = get_consultation_request(project_path)
    if not request:
        raise HTTPException(status_code=404, detail="No pending consultation request")

    return request

@app.post("/api/projects/{base_name}/consultation/answer")
async def submit_consultation_answer(base_name: str, answer: ConsultationAnswer):
    """Submit human consultation answer"""
    project_path = ONGOING_DIR / base_name
    if not project_path.exists():
        raise HTTPException(status_code=404, detail=f"Project {base_name} not found")

    # Get current consultation request
    request = get_consultation_request(project_path)
    if not request:
        raise HTTPException(status_code=404, detail="No pending consultation request")

    # Update human_review.md
    human_review = project_path / "human_review.md"
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

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
async def project_action(base_name: str, action: ProjectAction):
    """Execute project action (resume/pause/kill)"""
    try:
        cmd = [str(LAUNCH_SCRIPT), action.action, base_name]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)

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

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
