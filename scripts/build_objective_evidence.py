#!/usr/bin/env python3
"""Build a conservative objective-evidence bundle for one project.

The external evaluator used to pass a naked ``UNMATCHED_NUMBERS`` count to an
LLM.  That count is useful as a diagnostic, but it is not a proof: the legacy
checker uses broad numeric matching and can be contaminated by log noise.  This
entry point turns the available mechanical observations into the
``objective-evidence-v1`` contract instead:

* every observation is bound to a deterministic input fingerprint;
* heuristic checks are WARN/UNKNOWN evidence, never hard vetoes;
* missing or ambiguous inputs are UNKNOWN, not PASS; and
* the bundle explicitly records that it cannot predict an award or human
  agreement.

The builder is read-only with respect to the project.  Only the requested
output file is written.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:  # pragma: no cover - direct script execution
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.objective_evidence import (  # noqa: E402
    SCHEMA_VERSION,
    ObjectiveEvidenceError,
    build_bundle,
    canonical_hash,
    make_finding,
)


BUILDER_VERSION = "objective-evidence-builder-v1"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _selected_paths(project: Path, base: str) -> list[str]:
    """Return the bounded, relevant source set used for the bundle hash."""

    candidates = [
        f"{base}_paper.tex",
        "paper/paper.tex",
        "problem",
        "results",
        "logs",
        "tables",
        "models",
        "tests",
        "numbers_manifest.json",
        "symbol_table.md",
        "quality_contract.json",
        "quality_contract_verification.latest.json",
        "number_chain_verification.latest.json",
        "invariants_verification.latest.json",
        "provenance_verification.latest.json",
        "spec_impl_verification.latest.json",
    ]
    # Keep missing primary files in the selection.  ``fingerprint_paths``
    # intentionally records missing paths, preventing stale PASS reuse.
    return candidates


def _input_fingerprint(project: Path, paths: list[str]) -> str:
    # Import lazily so the selected paths retain the objective-evidence module's
    # exact missing/symlink semantics.
    from scripts.objective_evidence import fingerprint_paths

    return fingerprint_paths(project, paths)


def _finding(
    *,
    project: Path,
    input_paths: list[str],
    finding_id: str,
    claim_id: str,
    checker_id: str,
    status: str,
    severity: str,
    trust_level: str,
    observed: Any,
    expected: Any,
    message: str,
    reason: str | None = None,
    evidence: list[str] | None = None,
) -> Any:
    return make_finding(
        finding_id=finding_id,
        claim_id=claim_id,
        checker_id=checker_id,
        checker_version=BUILDER_VERSION,
        status=status,
        severity=severity,
        trust_level=trust_level,
        observed=observed,
        expected=expected,
        root=project,
        input_paths=input_paths,
        evidence=evidence or [],
        message=message,
        reason=reason,
    )


def _numeric_finding(project: Path, base: str, paths: list[str]) -> Any:
    from scripts.verify_numbers import collect_number_metrics

    paper = project / f"{base}_paper.tex"
    if not paper.is_file():
        return _finding(
            project=project,
            input_paths=paths,
            finding_id="numbers.traceability",
            claim_id="numbers_traceability",
            checker_id="verify_numbers",
            status="UNKNOWN",
            severity="soft_alert",
            trust_level="heuristic",
            observed=None,
            expected={"paper": f"{base}_paper.tex"},
            message="paper source is unavailable",
            reason="missing",
        )
    try:
        metrics = collect_number_metrics(project, base)
    except (OSError, ValueError, TypeError) as exc:
        return _finding(
            project=project,
            input_paths=paths,
            finding_id="numbers.traceability",
            claim_id="numbers_traceability",
            checker_id="verify_numbers",
            status="UNKNOWN",
            severity="soft_alert",
            trust_level="heuristic",
            observed=None,
            expected=None,
            message=f"numeric checker failed: {exc}",
            reason="error",
        )
    if not isinstance(metrics, dict):
        return _finding(
            project=project,
            input_paths=paths,
            finding_id="numbers.traceability",
            claim_id="numbers_traceability",
            checker_id="verify_numbers",
            status="UNKNOWN",
            severity="soft_alert",
            trust_level="heuristic",
            observed=None,
            expected=None,
            message="numeric checker returned no metrics",
            reason="unavailable",
        )
    unmatched = int(metrics.get("numbers_unmatched", 0) or 0)
    paper_count = int(len(metrics.get("_paper_numbers") or []))
    reference_count = int(metrics.get("_reference_count", 0) or 0)
    observed = {
        "paper_numbers": paper_count,
        "unmatched": unmatched,
        "matched": int(metrics.get("numbers_matched", 0) or 0),
        "reference_numbers": reference_count,
        "checker_semantics": "heuristic_approximate_matching",
    }
    if reference_count == 0 and paper_count > 0:
        status, reason, message = "UNKNOWN", "missing", "no independent numeric source set was found"
    elif unmatched:
        status, reason, message = (
            "WARN",
            "heuristic_unmatched_count",
            "legacy numeric matching found unmatched paper values; this is diagnostic only",
        )
    else:
        status, reason, message = "PASS", None, "all sampled values matched the heuristic source set"
    return _finding(
        project=project,
        input_paths=paths,
        finding_id="numbers.traceability",
        claim_id="numbers_traceability",
        checker_id="verify_numbers",
        status=status,
        severity="soft_alert" if status != "PASS" else "info",
        trust_level="heuristic",
        observed=observed,
        expected={"unmatched": 0},
        message=message,
        reason=reason,
        evidence=["numbers_manifest.json"] if (project / "numbers_manifest.json").is_file() else [],
    )


def _symbol_finding(project: Path, base: str, paths: list[str]) -> Any:
    from scripts.verify_symbols import collect_symbol_metrics

    paper = project / f"{base}_paper.tex"
    if not paper.is_file():
        return _finding(
            project=project,
            input_paths=paths,
            finding_id="symbols.consistency",
            claim_id="symbols_consistency",
            checker_id="verify_symbols",
            status="UNKNOWN",
            severity="soft_alert",
            trust_level="heuristic",
            observed=None,
            expected=None,
            message="paper source is unavailable",
            reason="missing",
        )
    try:
        metrics = collect_symbol_metrics(project, base)
    except (OSError, ValueError, TypeError) as exc:
        return _finding(
            project=project,
            input_paths=paths,
            finding_id="symbols.consistency",
            claim_id="symbols_consistency",
            checker_id="verify_symbols",
            status="UNKNOWN",
            severity="soft_alert",
            trust_level="heuristic",
            observed=None,
            expected=None,
            message=f"symbol checker failed: {exc}",
            reason="error",
        )
    if not isinstance(metrics, dict):
        return _finding(
            project=project,
            input_paths=paths,
            finding_id="symbols.consistency",
            claim_id="symbols_consistency",
            checker_id="verify_symbols",
            status="UNKNOWN",
            severity="soft_alert",
            trust_level="heuristic",
            observed=None,
            expected=None,
            message="symbol checker returned no metrics",
            reason="unavailable",
        )
    undefined = int(metrics.get("symbols_undefined", 0) or 0)
    before = int(metrics.get("use_before_def", 0) or 0)
    observed = {
        "symbols_defined": int(metrics.get("symbols_defined", 0) or 0),
        "symbols_used": int(metrics.get("symbols_used", 0) or 0),
        "undefined": undefined,
        "use_before_def": before,
        "checker_semantics": "regex_heuristic",
    }
    if not (project / "symbol_table.md").is_file():
        status, reason, message = "UNKNOWN", "missing", "symbol table is unavailable"
    elif undefined or before:
        status, reason, message = (
            "WARN",
            "heuristic_symbol_signal",
            "symbol checker reported possible consistency issues; not a hard gate",
        )
    else:
        status, reason, message = "PASS", None, "no heuristic symbol inconsistencies were observed"
    return _finding(
        project=project,
        input_paths=paths,
        finding_id="symbols.consistency",
        claim_id="symbols_consistency",
        checker_id="verify_symbols",
        status=status,
        severity="soft_alert" if status != "PASS" else "info",
        trust_level="heuristic",
        observed=observed,
        expected={"undefined": 0, "use_before_def": 0},
        message=message,
        reason=reason,
        evidence=["symbol_table.md"] if (project / "symbol_table.md").is_file() else [],
    )


def _artifact_finding(project: Path, base: str, paths: list[str]) -> Any:
    required = [
        f"{base}_paper.tex",
        "results",
        "models",
        "logs",
    ]
    present = [item for item in required if (project / item).exists()]
    missing = [item for item in required if item not in present]
    if missing:
        status, reason, message = "UNKNOWN", "missing", "core execution artifacts are missing"
    else:
        status, reason, message = "PASS", None, "core execution artifact paths are present"
    return _finding(
        project=project,
        input_paths=paths,
        finding_id="execution.artifact_presence",
        claim_id="execution_artifact_presence",
        checker_id="objective_evidence.builder",
        status=status,
        severity="soft_alert" if status != "PASS" else "info",
        trust_level="infrastructure",
        observed={"present": present, "missing": missing},
        expected={"required": required},
        message=message,
        reason=reason,
    )


def build_objective_evidence(project: Path, base: str | None = None) -> dict[str, Any]:
    project = Path(project).resolve()
    if not project.is_dir():
        raise ObjectiveEvidenceError(f"project directory not found: {project}")
    base = base or project.name
    paths = _selected_paths(project, base)
    fingerprint = _input_fingerprint(project, paths)
    findings = [
        _numeric_finding(project, base, paths),
        _symbol_finding(project, base, paths),
        _artifact_finding(project, base, paths),
    ]
    bundle = build_bundle(
        project,
        findings,
        project_id=base,
        input_paths=paths,
        metadata={
            "builder": BUILDER_VERSION,
            "source_paths": paths,
            "numeric_signal_semantics": "heuristic_diagnostic_only",
            "symbol_signal_semantics": "heuristic_diagnostic_only",
            "unknown_policy": "UNKNOWN_is_not_PASS",
            "hard_veto_policy": "factory_oracle_or_dual_impl_plus_exact_contradiction_only",
            "human_alignment": "UNAVAILABLE_WITHOUT_HUMAN_LABELS",
            "award_prediction": "UNAVAILABLE",
        },
    )
    # Keep a redundant explicit check close to the builder: if a future change
    # accidentally alters selected-path hashing, fail closed before publishing.
    if bundle.input_fingerprint != fingerprint:
        raise ObjectiveEvidenceError("bundle input fingerprint mismatch")
    payload = bundle.to_dict()
    payload["decision_semantics"] = "EVIDENCE_COLLECTION_ONLY"
    payload["quality_verdict"] = "UNAVAILABLE"
    payload["bundle_sha256"] = canonical_hash(payload)
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", type=Path)
    parser.add_argument("base", nargs="?")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--json", action="store_true", help="print the bundle")
    args = parser.parse_args(argv)
    try:
        payload = build_objective_evidence(args.project, args.base)
        output = args.output
        if not output.is_absolute():
            output = Path.cwd() / output
        project = Path(args.project).resolve()
        output = output.resolve()
        # The evaluator may keep the bundle in a staging directory.  It must
        # still be explicit about where it writes; no project files are changed
        # implicitly.
        _atomic_json(output, payload)
        summary = payload.get("summary", {})
        print(
            f"objective evidence: {summary.get('decision', 'INDETERMINATE')} "
            f"(findings={len(payload.get('findings', []))}, "
            f"fingerprint={payload.get('input_fingerprint')})"
        )
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except (OSError, ObjectiveEvidenceError, ValueError, TypeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
