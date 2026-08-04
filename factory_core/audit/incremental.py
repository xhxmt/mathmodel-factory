from __future__ import annotations

import fcntl
import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable

from ..adapters.infrastructure.commands import CommandRunner
from ..domain import ExecutionResult
from .domain import AuditOutcome, AuditProfile, AuditRecord, AuditSnapshot, AuditStatus
from .ledger import LedgerFinding, sync_incremental_findings
from .persistence import atomic_write_json, utc_now


INCOMPLETE_STATUSES = frozenset(
    {"RUNNING", "PARTIAL", "PENDING", "INCOMPLETE", "FAILED"}
)
PROFILE_DECISIONS = {
    AuditProfile.MODEL: "MODEL_READY",
    AuditProfile.RESULTS: "RESULTS_VERIFIED",
    AuditProfile.PAPER: "PAPER_TRACE_READY",
}


@dataclass(frozen=True)
class StageCheck:
    name: str
    passed: bool
    severity: str
    detail: str
    report: str = ""
    returncode: int = 0
    timed_out: bool = False

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class IncrementalAuditService:
    """Run cacheable pre-publication checks without granting delivery permission."""

    def __init__(
        self,
        factory_root: str | Path,
        *,
        runner: CommandRunner | None = None,
    ) -> None:
        self.factory_root = Path(factory_root).resolve()
        self.runner = runner or CommandRunner()

    def run_project(
        self,
        project: str | Path,
        profile: AuditProfile | str,
        *,
        checkpoint_step: int | None = None,
        reuse_pass: bool = True,
    ) -> AuditOutcome:
        resolved = Path(project).resolve()
        selected = (
            profile if isinstance(profile, AuditProfile) else AuditProfile(profile)
        )
        if selected is AuditProfile.FINAL:
            raise ValueError("the final profile is owned by FinalAuditService")
        step = checkpoint_step if checkpoint_step is not None else {
            AuditProfile.MODEL: 4,
            AuditProfile.RESULTS: 6,
            AuditProfile.PAPER: 10,
        }[selected]
        allowed_steps = {
            AuditProfile.MODEL: {4},
            AuditProfile.RESULTS: {5, 6},
            AuditProfile.PAPER: {10},
        }[selected]
        if step not in allowed_steps:
            allowed = ", ".join(str(value) for value in sorted(allowed_steps))
            raise ValueError(
                f"checkpoint step for {selected.value} must be one of: {allowed}"
            )
        lock_path = resolved / ".factory" / "audits" / ".lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+", encoding="ascii") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return self._busy(resolved, selected, step)
            try:
                return self._run_unlocked(
                    resolved,
                    selected,
                    checkpoint_step=step,
                    reuse_pass=reuse_pass,
                )
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _run_unlocked(
        self,
        project: Path,
        profile: AuditProfile,
        *,
        checkpoint_step: int,
        reuse_pass: bool,
    ) -> AuditOutcome:
        snapshot = self._snapshot(project, profile, checkpoint_step)
        if reuse_pass:
            cached = self._load_reusable(project, profile, snapshot)
            if cached is not None:
                return AuditOutcome(
                    ExecutionResult.succeeded(
                        audit_profile=profile.value,
                        audit_status=AuditStatus.PASS.value,
                        audit_snapshot=snapshot.snapshot_id,
                        audit_reused=True,
                        audit_result=str(
                            self._latest_path(project, profile).relative_to(project)
                        ),
                    ),
                    cached,
                    snapshot,
                )

        checks = self._checks(project, profile, checkpoint_step)
        hard_failures = [
            check
            for check in checks
            if not check.passed and check.severity == "hard"
        ]
        indeterminate = [check for check in hard_failures if check.timed_out]
        warnings = [
            check
            for check in checks
            if not check.passed and check.severity == "warning"
        ]
        if indeterminate:
            status = AuditStatus.INDETERMINATE
            decision = f"{profile.value.upper()}_AUDIT_INDETERMINATE"
            error_class = "TRANSIENT_INCREMENTAL_AUDIT"
            returncode = 75
        elif hard_failures:
            status = AuditStatus.FAIL
            decision = f"{profile.value.upper()}_AUDIT_FAILED"
            error_class = "AUDIT_REPAIR_REQUIRED"
            returncode = 1
        else:
            status = AuditStatus.PASS
            decision = PROFILE_DECISIONS[profile]
            error_class = ""
            returncode = 0

        evidence = {
            "checkpoint_step": checkpoint_step,
            "checks": [check.to_dict() for check in checks],
            "hard_failures": [check.name for check in hard_failures],
            "warnings": [check.name for check in warnings],
            "repair_step": self._repair_step(
                profile, checkpoint_step, hard_failures
            ),
        }
        record = AuditRecord(
            snapshot_id=snapshot.snapshot_id,
            base=project.name,
            profile=profile.value,
            status=status,
            decision=decision,
            judge_completed=False,
            delivery_allowed=False,
            created_at=utc_now(),
            error_class=error_class,
            returncode=returncode,
            evidence=evidence,
        )
        self._sync_ledger(project, profile, checkpoint_step, checks)
        self._persist(project, profile, snapshot, record)
        metadata = {
            "audit_profile": profile.value,
            "audit_status": status.value,
            "audit_snapshot": snapshot.snapshot_id,
            "audit_result": str(
                self._latest_path(project, profile).relative_to(project)
            ),
            "audit_reused": False,
            "failed_check": hard_failures[0].name if hard_failures else "",
            "repair_step": evidence["repair_step"],
        }
        execution = (
            ExecutionResult.succeeded(**metadata)
            if status is AuditStatus.PASS
            else ExecutionResult.failed(
                error_class, returncode=returncode, **metadata
            )
        )
        return AuditOutcome(execution, record, snapshot)

    def _checks(
        self, project: Path, profile: AuditProfile, checkpoint_step: int
    ) -> list[StageCheck]:
        if profile is AuditProfile.MODEL:
            return self._model_checks(project)
        if profile is AuditProfile.RESULTS:
            return self._results_checks(project, checkpoint_step)
        if profile is AuditProfile.PAPER:
            return self._paper_checks(project)
        raise ValueError(f"unsupported incremental audit profile: {profile.value}")

    def _model_checks(self, project: Path) -> list[StageCheck]:
        required = {
            "model.md": 100,
            "symbol_table.md": 10,
            "assumption_ledger.md": 10,
            "modeling_scope_gate.md": 1,
            "claim_registry.json": 1,
            "quality_contract.json": 1,
        }
        missing = [
            relative
            for relative, minimum in required.items()
            if not self._has_lines(project / relative, minimum)
        ]
        checks = [
            StageCheck(
                "model_artifacts",
                not missing,
                "hard",
                "required model artifacts are present"
                if not missing
                else "missing or short: " + ", ".join(missing),
            )
        ]
        verdict = self._verdict(project / "modeling_scope_gate.md")
        checks.append(
            StageCheck(
                "model_scope",
                verdict == "PASS",
                "hard",
                f"verdict={verdict or 'MISSING'}",
            )
        )
        try:
            from scripts.quality_contract import load_contract

            contract = load_contract(project / "quality_contract.json")
            valid = contract.get("version") == 3
            detail = (
                "quality contract v3 schema is valid"
                if valid
                else f"quality contract version={contract.get('version')} (expected 3)"
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            detail = f"quality contract invalid: {exc}"
            valid = False
        checks.append(
            StageCheck("quality_contract_schema", valid, "hard", detail)
        )
        try:
            from scripts.claim_graph import load_declared_registry

            registry = load_declared_registry(project)
            registry_valid = registry is not None
            registry_detail = "claim-registry-v1 schema is valid"
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            registry_valid = False
            registry_detail = f"claim registry invalid: {exc}"
        checks.append(
            StageCheck(
                "claim_registry_schema",
                registry_valid,
                "hard",
                registry_detail,
            )
        )
        return checks

    def _results_checks(
        self, project: Path, checkpoint_step: int
    ) -> list[StageCheck]:
        checks = [self._canonical_results_check(project)]
        values = (
            list((project / "results").rglob("values.json"))
            if (project / "results").is_dir()
            else []
        )
        checks.append(
            StageCheck(
                "solve_artifacts",
                self._has_lines(project / "solve_log.md", 20)
                and bool(values)
                and (project / "results/invariants.json").is_file()
                and not self._has_stub(project),
                "hard",
                f"values_files={len(values)} "
                f"invariants={'yes' if (project / 'results/invariants.json').is_file() else 'no'} "
                f"stubs={'yes' if self._has_stub(project) else 'no'}",
            )
        )
        if checkpoint_step >= 6:
            figures = []
            if (project / "figures").is_dir():
                figures = [
                    path
                    for path in (project / "figures").iterdir()
                    if path.name.startswith("sensitivity_")
                    and path.suffix.lower() in {".pdf", ".png"}
                ]
            checks.append(
                StageCheck(
                    "sensitivity_artifacts",
                    self._has_lines(project / "sensitivity_report.md", 20)
                    and bool(figures),
                    "hard",
                    f"sensitivity_figures={len(figures)}",
                )
            )
        checks.extend(
            self._command_checks(
                project,
                AuditProfile.RESULTS,
                (
                    (
                        "provenance",
                        "scripts/verify_provenance.py",
                        [project],
                        project / "provenance_verification.latest.txt",
                        "hard",
                    ),
                    (
                        "invariants",
                        "scripts/verify_invariants.py",
                        [project],
                        project / "invariants_verification.latest.txt",
                        "hard",
                    ),
                    (
                        "spec_impl",
                        "scripts/verify_spec_impl.py",
                        [project],
                        project / "spec_impl_verification.latest.txt",
                        "hard",
                    ),
                    (
                        "quality_contract",
                        "scripts/verify_quality_contract.py",
                        [
                            project,
                            "--factory-root",
                            self.factory_root,
                            "--json-out",
                            project
                            / "quality_contract_verification.latest.json",
                            "--text-out",
                            project
                            / "quality_contract_verification.latest.txt",
                        ],
                        project / "logs" / "native_results_quality_contract.log",
                        "hard",
                    ),
                ),
            )
        )
        return checks

    def _paper_checks(self, project: Path) -> list[StageCheck]:
        paper = project / f"{project.name}_paper.tex"
        if not paper.is_file():
            paper = project / "paper" / "paper.tex"
        paper_name = str(paper.relative_to(project)) if paper.is_file() else "MISSING"
        code_review = self._has_lines(project / "code_review.md", 20)
        checks = [
            StageCheck(
                "paper_artifacts",
                paper.is_file() and code_review,
                "hard",
                f"paper={paper_name} code_review={'yes' if code_review else 'no'}",
            )
        ]
        checks.extend(
            self._command_checks(
                project,
                AuditProfile.PAPER,
                (
                    (
                        "numbers",
                        "scripts/verify_numbers.py",
                        ["--verify", project, project.name],
                        project / "number_verification.latest.stdout",
                        "hard",
                    ),
                    (
                        "symbols",
                        "scripts/verify_symbols.py",
                        [project, project.name],
                        project / "symbol_verification.latest.txt",
                        "warning",
                    ),
                    (
                        "deliverables",
                        "scripts/verify_deliverables.py",
                        [project, project.name],
                        project / "deliverables_verification.latest.txt",
                        "hard",
                    ),
                    (
                        "invariants",
                        "scripts/verify_invariants.py",
                        [project],
                        project / "invariants_verification.latest.txt",
                        "hard",
                    ),
                    (
                        "spec_impl",
                        "scripts/verify_spec_impl.py",
                        [project],
                        project / "spec_impl_verification.latest.txt",
                        "hard",
                    ),
                    (
                        "quality_contract",
                        "scripts/verify_quality_contract.py",
                        [
                            project,
                            "--factory-root",
                            self.factory_root,
                            "--json-out",
                            project
                            / "quality_contract_verification.latest.json",
                            "--text-out",
                            project
                            / "quality_contract_verification.latest.txt",
                        ],
                        project / "logs" / "native_paper_quality_contract.log",
                        "hard",
                    ),
                ),
            )
        )
        return checks

    def _command_checks(
        self,
        project: Path,
        profile: AuditProfile,
        definitions: Iterable[
            tuple[str, str, list[str | Path], Path, str]
        ],
    ) -> list[StageCheck]:
        checks: list[StageCheck] = []
        for name, script, args, report, severity in definitions:
            try:
                result = self.runner.python(
                    self.factory_root,
                    project,
                    script,
                    args,
                    label=f"audit_{profile.value}_{name}",
                    timeout_seconds=600,
                    accepted=(0,),
                    log_path=report,
                )
                checks.append(
                    StageCheck(
                        name,
                        result.accepted,
                        severity,
                        "PASS"
                        if result.accepted
                        else f"exit={result.returncode}",
                        str(report.relative_to(project)),
                        result.returncode,
                        result.timed_out,
                    )
                )
            except OSError as exc:
                checks.append(
                    StageCheck(
                        name,
                        False,
                        severity,
                        f"command error: {exc}",
                        str(report.relative_to(project)),
                        1,
                        True,
                    )
                )
        return checks

    def _canonical_results_check(self, project: Path) -> StageCheck:
        path = project / "results" / "canonical_results.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return StageCheck(
                "canonical_results", False, "hard", f"unreadable: {exc}"
            )
        if not isinstance(payload, dict):
            return StageCheck(
                "canonical_results", False, "hard", "root must be an object"
            )
        root_method = payload.get("primary_method")
        project_id = payload.get("project")
        contract_errors: list[str] = []
        if not isinstance(project_id, str) or not project_id.strip():
            contract_errors.append("missing project")
        if not isinstance(root_method, str) or not root_method.strip():
            contract_errors.append("missing primary_method")
        subproblems = [
            (key, value)
            for key, value in payload.items()
            if re.fullmatch(r"(?:p|P|problem)\d+", str(key))
            and isinstance(value, dict)
        ]
        if not subproblems:
            contract_errors.append("missing pN/problemN entries")
        for key, item in subproblems:
            source = item.get("source_file") or item.get("source")
            if not isinstance(source, str) or not source.strip():
                contract_errors.append(f"{key} missing source/source_file")
                continue
            candidate = Path(source)
            try:
                resolved = (project / candidate).resolve(strict=True)
                resolved.relative_to(project)
            except (OSError, RuntimeError, ValueError):
                contract_errors.append(f"{key} invalid source={source}")
            item_method = item.get("primary_method")
            if root_method and item_method and str(item_method) != str(root_method):
                contract_errors.append(
                    f"{key} primary_method={item_method} != {root_method}"
                )

        incomplete: list[str] = []

        def walk(value: object, location: str) -> None:
            if isinstance(value, dict):
                for key, child in value.items():
                    child_location = (
                        f"{location}.{key}" if location else str(key)
                    )
                    if (
                        str(key).lower() == "status"
                        and str(child).upper() in INCOMPLETE_STATUSES
                    ):
                        incomplete.append(f"{child_location}={child}")
                    walk(child, child_location)
            elif isinstance(value, list):
                for index, child in enumerate(value):
                    walk(child, f"{location}[{index}]")

        walk(payload, "")
        numeric = self._numeric_leaf_count(payload)
        passed = numeric > 0 and not incomplete and not contract_errors
        detail = f"numeric_leaves={numeric}"
        if contract_errors:
            detail += " contract=" + ", ".join(contract_errors[:8])
        if incomplete:
            detail += " incomplete=" + ", ".join(incomplete[:8])
        return StageCheck("canonical_results", passed, "hard", detail)

    @staticmethod
    def _numeric_leaf_count(value: object) -> int:
        if isinstance(value, bool):
            return 0
        if isinstance(value, (int, float)):
            return 1
        if isinstance(value, dict):
            return sum(
                IncrementalAuditService._numeric_leaf_count(child)
                for child in value.values()
            )
        if isinstance(value, list):
            return sum(
                IncrementalAuditService._numeric_leaf_count(child)
                for child in value
            )
        return 0

    def _snapshot(
        self, project: Path, profile: AuditProfile, checkpoint_step: int
    ) -> AuditSnapshot:
        files = self._profile_files(project, profile)
        checker_files = self._checker_files(profile)
        identity: dict[str, object] = {
            "base": project.name,
            "profile": profile.value,
            "checkpoint_step": checkpoint_step,
            "inputs": {
                relative: self._sha256(project / relative) for relative in files
            },
            "checker_contract": {
                relative: self._sha256(self.factory_root / relative)
                for relative in checker_files
            },
        }
        encoded = json.dumps(
            identity,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        snapshot_id = hashlib.sha256(encoded).hexdigest()
        return AuditSnapshot(
            snapshot_id=snapshot_id,
            base=project.name,
            profile=profile.value,
            created_at=utc_now(),
            identity=identity,
        )

    def _profile_files(
        self, project: Path, profile: AuditProfile
    ) -> list[str]:
        exact = {
            "chosen_method.md",
            "model.md",
            "symbol_table.md",
            "assumption_ledger.md",
            "modeling_scope_gate.md",
            "claim_registry.json",
            "quality_contract.json",
            "problem/problem_brief.md",
            "problem/deliverables.json",
        }
        roots: set[str] = {"models"}
        if profile in {AuditProfile.RESULTS, AuditProfile.PAPER}:
            exact.update({"solve_log.md", "sensitivity_report.md"})
            roots.update({"models", "results", "run_state/solver_jobs"})
        if profile is AuditProfile.PAPER:
            exact.update(
                {
                    f"{project.name}_paper.tex",
                    "paper/paper.tex",
                    "code_review.md",
                    "numbers_manifest.json",
                }
            )
            roots.add("tables")
            exact.update(
                path.relative_to(project).as_posix()
                for path in project.glob("result*.xlsx")
                if path.is_file()
            )
            try:
                deliverables = json.loads(
                    (project / "problem/deliverables.json").read_text(
                        encoding="utf-8"
                    )
                )
            except (OSError, json.JSONDecodeError):
                deliverables = {}
            if isinstance(deliverables, dict):
                for item in deliverables.get("attachments", []):
                    value = item.get("file") if isinstance(item, dict) else item
                    if not isinstance(value, str) or not value.strip():
                        continue
                    candidate = Path(value)
                    if candidate.is_absolute() or ".." in candidate.parts:
                        continue
                    exact.add(candidate.as_posix())
                    exact.add((Path("results") / candidate).as_posix())
        selected = {
            relative for relative in exact if (project / relative).is_file()
        }
        for root_name in roots:
            root = project / root_name
            if not root.is_dir():
                continue
            for path in root.rglob("*"):
                if (
                    not path.is_file()
                    or "__pycache__" in path.parts
                    or path.suffix == ".pyc"
                ):
                    continue
                selected.add(path.relative_to(project).as_posix())
        return sorted(selected)

    def _checker_files(self, profile: AuditProfile) -> list[str]:
        common = [
            "factory_core/audit/domain.py",
            "factory_core/audit/incremental.py",
            "factory_core/audit/ledger.py",
        ]
        if profile is AuditProfile.MODEL:
            selected = [
                *common,
                "scripts/claim_graph.py",
                "scripts/quality_contract.py",
            ]
        elif profile is AuditProfile.RESULTS:
            selected = [
                *common,
                "scripts/verify_provenance.py",
                "scripts/verify_invariants.py",
                "scripts/verify_spec_impl.py",
                "scripts/verify_quality_contract.py",
                "scripts/quality_contract.py",
            ]
        else:
            selected = [
                *common,
                "scripts/verify_numbers.py",
                "scripts/verify_symbols.py",
                "scripts/verify_deliverables.py",
                "scripts/verify_invariants.py",
                "scripts/verify_spec_impl.py",
                "scripts/verify_quality_contract.py",
                "scripts/quality_contract.py",
            ]
        oracle_root = self.factory_root / "scripts" / "domain_oracles"
        if oracle_root.is_dir():
            selected.extend(
                path.relative_to(self.factory_root).as_posix()
                for path in oracle_root.rglob("*")
                if path.is_file()
                and "__pycache__" not in path.parts
                and path.suffix != ".pyc"
            )
        return sorted(set(selected))

    @staticmethod
    def _sha256(path: Path) -> str:
        try:
            return hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            return "MISSING"

    def _persist(
        self,
        project: Path,
        profile: AuditProfile,
        snapshot: AuditSnapshot,
        record: AuditRecord,
    ) -> None:
        audit_dir = self._profile_root(project, profile) / snapshot.snapshot_id
        snapshot_path = audit_dir / "snapshot.json"
        if snapshot_path.is_file():
            existing = json.loads(snapshot_path.read_text(encoding="utf-8"))
            comparable = snapshot.to_dict()
            comparable["created_at"] = existing.get(
                "created_at", comparable["created_at"]
            )
            if existing != comparable:
                raise ValueError(
                    f"audit snapshot collision for {snapshot.snapshot_id}"
                )
        else:
            atomic_write_json(snapshot_path, snapshot.to_dict())
        attempt = (
            audit_dir
            / "attempts"
            / f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%S.%fZ')}.json"
        )
        atomic_write_json(attempt, record.to_dict())
        atomic_write_json(audit_dir / "latest.json", record.to_dict())
        atomic_write_json(self._latest_path(project, profile), record.to_dict())

    def _load_reusable(
        self, project: Path, profile: AuditProfile, snapshot: AuditSnapshot
    ) -> AuditRecord | None:
        path = (
            self._profile_root(project, profile)
            / snapshot.snapshot_id
            / "latest.json"
        )
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if (
            value.get("snapshot_id") != snapshot.snapshot_id
            or value.get("status") != "PASS"
            or value.get("profile") != profile.value
            or value.get("delivery_allowed") is not False
            or value.get("judge_completed") is not False
        ):
            return None
        return AuditRecord(
            snapshot_id=snapshot.snapshot_id,
            base=project.name,
            profile=profile.value,
            status=AuditStatus.PASS,
            decision=str(value.get("decision") or PROFILE_DECISIONS[profile]),
            judge_completed=False,
            delivery_allowed=False,
            created_at=str(value.get("created_at") or utc_now()),
            reused=True,
            evidence=dict(value.get("evidence") or {}),
        )

    def _sync_ledger(
        self,
        project: Path,
        profile: AuditProfile,
        checkpoint_step: int,
        checks: list[StageCheck],
    ) -> None:
        blocking_until = {
            AuditProfile.MODEL: "Step 5",
            AuditProfile.RESULTS: "Step 9",
            AuditProfile.PAPER: "Step 16",
        }[profile]
        findings = [
            LedgerFinding(
                issue_id=(
                    f"AUDIT-{profile.value.upper()}-"
                    f"{check.name.upper().replace('_', '-')}"
                ),
                raised_in=f"audit:{profile.value}:step{checkpoint_step}",
                category=profile.value,
                severity=(
                    "BLOCKING" if check.severity == "hard" else "MAJOR"
                ),
                blocking_until=blocking_until,
                status="RESOLVED" if check.passed else "OPEN",
                notes=(
                    check.detail
                    + (f"; report={check.report}" if check.report else "")
                ),
            )
            for check in checks
        ]
        sync_incremental_findings(
            project / "audit_issue_ledger.md", findings
        )

    @staticmethod
    def _repair_step(
        profile: AuditProfile,
        checkpoint_step: int,
        failures: list[StageCheck],
    ) -> int:
        if profile is AuditProfile.MODEL:
            return 4
        if profile is AuditProfile.RESULTS:
            if failures and all(
                check.name == "sensitivity_artifacts" for check in failures
            ):
                return 6
            return 5
        return (
            9
            if any(
                check.name in {"numbers", "paper_artifacts"}
                for check in failures
            )
            else 10
        )

    def _busy(
        self, project: Path, profile: AuditProfile, checkpoint_step: int
    ) -> AuditOutcome:
        snapshot_id = hashlib.sha256(
            f"audit-busy:{profile.value}:{project}".encode("utf-8")
        ).hexdigest()
        snapshot = AuditSnapshot(
            snapshot_id=snapshot_id,
            base=project.name,
            profile=profile.value,
            created_at=utc_now(),
            identity={
                "state": "AUDIT_BUSY",
                "profile": profile.value,
                "checkpoint_step": checkpoint_step,
            },
        )
        record = AuditRecord(
            snapshot_id=snapshot_id,
            base=project.name,
            profile=profile.value,
            status=AuditStatus.INDETERMINATE,
            decision="AUDIT_BUSY",
            judge_completed=False,
            delivery_allowed=False,
            created_at=utc_now(),
            error_class="TRANSIENT_AUDIT_BUSY",
            returncode=75,
        )
        return AuditOutcome(
            ExecutionResult.failed(
                "TRANSIENT_AUDIT_BUSY",
                returncode=75,
                audit_profile=profile.value,
                audit_status=AuditStatus.INDETERMINATE.value,
                final_decision="AUDIT_BUSY",
            ),
            record,
            snapshot,
        )

    @staticmethod
    def _profile_root(project: Path, profile: AuditProfile) -> Path:
        return project / ".factory" / "audits" / "profiles" / profile.value

    @classmethod
    def _latest_path(cls, project: Path, profile: AuditProfile) -> Path:
        return cls._profile_root(project, profile) / "latest.json"

    @staticmethod
    def _has_lines(path: Path, minimum: int) -> bool:
        if not path.is_file():
            return False
        return (
            len(
                path.read_text(encoding="utf-8", errors="replace").splitlines()
            )
            >= minimum
        )

    @staticmethod
    def _verdict(path: Path) -> str:
        if not path.is_file():
            return ""
        match = re.search(
            r"^VERDICT:\s*(\S+)",
            path.read_text(encoding="utf-8", errors="replace"),
            re.MULTILINE,
        )
        return match.group(1) if match else ""

    @staticmethod
    def _has_stub(project: Path) -> bool:
        models = project / "models"
        return models.is_dir() and any(models.rglob("*.stub"))
