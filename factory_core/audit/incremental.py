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
# A checker that dies without a verdict is not evidence about the project.
# These markers identify the host running out of a resource the check needed.
_RESOURCE_EXHAUSTION_MARKERS = (
    "No space left on device",
    "[Errno 28]",
    "database or disk is full",
    "Disk quota exceeded",
    "[Errno 122]",
    "MemoryError",
    "Cannot allocate memory",
    "[Errno 12]",
    "Too many open files",
    "[Errno 24]",
)
# The last line of an uncaught traceback: an exception's qualified name,
# optionally followed by ": message".  Any name, not only *Error.
_EXCEPTION_LINE = re.compile(r"^[A-Za-z_][\w.]*(?::(?:\s.*)?)?$")
_TRACEBACK_HEADER = "Traceback (most recent call last):"
_LOG_TAIL_BYTES = 65_536
# Resource markers count only in the last line a process wrote before dying,
# so a finding report that quotes "MemoryError" earlier still reads as a FAIL.
_RESOURCE_TAIL_LINES = 1


def _ends_in_traceback(lines: list[str]) -> bool:
    """Whether the log ends with an uncaught Python exception.

    After the last traceback header, every line is an indented frame (or a
    chained-exception separator) until a final non-indented exception line.
    """

    try:
        start = max(index for index, line in enumerate(lines) if line.strip() == _TRACEBACK_HEADER)
    except ValueError:
        return False
    body = lines[start + 1 :]
    if not body or body[-1][:1].isspace() or not _EXCEPTION_LINE.match(body[-1].strip()):
        return False
    return all(line[:1].isspace() or not line.strip() for line in body[:-1])


def checker_no_verdict(returncode: int, log: Path) -> str:
    """Why a non-zero checker exit produced no verdict, or ``""`` if it did.

    The check scripts use different exit codes for FAIL (1, 2 or 3), so the
    code alone cannot separate a finding from a crash.  Three signals can:
    the process was killed by a signal, its last lines name an exhausted host
    resource, or the log ends in an uncaught Python traceback.  A script that
    reports a finding and exits normally ends with its report, not that way.
    """

    if returncode == 0:
        return ""
    if returncode < 0 or returncode >= 128:
        return f"terminated by signal (returncode {returncode})"
    try:
        with log.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - _LOG_TAIL_BYTES))
            tail = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
    lines = tail.rstrip().splitlines()
    last_lines = "\n".join(line for line in lines[-_RESOURCE_TAIL_LINES:])
    for marker in _RESOURCE_EXHAUSTION_MARKERS:
        if marker in last_lines:
            return f"host resource exhausted ({marker})"
    if _ends_in_traceback(lines):
        return f"uncaught exception: {lines[-1].strip()[:200]}"
    return ""


PROFILE_DECISIONS = {
    AuditProfile.MODEL: "MODEL_READY",
    AuditProfile.RESULTS: "RESULTS_VERIFIED",
    AuditProfile.PAPER: "PAPER_TRACE_READY",
}


#: A per-subproblem result directory as Step 5 names it (``problem1``,
#: ``problem4``, optionally a suffix such as ``problem2a``).
SUBPROBLEM_RESULT_DIR = re.compile(r"problem\d+[a-z0-9_-]*")


