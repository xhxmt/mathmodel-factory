from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..domain import PendingAction, ValidationResult
from ..audit.ledger import has_unresolved_blocking
from scripts.step8_5_gate import collect_step8_5_state
from scripts.workflow_state import gate2_delivery_allowed, gate2_verdict, step16_ready


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""


def _lines(path: Path) -> int:
    return len(_text(path).splitlines())


def _has(project: Path, relative: str, minimum_lines: int = 0) -> bool:
    path = project / relative
    return path.is_file() and (minimum_lines <= 0 or _lines(path) >= minimum_lines)


def _paper(project: Path) -> Path:
    direct = project / f"{project.name}_paper.tex"
    return direct if direct.is_file() else project / "paper" / "paper.tex"


def _verdict(path: Path) -> str:
    match = re.search(r"^VERDICT:\s*(\S+)", _text(path), re.MULTILINE)
    return match.group(1) if match else ""


def _stream_ids(project: Path) -> list[int]:
    return sorted(
        {int(value) for value in re.findall(r"^## Stream m(\d+)[：:]", _text(project / "viable_streams.md"), re.MULTILINE)}
    )


def _stream_ready(project: Path, stream_id: int) -> bool:
    return _has(project, f"m{stream_id}_spec.md", 30) and (project / f"m{stream_id}_demo_result.json").is_file()


def _stream_verdict(project: Path, stream_id: int) -> str:
    return _verdict(project / f"m{stream_id}_critique.md")


@dataclass(frozen=True)
class ValidatorCommandResult:
    returncode: int
    accepted: bool


