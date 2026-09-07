"""Derive required execution evidence from adopted results and declared claims.

Selection is read-only. A missing or stale chain becomes an unsatisfied packet
requirement; it cannot disappear merely because a file was never selected.
"""
from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
import re

from scripts.solver_job_receipt import (
    SUBMISSION_SCHEMA, COMPLETION_SCHEMA, build_evidence, read_receipt, file_sha256,
)


def _path(project, relative):
    if (not isinstance(relative, str) or not relative or "\\" in relative
            or PurePosixPath(relative).is_absolute() or ".." in PurePosixPath(relative).parts):
        raise ValueError("solver evidence requires a project-relative path")
    path = project / relative
    if not path.resolve().is_relative_to(project) or path.is_symlink():
        raise ValueError("solver evidence path is outside the regular project files")
    return path


def _json(project, relative):
    value = json.loads(_path(project, relative).read_text())
    if not isinstance(value, dict):
        raise ValueError(f"solver evidence must be an object: {relative}")
    return value


def required_solver_evidence(project: Path, registry: dict) -> list[dict]:
    project = project.resolve()
    requirements = []
    selections = []

    def failed(label, anchor, error):
        requirements.append(dict(id="solver_chain:" + label, paths=[anchor],
            description="adopted computation evidence", required_status="included",
            binding_error=str(error)))

    ledger_path = "results/canonical_claims.json"
    if (project / ledger_path).exists():
        try:
            ledger = _json(project, ledger_path)
            if ledger.get("schema") != "canonical-claims-v1" or not isinstance(ledger.get("claims"), dict):
                raise ValueError("invalid adopted claim ledger")
            for claim_id, claim in ledger["claims"].items():
                try:
                    source = claim["accepted"]["locator"].split("::", 1)[0]
                    selections.append(dict(label="claim:" + claim_id, source=source,
                        job_id=claim["job_id"], submission=claim["submission"], completion=claim["completion"],
                        submission_sha256=claim["submission_sha256"], completion_sha256=claim["completion_sha256"],
                        anchors=[ledger_path, f"results/canonical_claim_versions/{claim['version']}.json"]))
                except (KeyError, TypeError, AttributeError) as exc:
                    failed("claim:" + claim_id, ledger_path, exc)
        except (OSError, ValueError, TypeError) as exc:
            failed("canonical_claims", ledger_path, exc)

    sources = {}
    canonical_path = "results/canonical_results.json"
    if (project / canonical_path).exists():
        try:
            canonical = _json(project, canonical_path)
            for key, item in canonical.items():
                if re.fullmatch(r"(?:p|P|problem)\d+", key) and isinstance(item, dict):
                    source = item.get("source_file") or item.get("source")
                    if source:
                        sources[source] = [canonical_path]
                    elif canonical.get("project") and canonical.get("primary_method"):
                        failed("canonical:" + key, canonical_path, "adopted result has no source")
        except (OSError, ValueError, TypeError) as exc:
            failed("canonical_results", canonical_path, exc)
    for claim in registry.get("claims", []):
        if not claim.get("required", True):
            continue
        for artifact in claim.get("artifacts", []):
            relative = artifact["path"]
            if relative.endswith(".json") and "execution" in artifact.get("roles", []):
                sources.setdefault(relative, [])
    for source, anchors in sorted(sources.items()):
        try:
            value = _json(project, source)
            job_id = value.get("job_id") or (value.get("provenance") or {}).get("job_id")
            if not job_id:
                if anchors:
                    raise ValueError("adopted source has no solver job identity")
                continue  # Non-computational claim artifacts have no job chain.
            if not isinstance(job_id, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", job_id):
                raise ValueError("invalid adopted solver job identity")
            prefix = f".factory/solver_receipts/{job_id}"
            selections.append(dict(label="source:" + source, source=source, job_id=job_id,
                                   submission=prefix + ".submitted.json", completion=prefix + ".completed.json",
                                   anchors=anchors))
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            failed("source:" + source, source, exc)

    for selection in selections:
        paths = set()
        error = None
        try:
            for relative in [*selection["anchors"], selection["source"], selection["submission"], selection["completion"]]:
                _path(project, relative)
                paths.add(relative)
            submitted_path = _path(project, selection["submission"])
            completed_path = _path(project, selection["completion"])
            submitted = read_receipt(submitted_path, SUBMISSION_SCHEMA)
            for relative in [submitted["script"]["path"], *[r["path"] for r in submitted["inputs"]],
                             *submitted["declared_outputs"],
                             *[r["snapshot"]["path"] for r in submitted.get("input_output_snapshots", [])]]:
                _path(project, relative)
                paths.add(relative)
            completed = read_receipt(completed_path, COMPLETION_SCHEMA)
            for artifact in completed.get("result_artifacts", []):
                if artifact.get("name") == "input_closure" and artifact.get("local"):
                    _path(project, artifact["path"])
                    paths.add(artifact["path"])
            if submitted["job_id"] != selection["job_id"] or selection["source"] not in submitted["declared_outputs"]:
                raise ValueError("adopted result is not an output of the selected solver job")
            for stage, path in (("submission", submitted_path), ("completion", completed_path)):
                expected_hash = selection.get(stage + "_sha256")
                if expected_hash and file_sha256(path) != expected_hash:
                    raise ValueError(f"adopted {stage} receipt changed")
            evidence = build_evidence(project, submitted_path, completed_path)
            if not evidence["receipt_ready"]:
                raise ValueError("solver chain is not current: " + ", ".join(evidence["errors"]))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            error = str(exc)
        requirement = dict(id="solver_chain:" + selection["label"],
            description="adopted solver inputs, receipts and declared outputs",
            job_id=selection["job_id"], adopted_output=selection["source"],
            required_status="included", paths=sorted(paths))
        if error:
            requirement["binding_error"] = error
        requirements.append(requirement)
    return requirements