def declared_result_path_issues(
    registry: dict[str, object] | None, contract: dict[str, object] | None
) -> list[str]:
    """Declared solve outputs outside the directories Step 5 writes (F1).

    Step 4 declares in ``claim_registry.json`` and ``quality_contract.json``
    where each subproblem's solve output will be; Step 5 writes it to
    ``results/<slug>/`` with the slug ``problem<N>``.  Acceptance A declared
    ``results/qN/values.json``: the model profile only checked the schema, so
    the mismatch surfaced after the full solve as ``PERMANENT_CLAIM_BINDING``.
    Only files the ownership registry gives the solve (Stage 4) inside a
    subdirectory of ``results/`` are checked; top-level files such as
    ``results/canonical_results.json`` and sensitivity outputs are not.
    """

    from ..artifact_ownership import normalize_artifact_path
    from ..artifact_policy import artifact_policy_owner_stage

    paths: set[str] = set()
    if isinstance(registry, dict):
        for section in ("claims", "delivery_requirements"):
            for claim in registry.get(section) or []:
                for artifact in (claim.get("artifacts") or []) if isinstance(claim, dict) else []:
                    if isinstance(artifact, dict) and isinstance(artifact.get("path"), str):
                        paths.add(artifact["path"])
    if isinstance(contract, dict):
        for check in contract.get("competitiveness_checks") or []:
            if not isinstance(check, dict):
                continue
            for value in check.values():
                if isinstance(value, dict) and isinstance(value.get("path"), str):
                    paths.add(value["path"])
    issues = []
    for raw in sorted(paths):
        path = normalize_artifact_path(raw)
        parts = path.split("/")
        if len(parts) < 3 or parts[0] != "results":
            continue
        if artifact_policy_owner_stage(path) != 4:
            continue
        if not SUBPROBLEM_RESULT_DIR.fullmatch(parts[1].lower()):
            issues.append(path)
    return issues


#: The paper profile's advisory visual-gate report, under its profile root.
PAPER_VISUAL_GATE_REPORT = "visual_gate.latest.json"