def _run_status(
    root: Path,
    args: list[str],
    *,
    stdout: Path | None = None,
    accepted: tuple[int, ...] = (0,),
) -> ValidatorCommandResult:
    if stdout is not None:
        stdout.parent.mkdir(parents=True, exist_ok=True)
        with stdout.open("w", encoding="utf-8") as handle:
            result = subprocess.run(
                [sys.executable, *args],
                cwd=root,
                stdout=handle,
                stderr=subprocess.STDOUT,
                timeout=300,
                check=False,
            )
    else:
        result = subprocess.run(
            [sys.executable, *args],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    return ValidatorCommandResult(
        returncode=result.returncode,
        accepted=result.returncode in accepted,
    )


def _run(root: Path, args: list[str], *, stdout: Path | None = None, accepted: tuple[int, ...] = (0,)) -> bool:
    return _run_status(root, args, stdout=stdout, accepted=accepted).accepted


def _no_stubs(project: Path) -> bool:
    models = project / "models"
    return not models.is_dir() or not any(models.rglob("*.stub"))


def _verification_fresh(project: Path, report: Path) -> bool:
    if not report.is_file():
        return False
    results = project / "results"
    if not results.is_dir():
        return True
    report_mtime = report.stat().st_mtime
    return all(path.stat().st_mtime <= report_mtime for path in results.rglob("*") if path.is_file())


def _unresolved_blocking(project: Path) -> bool:
    return has_unresolved_blocking(project / "audit_issue_ledger.md")


def _incremental_gate(
    factory_root: Path,
    project: Path,
    profile: str,
    checkpoint_step: int,
) -> tuple[bool, str, tuple[str, ...], dict[str, object]]:
    from ..audit.domain import AuditStatus
    from ..audit.incremental import IncrementalAuditService

    outcome = IncrementalAuditService(factory_root).run_project(
        project,
        profile,
        checkpoint_step=checkpoint_step,
    )
    checks = outcome.record.evidence.get("checks", [])
    reports = tuple(
        str(check.get("report"))
        for check in checks
        if isinstance(check, dict) and check.get("report")
    )
    latest = str(
        (
            Path(".factory")
            / "audits"
            / "profiles"
            / profile
            / "latest.json"
        )
    )
    evidence = tuple(dict.fromkeys((latest, *reports)))
    valid = outcome.record.status is AuditStatus.PASS
    metadata: dict[str, object] = {
        **outcome.execution.metadata,
        "audit_profile": profile,
        "audit_status": outcome.record.status.value,
        "audit_snapshot": outcome.snapshot.snapshot_id,
        "audit_warnings": outcome.record.evidence.get("warnings", []),
    }
    if not valid:
        metadata["error_class"] = outcome.execution.error_class
        metadata["returncode"] = outcome.record.returncode
    reason = "" if valid else f"{profile} audit {outcome.record.status.value}: {outcome.record.decision}"
    return valid, reason, evidence, metadata


@dataclass(frozen=True)
class NativeArtifactValidator:
    factory_root: Path
    step_id: int

    def validate(self, context) -> ValidationResult:
        project = context.project_dir
        check = getattr(self, f"_step_{self.step_id}")
        try:
            valid, reason, evidence, metadata = check(project)
        except (OSError, subprocess.SubprocessError, ValueError, json.JSONDecodeError) as exc:
            return ValidationResult.invalid(f"native validator error: {exc}")
        if valid:
            return ValidationResult.valid(*evidence, metadata=metadata)
        action = metadata.get("pending_action")
        if isinstance(action, PendingAction):
            return ValidationResult.awaiting(action, *evidence)
        return ValidationResult.invalid(reason, *evidence, metadata=metadata)

    def _step_0(self, project: Path):
        required = (
            "problem/problem_brief.md",
            "problem/terminology_table.md",
            "problem/data_inventory.md",
            "problem/feasibility_constraints.md",
            "problem/candidate_methods.md",
            "problem/deliverables.json",
        )
        ok = all((project / path).is_file() for path in required)
        if ok and (project / "problem/candidate_methods.md").is_file():
            ok = _run(
                self.factory_root,
                [
                    "scripts/method_retrieve.py",
                    "--check-citations",
                    str(project / "problem/candidate_methods.md"),
                ],
            )
        return ok, "Step 0 artifacts or method citations invalid", required, {}

    def _step_1(self, project: Path):
        verdict = _verdict(project / "viability_gate.md")
        if verdict == "KILL" and _has(project, "kill_memo.md", 5):
            return True, "viability gate killed project", ("viability_gate.md", "kill_memo.md"), {"killed": True}
        ok = verdict == "PASS" and _has(project, "research_brief.md", 30) and _has(project, "viable_streams.md", 20)
        if ok:
            ok = _run(
                self.factory_root,
                ["scripts/method_retrieve.py", "--check-citations", str(project / "viable_streams.md")],
            )
        return ok, "Step 1 viability artifacts invalid", ("research_brief.md", "viable_streams.md", "viability_gate.md"), {}

    def _step_2(self, project: Path):
        validated = [stream for stream in _stream_ids(project) if _stream_ready(project, stream) and _stream_verdict(project, stream) == "VALIDATED"]
        return len(validated) >= 2, "fewer than two validated modeling streams", tuple(f"m{stream}_critique.md" for stream in validated), {}

    def _step_3(self, project: Path):
        ok = _has(project, "method_decision.md", 30) and _has(project, "chosen_method.md", 10) and _text(project / "chosen_method.md").startswith("PRIMARY:")
        return ok, "Step 3 decision artifacts invalid", ("method_decision.md", "chosen_method.md"), {}

    def _step_4(self, project: Path):
        ok = all((_has(project, name, minimum) for name, minimum in (("model.md", 100), ("symbol_table.md", 10), ("assumption_ledger.md", 10))))
        ok = ok and _verdict(project / "modeling_scope_gate.md") == "PASS" and (project / "quality_contract.json").is_file()
        if ok:
            return _incremental_gate(self.factory_root, project, "model", 4)
        return ok, "Step 4 model contract invalid", ("model.md", "symbol_table.md", "assumption_ledger.md", "modeling_scope_gate.md", "quality_contract.json"), {}

    def _step_5(self, project: Path):
        values = list((project / "results").rglob("values.json")) if (project / "results").is_dir() else []
        ok = _has(project, "solve_log.md", 20) and bool(values) and (project / "results/canonical_results.json").is_file() and _no_stubs(project)
        if ok:
            return _incremental_gate(self.factory_root, project, "results", 5)
        return ok, "Step 5 solve/provenance contract invalid", ("solve_log.md", "results/canonical_results.json"), {}

    def _step_6(self, project: Path):
        figures = list((project / "figures").glob("sensitivity_*.pdf")) + list((project / "figures").glob("sensitivity_*.png")) if (project / "figures").is_dir() else []
        ok = _has(project, "sensitivity_report.md", 20) and bool(figures)
        if ok:
            return _incremental_gate(self.factory_root, project, "results", 6)
        return ok, "Step 6 sensitivity artifacts invalid", ("sensitivity_report.md",), {}

    def _step_7(self, project: Path):
        return _has(project, "evaluation.md", 30), "Step 7 evaluation is incomplete", ("evaluation.md",), {}

    def _step_8(self, project: Path):
        figures = [p for p in (project / "figures").iterdir() if p.suffix.lower() in {".pdf", ".png"}] if (project / "figures").is_dir() else []
        return _has(project, "visualization_log.md", 20) and bool(figures), "Step 8 visualization artifacts invalid", ("visualization_log.md",), {}

    def _step_9(self, project: Path):
        gate = collect_step8_5_state(project)
        if not gate.get("ready"):
            action = PendingAction(type="human_consultation", gate="step8_5", metadata={"reason": gate.get("reason")})
            return False, "Step 8.5 is not ready", ("entry_gate.md", "reviewer_entry_map.md", "anchor_figure_plan.md"), {"pending_action": action}
        paper = _paper(project)
        text = _text(paper)
        ok = _lines(paper) > 200 and "\\begin{document}" in text and "\\end{document}" in text and "ABSTRACT_PLACEHOLDER" in text
        return ok, "Step 9 paper draft invalid", (str(paper.relative_to(project)),), {}

    def _step_10(self, project: Path):
        if not _has(project, "code_review.md", 20):
            return False, "code_review.md is incomplete", ("code_review.md",), {
                "error_class": "TRANSIENT_ARTIFACT_MISSING",
                "failed_check": "code_review",
                "missing_artifacts": ["code_review.md"],
            }
        return _incremental_gate(self.factory_root, project, "paper", 10)

    def _step_11(self, project: Path):
        return _has(project, "review_comments.md", 30), "Step 11 review is incomplete", ("review_comments.md",), {}

    def _step_12(self, project: Path):
        ok = _has(project, "revision_summary.md", 10) and _paper(project).is_file() and (project / "paper/archive/pre_step12").is_dir()
        return ok, "Step 12 revision artifacts invalid", ("revision_summary.md", "paper/archive/pre_step12"), {}

    def _step_13(self, project: Path):
        verdict = gate2_verdict(project)
        if verdict in {"PASS", "PRECHECK_PASS"} or gate2_delivery_allowed(project):
            return True, "", ("judge_evaluation.md",), {}
        if verdict == "INDETERMINATE_REVIEW":
            missing, resume = self._missing_packet_sources(project)
            if missing and resume is not None:
                return False, "Gate 2 packet is missing upstream artifacts", (
                    "judge_evaluation.md",
                ), {
                    "resume_after_step": resume,
                    "normalized_verdict": "PACKET_UPSTREAM_MISSING",
                    "missing_artifacts": missing,
                }
            return False, "Gate 2 evidence is indeterminate", ("judge_evaluation.md",), {
                "error_class": "TRANSIENT_JUDGE_INFRASTRUCTURE",
                "normalized_verdict": "INFRA_RETRY",
                "retry_scope": "step_13",
            }
        if verdict in {"REOPEN_REVISION_TEXT", "REOPEN_REVISION_MODEL"}:
            resume = self._gate2_resume(project, verdict)
            return False, f"Gate 2 requests {verdict}", ("judge_evaluation.md",), {"resume_after_step": resume}
        return False, "Step 13 judge verdict missing or invalid", ("judge_evaluation.md",), {}

    @staticmethod
    def _missing_packet_sources(project: Path) -> tuple[list[str], int | None]:
        missing: set[str] = set()
        for manifest_path in sorted((project / "judge_packets").glob("*/manifest.json")):
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            completeness = manifest.get("completeness")
            if not isinstance(completeness, dict):
                continue
            requirements = completeness.get("requirements")
            if not isinstance(requirements, list):
                continue
            for requirement in requirements:
                if not isinstance(requirement, dict) or requirement.get("satisfied") is True:
                    continue
                paths = requirement.get("paths")
                if not isinstance(paths, list):
                    continue
                if not paths:
                    inferred = NativeArtifactValidator._required_artifact_for(
                        project, str(requirement.get("id") or "")
                    )
                    if inferred:
                        missing.add(inferred)
                    continue
                for relative in paths:
                    if isinstance(relative, str) and not (project / relative).is_file():
                        missing.add(relative)
        if not missing:
            return [], None
        resume = min(NativeArtifactValidator._artifact_owner(path) for path in missing)
        return sorted(missing), resume

    @staticmethod
    def _required_artifact_for(project: Path, requirement_id: str) -> str:
        if requirement_id == "final_paper":
            return str(_paper(project).relative_to(project))
        if requirement_id.startswith(("question:", "claim:")):
            return "claim_registry.json"
        return {
            "problem_statement": "problem/problem_brief.md",
            "mathematical_exposition": "model.md",
            "primary_results": "results/canonical_results.json",
            "implementation": "models/<primary>/03_solve.py",
            "execution_trace": "solve_log.md",
            "claim_registry": "claim_registry.json",
        }.get(requirement_id, "")

    @staticmethod
    def _artifact_owner(relative: str) -> int:
        """Return the last completed step needed to rerun an artifact's owner."""
        if relative.startswith("problem/"):
            return -1
        if relative in {"research_brief.md", "viable_streams.md", "viability_gate.md"}:
            return 0
        if re.match(r"m\d+_(spec|demo_result|critique)", relative):
            return 1
        if relative in {"method_decision.md", "chosen_method.md"}:
            return 2
        if relative.startswith("models/") or relative in {
            "claim_registry.json",
            "model.md",
            "symbol_table.md",
            "assumption_ledger.md",
            "modeling_scope_gate.md",
            "quality_contract.json",
        }:
            return 3
        if relative.startswith("results/") or relative == "solve_log.md":
            return 4
        if relative.startswith("figures/sensitivity_") or relative == "sensitivity_report.md":
            return 5
        if relative == "evaluation.md":
            return 6
        if relative.startswith("figures/") or relative == "visualization_log.md":
            return 7
        if relative.endswith("_paper.tex") or relative.startswith("paper/") or relative in {
            "reviewer_entry_map.md",
            "anchor_figure_plan.md",
            "entry_gate.md",
        }:
            return 8
        if relative.endswith("verification.latest.txt") or relative.endswith("verification.latest.json"):
            return 9
        if relative == "review_comments.md":
            return 10
        return 11

    def _step_14(self, project: Path):
        paper = _paper(project)
        ok = _has(project, "abstract_draft.md", 20) and paper.is_file() and "ABSTRACT_PLACEHOLDER" not in _text(paper)
        return ok, "Step 14 abstract is incomplete", ("abstract_draft.md",), {}

    def _step_15(self, project: Path):
        paper = _paper(project)
        ok = _has(project, "citation_audit.md", 10) and _has(project, "derobotification.md", 10) and paper.is_file() and "ABSTRACT_PLACEHOLDER" not in _text(paper)
        return ok, "Step 15 polish artifacts invalid", ("citation_audit.md", "derobotification.md"), {}

    def _step_16(self, project: Path):
        reports = (project / "provenance_verification.latest.txt", project / "quality_contract_verification.latest.txt")
        ok = step16_ready(project, self.factory_root, project.name) and _no_stubs(project) and not _unresolved_blocking(project)
        ok = ok and all("VERDICT: PASS" in _text(report) and _verification_fresh(project, report) for report in reports)
        return ok, "Step 16 delivery contract invalid", (f"{project.name}_paper.pdf", "delivery_manifest.json"), {}

    @staticmethod
    def _gate2_resume(project: Path, verdict: str) -> int:
        if verdict == "REOPEN_REVISION_TEXT":
            return 11
        text = _text(project / "judge_evaluation.md").lower()
        if "math" in text and ("veto" in text or "indeterminate" in text):
            return 3
        if "execution" in text and ("veto" in text or "indeterminate" in text):
            return 4
        if "paper" in text and "indeterminate" in text:
            return 11
        return 3


def validator_for(factory_root: str | Path, step_id: int) -> NativeArtifactValidator:
    return NativeArtifactValidator(Path(factory_root).resolve(), step_id)
