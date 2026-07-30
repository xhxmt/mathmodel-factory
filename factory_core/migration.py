from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from .domain import MigrationConflict, WorkflowState, WorkflowStatus
from .storage import SQLiteStateStore


_CHECKPOINT_RE = re.compile(r"Last completed step\*{0,2}\s*[:：]\s*(-?\d+)")
_STATE_FILES = (
    "checkpoint.md",
    ".heartbeat",
    ".runner.pid",
    ".runner.lock.info",
    ".paused",
    ".killed",
    ".awaiting_consultation",
    ".review_state.json",
    ".state.json",
    "diagnostics/status.json",
    "selection/step3_options.json",
    "selection/step3_decision.json",
)


def _sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _pid_is_live(project: Path) -> bool:
    path = project / ".runner.pid"
    if not path.is_file():
        return False
    try:
        os.kill(int(path.read_text(encoding="utf-8").strip()), 0)
        return True
    except (OSError, ValueError):
        return False


@dataclass(frozen=True)
class MigrationReport:
    project_dir: str
    project_id: str
    project_type: str
    inferred_step: int
    checkpoint_step: int | None
    storage_scope: str
    active_runner: bool
    legacy_status: str
    pending_action: dict | None
    fingerprints: dict[str, str | None]
    conflicts: tuple[str, ...] = field(default_factory=tuple)
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def digest(self) -> str:
        raw = json.dumps(asdict(self), ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def to_json(self) -> str:
        payload = asdict(self)
        payload["digest"] = self.digest
        return json.dumps(payload, ensure_ascii=True, sort_keys=True, indent=2)

    @classmethod
    def from_json(cls, text: str) -> "MigrationReport":
        payload = json.loads(text)
        digest = payload.pop("digest", None)
        payload["conflicts"] = tuple(payload.get("conflicts", ()))
        payload["warnings"] = tuple(payload.get("warnings", ()))
        report = cls(**payload)
        if digest != report.digest:
            raise MigrationConflict("migration report digest does not match its contents")
        return report


class LegacyInspector:
    def __init__(self, *, infer_step: Callable[[Path], int]):
        self._infer_step = infer_step

    def inspect(self, project_dir: str | Path) -> MigrationReport:
        project = Path(project_dir).resolve()
        if not project.is_dir():
            raise MigrationConflict(f"project does not exist: {project}")
        if (project / "problem").is_dir():
            project_type = "modeling"
        elif (project / "project_brief.md").is_file():
            project_type = "social_science"
        else:
            project_type = "unknown"
        inferred = int(self._infer_step(project))
        checkpoint = self._checkpoint_step(project)
        active = (
            _pid_is_live(project)
            or (project / ".runner.lock").exists()
            or (project / ".runner.lock.info").exists()
        )
        conflicts: list[str] = []
        warnings: list[str] = []
        if project_type == "social_science":
            conflicts.append("LEGACY_DOMAIN_RETIRED")
        elif project_type != "modeling":
            conflicts.append("UNKNOWN_PROJECT_TYPE")
        if active:
            conflicts.append("ACTIVE_RUNNER")
        if checkpoint is not None and checkpoint != inferred:
            mismatch = f"CHECKPOINT_MISMATCH:{checkpoint}!={inferred}"
            if project.parent.name == "complete":
                warnings.append(mismatch)
            else:
                conflicts.append(mismatch)
        if SQLiteStateStore(project).exists:
            conflicts.append("STATE_ALREADY_EXISTS")
        legacy_status, pending_action = self._legacy_status(project, inferred)
        fingerprints = {relative: _sha256(project / relative) for relative in _STATE_FILES}
        return MigrationReport(
            project_dir=str(project),
            project_id=project.name,
            project_type=project_type,
            inferred_step=inferred,
            checkpoint_step=checkpoint,
            storage_scope=project.parent.name if project.parent.name in {"ongoing", "complete"} else "external",
            active_runner=active,
            legacy_status=legacy_status.value,
            pending_action=pending_action,
            fingerprints=fingerprints,
            conflicts=tuple(conflicts),
            warnings=tuple(warnings),
        )

    @staticmethod
    def _checkpoint_step(project: Path) -> int | None:
        path = project / "checkpoint.md"
        if not path.is_file():
            return None
        match = _CHECKPOINT_RE.search(path.read_text(encoding="utf-8", errors="replace"))
        return int(match.group(1)) if match else None

    @staticmethod
    def _legacy_status(project: Path, inferred: int) -> tuple[WorkflowStatus, dict | None]:
        if (project / ".killed").is_file():
            return WorkflowStatus.KILLED, None
        if (project / ".paused").is_file():
            return WorkflowStatus.PAUSED, None
        options = project / "selection" / "step3_options.json"
        decision = project / "selection" / "step3_decision.json"
        if options.is_file() and not decision.is_file():
            deadline = None
            try:
                deadline = json.loads(options.read_text(encoding="utf-8")).get("deadline_epoch")
            except (json.JSONDecodeError, OSError, AttributeError):
                pass
            return WorkflowStatus.AWAITING_SELECTION, {
                "type": "step3_selection",
                "gate": "step3",
                "deadline_epoch": deadline,
                "metadata": {},
            }
        awaiting = project / ".awaiting_consultation"
        if awaiting.is_file():
            match = re.search(
                r"GATE:([^\s]+)", awaiting.read_text(encoding="utf-8", errors="replace")
            )
            return WorkflowStatus.AWAITING_CONSULTATION, {
                "type": "human_consultation",
                "gate": match.group(1) if match else None,
                "deadline_epoch": None,
                "metadata": {},
            }
        if inferred >= 16 or project.parent.name == "complete":
            return WorkflowStatus.COMPLETED, None
        return WorkflowStatus.READY, None


def apply_migration(
    project_dir: str | Path,
    report: MigrationReport,
    *,
    expected_digest: str,
    runtime_generation: str = "native_v2",
) -> WorkflowState:
    project = Path(project_dir).resolve()
    if expected_digest != report.digest:
        raise MigrationConflict("migration report approval digest does not match")
    if str(project) != report.project_dir:
        raise MigrationConflict("migration report belongs to a different project")
    if (
        _pid_is_live(project)
        or (project / ".runner.lock").exists()
        or (project / ".runner.lock.info").exists()
    ):
        raise MigrationConflict("project became active after inspection; inspect again")
    current_fingerprints = {relative: _sha256(project / relative) for relative in _STATE_FILES}
    if current_fingerprints != report.fingerprints:
        raise MigrationConflict("legacy state changed after inspection; inspect again")
    if report.conflicts:
        raise MigrationConflict("migration report has conflicts: " + ", ".join(report.conflicts))
    lock_dir = project / ".runner.lock"
    lock_info = project / ".runner.lock.info"
    try:
        lock_dir.mkdir()
    except FileExistsError as exc:
        raise MigrationConflict("project became active after inspection; inspect again") from exc
    try:
        lock_info.write_text(
            f"pid={os.getpid()}\nhost=migration\n",
            encoding="utf-8",
        )
        status = WorkflowStatus(report.legacy_status)
        return SQLiteStateStore(project).initialize(
            project_id=report.project_id,
            project_type=report.project_type,
            last_completed_step=report.inferred_step,
            status=status,
            active_step=(
                report.inferred_step + 1
                if status
                in {WorkflowStatus.AWAITING_SELECTION, WorkflowStatus.AWAITING_CONSULTATION}
                else None
            ),
            pending_action=report.pending_action,
            imported=True,
            import_payload={
                "report_digest": report.digest,
                "checkpoint_step": report.checkpoint_step,
                "inferred_step": report.inferred_step,
                "fingerprints": report.fingerprints,
                "warnings": report.warnings,
            },
            runtime_generation=runtime_generation,
        )
    finally:
        lock_info.unlink(missing_ok=True)
        try:
            lock_dir.rmdir()
        except OSError:
            pass