@dataclass(frozen=True)
class StageCheck:
    name: str
    passed: bool
    severity: str
    detail: str
    report: str = ""
    returncode: int = 0
    timed_out: bool = False
    # The check ran but produced no verdict (crash, signal, exhausted host
    # resource).  Like a timeout, it says nothing about the project.
    indeterminate: bool = False

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
        indeterminate = [
            check for check in hard_failures if check.timed_out or check.indeterminate
        ]
        # A determinate finding is a real defect and wins: only when every hard
        # failure lacks a verdict is the audit as a whole undecided.
        undecided = bool(indeterminate) and len(indeterminate) == len(hard_failures)
        # What the repair route and the reported failed check are judged on.
        decisive = indeterminate if undecided else [
            check for check in hard_failures if check not in indeterminate
        ]
        warnings = [
            check
            for check in checks
            if not check.passed and check.severity == "warning"
        ]
        if undecided:
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
            "indeterminate_checks": [check.name for check in indeterminate],
            "warnings": [check.name for check in warnings],
            "repair_step": self._repair_step(
                profile, checkpoint_step, decisive
            ),
        }
        if profile is AuditProfile.PAPER:
            inputs = snapshot.identity.get("inputs", {})
            checker_contract = snapshot.identity.get("checker_contract", {})
            paper_inputs = {
                path: digest
                for path, digest in inputs.items()
                if path.endswith("_paper.tex") or path == "paper/paper.tex"
            }
            evidence.update(
                draft_content_fingerprint=self._canonical_hash(paper_inputs),
                paper_audit_input_fingerprint=snapshot.snapshot_id,
                checker_contract_sha256=self._canonical_hash(checker_contract),
            )
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
            "failed_check": decisive[0].name if decisive else "",
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
            return [*self._paper_checks(project), self._paper_visual_check(project)]
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
            valid = contract.get("version") in {3, 4}
            detail = (
                f"quality contract v{contract.get('version')} schema is valid"
                if valid
                else f"quality contract version={contract.get('version')} (expected 3 or 4)"
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
        if registry_valid:
            mismatched = declared_result_path_issues(
                registry, contract if valid else None
            )
            checks.append(
                StageCheck(
                    "claim_result_paths",
                    not mismatched,
                    "hard",
                    "declared per-subproblem results use results/problem<N>/"
                    if not mismatched
                    else (
                        "Step 5 writes per-subproblem results to results/problem<N>/ "
                        "(prompts/step5_full_solve.txt); declared elsewhere: "
                        + ", ".join(mismatched)
                    ),
                )
            )
        return checks

    def _results_checks(
        self, project: Path, checkpoint_step: int
    ) -> list[StageCheck]:
        checks = [self._canonical_results_check(project)]
        from scripts.canonical_claims import verify as verify_canonical_claims
        errors = verify_canonical_claims(project)
        checks.append(StageCheck("canonical_claim_versions", not errors, "hard",
                                 "; ".join(errors) or "accepted numeric versions are current"))
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
                        "derived_artifacts",
                        "scripts/verify_derived_artifacts.py",
                        [
                            project,
                            "--json-out",
                            project / "derived_artifacts_verification.latest.json",
                        ],
                        project / "logs" / "native_paper_derived_artifacts.log",
                        "hard",
                        # The stdout log is the runtime trace: it names a random
                        # temporary output directory, so its bytes change on
                        # every run.  The check's evidence is the stable
                        # report, which the final snapshot hashes.
                        project / "derived_artifacts_verification.latest.json",
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

    def _paper_visual_check(self, project: Path) -> StageCheck:
        """Run the PDF visual gate early, as advice (acceptance A, item 6).

        Until now only the final audit rendered the PDF, so 3pt figure text, a
        blank page or content past the page edge surfaced after the content
        freeze, when the one repair cycle could already be spent.  The paper
        profile now runs the same gate on the PDF Step 9/10 compiled.

        The check is ``advisory``: it never fails the profile, never writes a
        ledger row (a v1 Stage 8 exit would block on any open MAJOR row), and is
        not part of the final audit's shared paper suite.  Its report lives
        with the profile records under ``.factory/audits/profiles/paper/``,
        outside every tracked artifact root, so writing it reopens nothing.
        The review Steps read it from there.
        """

        root = self._profile_root(project, AuditProfile.PAPER)
        report = root / PAPER_VISUAL_GATE_REPORT
        pdf = project / f"{project.name}_paper.pdf"
        relative = str(report.relative_to(project))
        if not pdf.is_file():
            # An earlier run's report must not reach this run's review Steps.
            try:
                report.unlink(missing_ok=True)
            except OSError:
                pass
            return StageCheck(
                "visual_gate", False, "advisory",
                f"no compiled {pdf.name}; the visual gate runs at the final audit",
                "", 0, False, True,
            )
        from .service import FinalAuditService

        args: list[str | Path] = [pdf, "--output", report]
        tex_log = project / f"{project.name}_paper.log"
        if tex_log.is_file():
            args.extend(["--tex-log", tex_log])
        try:
            max_pages = FinalAuditService._configured_max_pages(project)
        except ValueError:
            max_pages = None
        if max_pages is not None:
            args.extend(["--max-pages", str(max_pages)])
        try:
            root.mkdir(parents=True, exist_ok=True)
            report.unlink(missing_ok=True)
            result = self.runner.python(
                self.factory_root,
                project,
                "scripts/pdf_visual_gate.py",
                args,
                label="audit_paper_visual_gate",
                timeout_seconds=600,
                accepted=(0, 1, 2),
                log_path=root / "visual_gate.latest.log",
            )
        except OSError as exc:
            return StageCheck(
                "visual_gate", False, "advisory", f"command error: {exc}",
                "", 1, False, True,
            )
        try:
            value = json.loads(report.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            value = None
        if not isinstance(value, dict):
            value = {}
        status = value.get("status")
        if status not in {"PASS", "FAIL"}:
            error = value.get("error") or f"exit={result.returncode}"
            return StageCheck(
                "visual_gate", False, "advisory", f"no verdict: {error}",
                relative if report.is_file() else "",
                result.returncode, result.timed_out, True,
            )
        return StageCheck(
            "visual_gate",
            status == "PASS",
            "advisory",
            f"status={status} blocking={value.get('blocking_findings', 0)}",
            relative,
            result.returncode,
        )

    def _command_checks(
        self,
        project: Path,
        profile: AuditProfile,
        definitions: Iterable[
            tuple[str, str, list[str | Path], Path, str]
            | tuple[str, str, list[str | Path], Path, str, Path]
        ],
    ) -> list[StageCheck]:
        """Run each checker with stdout captured at ``log``.

        A definition may name a separate ``report`` (sixth element): the
        stable evidence file the checker writes itself, which the check then
        records instead of its stdout log.  It is removed before the run so a
        report left by an earlier run is never mistaken for this one's; when
        the checker does not write it, the log is recorded as before.
        """
        checks: list[StageCheck] = []
        for name, script, args, log, severity, *evidence in definitions:
            report = evidence[0] if evidence else log
            try:
                if report != log:
                    report.unlink(missing_ok=True)
                result = self.runner.python(
                    self.factory_root,
                    project,
                    script,
                    args,
                    label=f"audit_{profile.value}_{name}",
                    timeout_seconds=600,
                    accepted=(0,),
                    log_path=log,
                )
                if not report.is_file():
                    # A checker that skipped (or failed before writing its
                    # report) leaves only its stdout log as evidence.
                    report = log
                no_verdict = (
                    ""
                    if result.accepted or result.timed_out
                    else checker_no_verdict(result.returncode, result.log_path)
                )
                if result.accepted:
                    detail = "PASS"
                elif no_verdict:
                    detail = f"no verdict, exit={result.returncode}: {no_verdict}"
                else:
                    detail = f"exit={result.returncode}"
                checks.append(
                    StageCheck(
                        name,
                        result.accepted,
                        severity,
                        detail,
                        str(report.relative_to(project)),
                        result.returncode,
                        result.timed_out,
                        bool(no_verdict),
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
            "problem/problem_plan.json",
            "problem/deliverables.json",
        }
        roots: set[str] = {"models"}
        if profile in {AuditProfile.RESULTS, AuditProfile.PAPER}:
            exact.update({"solve_log.md", "sensitivity_report.md"})
            exact.add("tables/canonical_claim_values.tex")
            roots.update({"models", "results", "run_state/solver_jobs"})
            roots.add(".factory/solver_receipts")
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
            "scripts/canonical_claims.py",
            "scripts/solver_job_receipt.py",
            "scripts/claim_graph.py",
        ]
        if profile is AuditProfile.MODEL:
            selected = [
                *common,
                "scripts/claim_graph.py",
                "scripts/quality_contract.py",
                # claim_result_paths resolves the Stage-4 owner of each path.
                "factory_core/artifact_ownership.py",
                "factory_core/artifact_policy.py",
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
                "scripts/number_manifest_stream.py",
                "scripts/verify_symbols.py",
                "scripts/verify_deliverables.py",
                "scripts/verify_derived_artifacts.py",
                "scripts/verify_invariants.py",
                "scripts/verify_spec_impl.py",
                "scripts/verify_quality_contract.py",
                "scripts/quality_contract.py",
                "scripts/pdf_visual_gate.py",
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
    def _canonical_hash(value: object) -> str:
        encoded = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

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
        if profile is AuditProfile.PAPER:
            evidence = value.get("evidence")
            if not isinstance(evidence, dict) or any(
                not isinstance(evidence.get(key), str)
                or len(str(evidence.get(key))) != 64
                for key in (
                    "draft_content_fingerprint",
                    "paper_audit_input_fingerprint",
                    "checker_contract_sha256",
                )
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
            # A check without a verdict says nothing about the project: its
            # row keeps whatever the last verdict recorded.  Advisory checks
            # never write a row.
            if not (check.timed_out or check.indeterminate)
            and check.severity != "advisory"
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

    def paper_checks(self, project: str | Path) -> list[StageCheck]:
        """Run the shared deterministic paper suite for a final submission."""

        return self._paper_checks(Path(project).resolve())

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
