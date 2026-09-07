"""Explicit accepted numeric claim versions and deterministic LaTeX values.

Selection is an explicit modeling decision, never an optimization heuristic.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.solver_job_receipt import read_receipt, COMPLETION_SCHEMA, canonical_hash, build_evidence
from scripts.verify_numbers import _resolve_dotted_json_path

LEDGER = "results/canonical_claims.json"
DERIVED = "tables/canonical_claim_values.tex"


def source_record(project, locator):
    relative, field = locator.split("::", 1)
    path = project / relative
    if Path(relative).is_absolute() or ".." in Path(relative).parts or path.is_symlink():
        raise ValueError(f"unsafe canonical source: {relative}")
    if not path.resolve().is_relative_to(project.resolve()):
        raise ValueError("canonical source escapes project")
    data = path.read_bytes()
    value = _resolve_dotted_json_path(json.loads(data), field)
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(f"canonical value must be a finite numeric scalar: {locator}")
    return {"locator": locator, "sha256": hashlib.sha256(data).hexdigest(), "value": value}


def render(ledger):
    lines = ["% Generated from explicit canonical claim versions. Do not edit."]
    for claim_id, claim in sorted(ledger["claims"].items()):
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", claim_id):
            raise ValueError("invalid canonical claim ID")
        lines.append(f"% {claim_id} version={claim['version']}")
        lines.append("\\expandafter\\def\\csname FactoryClaim" + claim_id
                     + "\\endcsname{" + str(claim["accepted"]["value"]) + "}")
    return "\n".join(lines) + "\n"


def accept(project, claim_id, source, completion, candidates, reason):
    import fcntl
    folder = project / ".factory"
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "canonical_claims.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _accept_locked(project, claim_id, source, completion, candidates, reason)


def _accept_locked(project, claim_id, source, completion, candidates, reason):
    project = project.resolve()
    if not reason.strip():
        raise ValueError("explicit acceptance reason is required")
    selected = source_record(project, source)
    completion_path = (project / completion).resolve()
    if not completion_path.is_relative_to(project):
        raise ValueError("completion receipt must stay inside project")
    receipt = read_receipt(completion_path, COMPLETION_SCHEMA)
    submitted = completion_path.with_name(completion_path.name.replace(".completed.json", ".submitted.json"))
    if submitted == completion_path or not build_evidence(project, submitted, completion_path)["receipt_ready"]:
        raise ValueError("accepted source requires the complete current submitted/completed receipt chain")
    if receipt.get("status") != "COMPLETED" or receipt.get("successful_outputs") is not True:
        raise ValueError("accepted source requires successful completed solver evidence")
    relative = source.split("::", 1)[0]
    if not any(r.get("path") == relative and r.get("sha256") == selected["sha256"]
               and r.get("exists") is True for r in receipt.get("outputs", [])):
        raise ValueError("selected source is not bound to the completed solver output")
    record = {"accepted": selected, "candidates": [source_record(project, c) for c in sorted(set([source, *candidates]))],
              "job_id": receipt["job_id"], "completion": completion,
              "completion_sha256": hashlib.sha256(completion_path.read_bytes()).hexdigest(),
              "submission": submitted.relative_to(project).as_posix(),
              "submission_sha256": hashlib.sha256(submitted.read_bytes()).hexdigest(),
              "reason": reason}
    record["version"] = canonical_hash(record)
    path = project / LEDGER
    ledger = json.loads(path.read_text()) if path.is_file() else {"schema": "canonical-claims-v1", "claims": {}}
    ledger["claims"][claim_id] = record
    text = render(ledger)
    archive = project / "results/canonical_claim_versions" / f"{record['version']}.json"
    archive.parent.mkdir(parents=True, exist_ok=True)
    if archive.exists():
        if json.loads(archive.read_text()) != record:
            raise ValueError("existing accepted version archive differs")
    else:
        with archive.open("x") as handle:
            json.dump(record, handle, sort_keys=True)
    from factory_core.projections import _atomic_text
    if any(source_record(project, c["locator"]) != c for c in record["candidates"]):
        raise ValueError("candidate source changed during acceptance")
    _atomic_text(project / DERIVED, text)
    _atomic_text(path, json.dumps(ledger, sort_keys=True, indent=2) + "\n")
    return record


def verify(project, base=None):
    """Return fail-closed diagnostics; never pick among scientific candidates."""
    project = project.resolve()
    path = project / LEDGER
    if not path.is_file():
        from scripts.claim_graph import load_declared_registry
        try:
            registry = load_declared_registry(project)
            if registry:
                for claim in registry["claims"]:
                    if claim["kind"] == "numeric" or any("field" in a for a in claim["artifacts"]):
                        return ["CANONICAL_CLAIM_VERSION_MISSING: " + claim["id"]]
        except (OSError, ValueError, TypeError) as exc:
            return ["canonical claim registry invalid: " + str(exc)]
        # Key results are explicitly claimed numbers; without an accepted
        # version, checking membership in any results file is insufficient.
        for result in (project / "results").rglob("*.json"):
            if "canonical_claim_versions" in result.parts:
                continue
            try:
                value = json.loads(result.read_text())
                if isinstance(value, dict) and value.get("key_results"):
                    return ["CANONICAL_CLAIM_VERSION_MISSING: " + result.relative_to(project).as_posix()]
            except (OSError, ValueError):
                continue
        return []
    errors = []
    try:
        ledger = json.loads(path.read_text())
        if ledger.get("schema") != "canonical-claims-v1" or not ledger.get("claims"):
            raise ValueError("invalid canonical claim ledger")
        from scripts.claim_graph import load_declared_registry
        registry = load_declared_registry(project)
        if registry:
            for declared in registry["claims"]:
                locators = []
                for artifact in declared["artifacts"]:
                    if "field" in artifact:
                        locator = artifact["path"] + "::" + artifact["field"]
                        try:
                            source_record(project, locator)
                            locators.append(locator)
                        except (ValueError, TypeError):
                            if declared["kind"] == "numeric":
                                raise
                if locators:
                    binding = ledger["claims"].get(declared["id"])
                    if binding is None or set(locators) != {c["locator"] for c in binding["candidates"]}:
                        errors.append(f"{declared['id']}: numeric claim candidates are not completely version-bound")
        source_files = {c["locator"].split("::", 1)[0] for claim in ledger["claims"].values()
                        for c in claim["candidates"]}
        for result in (project / "results").rglob("*.json"):
            if result.relative_to(project).as_posix() in source_files or "canonical_claim_versions" in result.parts:
                continue
            value = json.loads(result.read_text())
            if not isinstance(value, dict):
                continue
            for item in value.get("key_results", []):
                if not isinstance(item, dict) or "value" not in item:
                    raise ValueError("invalid derived key result")
                binding = ledger["claims"].get(item.get("claim_id"))
                if binding is None:
                    matches = [c for c in ledger["claims"].values()
                               if c["accepted"]["locator"] == item.get("canonical_source")]
                    binding = matches[0] if len(matches) == 1 else None
                if (binding is None or item["value"] != binding["accepted"]["value"]
                        or item.get("canonical_version") != binding["version"]):
                    errors.append("UNBOUND_DERIVED_KEY_RESULT: " + result.relative_to(project).as_posix())
        if (project / DERIVED).read_text() != render(ledger):
            errors.append("CANONICAL_DERIVED_VALUES_STALE")
        from factory_core.paper_sources import discover_paper_dependencies
        sources = discover_paper_dependencies(project, base or project.name)
        content = "\n".join(p.read_text(errors="replace") for p in sources
                            if p.suffix == ".tex" and p != project / DERIVED)
        numbers = [float(n) for n in re.findall(r"(?<![A-Za-z0-9_])[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?", content)]
        for claim_id, claim in ledger["claims"].items():
            unsigned = {k: v for k, v in claim.items() if k != "version"}
            if canonical_hash(unsigned) != claim["version"]:
                errors.append(f"{claim_id}: canonical version hash mismatch")
            archive = project / "results/canonical_claim_versions" / f"{claim['version']}.json"
            if json.loads(archive.read_text()) != claim:
                errors.append(f"{claim_id}: immutable accepted version is missing or changed")
            if source_record(project, claim["accepted"]["locator"]) != claim["accepted"]:
                errors.append(f"{claim_id}: accepted upstream source changed")
            receipt_path = project / claim["completion"]
            if not receipt_path.resolve().is_relative_to(project):
                raise ValueError("accepted receipt escapes project")
            submitted = project / claim["submission"]
            if not submitted.resolve().is_relative_to(project):
                raise ValueError("accepted submission escapes project")
            if hashlib.sha256(submitted.read_bytes()).hexdigest() != claim["submission_sha256"]:
                errors.append(f"{claim_id}: accepted submission changed")
            if not build_evidence(project, submitted, receipt_path)["receipt_ready"]:
                errors.append(f"{claim_id}: accepted solver evidence is not current")
            if hashlib.sha256(receipt_path.read_bytes()).hexdigest() != claim["completion_sha256"]:
                errors.append(f"{claim_id}: accepted run receipt changed")
            receipt = read_receipt(receipt_path, COMPLETION_SCHEMA)
            if receipt.get("job_id") != claim["job_id"] or receipt.get("successful_outputs") is not True:
                errors.append(f"{claim_id}: accepted run invalid")
            if not any(r.get("path") == claim["accepted"]["locator"].split("::", 1)[0]
                       and r.get("sha256") == claim["accepted"]["sha256"]
                       and r.get("exists") is True for r in receipt.get("outputs", [])):
                errors.append(f"{claim_id}: accepted value is not an output of the selected run")
            accepted_value = claim["accepted"]["value"]
            for candidate in claim["candidates"]:
                current = source_record(project, candidate["locator"])
                if current != candidate:
                    errors.append(f"{claim_id}: candidate changed; explicit re-acceptance required")
                value = candidate["value"]
                if not math.isclose(value, accepted_value, rel_tol=0.005, abs_tol=1e-6):
                    if any(math.isclose(n, value, rel_tol=0.005, abs_tol=1e-6) for n in numbers):
                        errors.append(f"{claim_id}: MIXED_CANONICAL_VERSION ({candidate['locator']})")
    except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
        errors.append(f"canonical claim evidence invalid: {exc}")
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", type=Path)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--claim")
    parser.add_argument("--source")
    parser.add_argument("--completion")
    parser.add_argument("--candidate", action="append", default=[])
    parser.add_argument("--reason")
    args = parser.parse_args()
    if args.check:
        errors = verify(args.project)
        print(json.dumps({"valid": not errors, "errors": errors}, ensure_ascii=False))
        return int(bool(errors))
    if not all((args.claim, args.source, args.completion, args.reason)):
        parser.error("acceptance requires --claim --source --completion --reason")
    print(json.dumps(accept(args.project, args.claim, args.source, args.completion,
                            args.candidate, args.reason), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
