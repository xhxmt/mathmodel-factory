#!/usr/bin/env python3
"""Oracle-backed capability calibration for exact runtime judge packets.

This module deliberately does not call a judge or alter delivery control flow.
It prepares deterministic packet mutations and evaluates externally produced
observations.  The resulting metrics describe bounded mutation-detection
capability, not agreement with human judges or award-level validity.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import shutil
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


MANIFEST_SCHEMA = "judge-capability-manifest-v1"
PREPARED_SCHEMA = "judge-capability-prepared-v1"
OBSERVATION_SCHEMA = "judge-capability-observation-v1"
GROUNDING_SCHEMA = "evidence-grounding-v1"
REPORT_SCHEMA = "judge-capability-report-v1"
CASE_KINDS = {"hard_defect", "neutral_transform"}
SPLITS = {"train", "test"}
DECISIONS = {
    "PASS",
    "FAIL",
    "REVISE",
    "REOPEN_MODEL",
    "REOPEN_TEXT",
    "REOPEN_REVISION_MODEL",
    "REOPEN_REVISION_TEXT",
    "INDETERMINATE",
    "INDETERMINATE_REVIEW",
}
HASH_FIELDS = ("prompt_sha256", "schema_sha256", "packet_sha256")
EVALUATOR_FIELDS = ("model", "backend", "prompt_sha256", "schema_sha256")
HOLDOUT_AXES = ("project_id", "problem_id", "mutation_family")
NEUTRAL_INVARIANTS = {
    "json_semantically_equal_to_source",
    "line_multiset_equal_to_source",
    "redaction_equivalent_to_source",
    "whitespace_equivalent_to_source",
}


class CapabilityError(ValueError):
    """Raised when calibration evidence is invalid or cannot be trusted."""


def canonical_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tree_sha256(root: Path) -> str:
    """Hash packet bytes and relative paths without relying on filesystem metadata."""
    if not root.is_dir():
        raise CapabilityError(f"packet directory not found: {root}")
    entries: list[dict[str, str]] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise CapabilityError(f"packet must not contain symlinks: {path}")
        if path.is_file():
            entries.append({"path": path.relative_to(root).as_posix(), "sha256": file_sha256(path)})
    if not entries:
        raise CapabilityError(f"packet directory is empty: {root}")
    return canonical_hash(entries)


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[0-9a-f]{64}", value))


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CapabilityError(f"invalid JSON file {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise CapabilityError(f"JSON root must be an object: {path}")
    return value


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _required_text(mapping: dict[str, Any], field: str, context: str) -> str:
    value = mapping.get(field)
    if not isinstance(value, str) or not value.strip():
        raise CapabilityError(f"{context}.{field} must be a non-empty string")
    return value


def _safe_relative(root: Path, value: Any, *, must_exist: bool = True) -> Path:
    if not isinstance(value, str) or not value:
        raise CapabilityError("path must be a non-empty relative string")
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise CapabilityError(f"path must stay below its root: {value}")
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise CapabilityError(f"path escapes its root: {value}") from exc
    if must_exist and not candidate.exists():
        raise CapabilityError(f"path does not exist: {candidate}")
    return candidate


def _packet_file(packet: Path, value: Any, *, must_exist: bool = True) -> Path:
    path = _safe_relative(packet, value, must_exist=must_exist)
    if path.exists() and not path.is_file():
        raise CapabilityError(f"oracle/mutation target is not a file: {path}")
    return path


def _json_pointer(value: Any, pointer: Any, *, parent: bool = False) -> Any:
    if not isinstance(pointer, str) or (pointer and not pointer.startswith("/")):
        raise CapabilityError(f"invalid JSON pointer: {pointer!r}")
    tokens = [] if not pointer else [part.replace("~1", "/").replace("~0", "~") for part in pointer[1:].split("/")]
    if parent:
        if not tokens:
            raise CapabilityError("cannot mutate the JSON document root")
        tokens, final = tokens[:-1], tokens[-1]
    current = value
    for token in tokens:
        if isinstance(current, dict) and token in current:
            current = current[token]
        elif isinstance(current, list) and token.isdigit() and int(token) < len(current):
            current = current[int(token)]
        else:
            raise CapabilityError(f"JSON pointer does not exist: {pointer}")
    return (current, final) if parent else current


def evaluator_identity(value: dict[str, Any]) -> dict[str, str]:
    identity = {field: _required_text(value, field, "evaluator") for field in EVALUATOR_FIELDS}
    for field in ("prompt_sha256", "schema_sha256"):
        if not _is_sha256(identity[field]):
            raise CapabilityError(f"evaluator.{field} must be a lowercase SHA-256")
    return identity


def _bind_evaluator_artifacts(
    value: dict[str, Any], manifest_root: Path, identity: dict[str, str]
) -> dict[str, dict[str, str]]:
    receipts: dict[str, dict[str, str]] = {}
    for name, hash_field in (("prompt", "prompt_sha256"), ("schema", "schema_sha256")):
        path_field = f"{name}_path"
        path = _safe_relative(manifest_root, value.get(path_field))
        if not path.is_file():
            raise CapabilityError(f"evaluator.{path_field} must identify a regular file")
        actual = file_sha256(path)
        if actual != identity[hash_field]:
            raise CapabilityError(f"evaluator.{hash_field} does not match {path_field}")
        receipts[name] = {
            "path": str(path.relative_to(manifest_root.resolve())),
            "sha256": actual,
        }
    return receipts


def exact_runtime_identity(evaluator: dict[str, str], packet_hash: str) -> dict[str, str]:
    if not _is_sha256(packet_hash):
        raise CapabilityError("packet_sha256 must be a lowercase SHA-256")
    return {**evaluator, "packet_sha256": packet_hash}


def evaluator_fingerprint(identity: dict[str, Any]) -> str:
    return canonical_hash({field: identity.get(field) for field in EVALUATOR_FIELDS})


def runtime_fingerprint(identity: dict[str, Any]) -> str:
    return canonical_hash({field: identity.get(field) for field in (*EVALUATOR_FIELDS, "packet_sha256")})


def decision_class(decision: str) -> str:
    if decision == "PASS":
        return "PASS"
    if decision in {"INDETERMINATE", "INDETERMINATE_REVIEW"}:
        return "INDETERMINATE"
    if decision in {"REVISE", "REOPEN_TEXT", "REOPEN_REVISION_TEXT"}:
        return "BLOCK_TEXT"
    if decision in {"FAIL", "REOPEN_MODEL", "REOPEN_REVISION_MODEL"}:
        return "BLOCK_MODEL"
    raise CapabilityError(f"unsupported judge decision: {decision}")


def _validate_exact_identity(value: Any, expected: dict[str, str], context: str) -> None:
    if not isinstance(value, dict):
        raise CapabilityError(f"{context}.runtime_identity must be an object")
    actual = {field: value.get(field) for field in (*EVALUATOR_FIELDS, "packet_sha256")}
    if actual != expected:
        raise CapabilityError(f"{context}.runtime_identity does not match prepared packet identity")
    for field in HASH_FIELDS:
        if not _is_sha256(actual[field]):
            raise CapabilityError(f"{context}.runtime_identity.{field} is not a SHA-256")


def _apply_mutation(packet: Path, mutation: dict[str, Any]) -> None:
    kind = _required_text(mutation, "type", "mutation")
    target = _packet_file(packet, mutation.get("path"))
    if kind == "delete_file":
        target.unlink()
    elif kind == "text_replace":
        old = mutation.get("old")
        new = mutation.get("new")
        count = mutation.get("count", 1)
        if not isinstance(old, str) or not old or not isinstance(new, str) or old == new:
            raise CapabilityError("text_replace needs distinct non-empty old and string new values")
        if not isinstance(count, int) or count < 1:
            raise CapabilityError("text_replace.count must be a positive integer")
        text = target.read_text(encoding="utf-8")
        if text.count(old) < count:
            raise CapabilityError(f"text_replace precondition failed for {target}: expected {count} occurrences")
        target.write_text(text.replace(old, new, count), encoding="utf-8")
    elif kind == "text_append":
        text = mutation.get("text")
        if not isinstance(text, str) or not text:
            raise CapabilityError("text_append.text must be a non-empty string")
        target.write_text(target.read_text(encoding="utf-8") + text, encoding="utf-8")
    elif kind in {"json_set", "json_delete"}:
        document = _read_json(target)
        parent, key = _json_pointer(document, mutation.get("pointer"), parent=True)
        if isinstance(parent, dict):
            if key not in parent and kind == "json_delete":
                raise CapabilityError(f"json_delete target does not exist: {mutation.get('pointer')}")
            if kind == "json_set":
                if key not in parent and mutation.get("allow_create") is not True:
                    raise CapabilityError(f"json_set target does not exist: {mutation.get('pointer')}")
                parent[key] = mutation.get("value")
            else:
                del parent[key]
        elif isinstance(parent, list) and key.isdigit() and int(key) < len(parent):
            if kind == "json_set":
                parent[int(key)] = mutation.get("value")
            else:
                del parent[int(key)]
        else:
            raise CapabilityError(f"JSON mutation target does not exist: {mutation.get('pointer')}")
        target.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    elif kind == "json_reorder_keys":
        document = _read_json(target)
        target.write_text(json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    elif kind == "normalize_whitespace":
        text = target.read_text(encoding="utf-8")
        normalized = "\n".join(line.rstrip() for line in text.splitlines()) + "\n"
        target.write_text(normalized, encoding="utf-8")
    elif kind == "sort_lines":
        lines = target.read_text(encoding="utf-8").splitlines()
        target.write_text("\n".join(sorted(lines)) + "\n", encoding="utf-8")
    else:
        raise CapabilityError(f"unsupported mutation type: {kind}")


def _normalized_whitespace(text: str) -> str:
    return " ".join(text.split())


def _oracle_assertion(
    assertion: dict[str, Any], packet: Path, source: Path, context: str
) -> dict[str, Any]:
    kind = _required_text(assertion, "type", context)
    path_value = assertion.get("path")
    result = False
    detail = ""
    if kind in {"file_exists", "file_missing"}:
        path = _packet_file(packet, path_value, must_exist=False)
        result = path.is_file() if kind == "file_exists" else not path.exists()
        detail = str(path.relative_to(packet))
    elif kind in {"text_contains", "text_not_contains", "text_regex", "text_count_equals"}:
        path = _packet_file(packet, path_value)
        text = path.read_text(encoding="utf-8")
        needle = assertion.get("value")
        if not isinstance(needle, str):
            raise CapabilityError(f"{context}.value must be a string")
        if kind == "text_contains":
            result = needle in text
        elif kind == "text_not_contains":
            result = needle not in text
        elif kind == "text_regex":
            try:
                result = re.search(needle, text) is not None
            except re.error as exc:
                raise CapabilityError(f"{context}.value is not a valid regular expression: {exc}") from exc
        else:
            expected_count = assertion.get("count")
            if not isinstance(expected_count, int) or expected_count < 0:
                raise CapabilityError(f"{context}.count must be a non-negative integer")
            result = text.count(needle) == expected_count
        detail = str(path.relative_to(packet))
    elif kind in {"json_path_equals", "json_path_missing"}:
        path = _packet_file(packet, path_value)
        document = _read_json(path)
        pointer = assertion.get("pointer")
        if not isinstance(pointer, str) or (pointer and not pointer.startswith("/")):
            raise CapabilityError(f"{context}.pointer must be an RFC 6901 JSON pointer")
        try:
            actual = _json_pointer(document, pointer)
            exists = True
        except CapabilityError:
            actual, exists = None, False
        result = (exists and actual == assertion.get("value")) if kind == "json_path_equals" else not exists
        detail = f"{path.relative_to(packet)}:{pointer}"
    elif kind == "file_sha256_equals":
        path = _packet_file(packet, path_value)
        expected = assertion.get("sha256")
        if not _is_sha256(expected):
            raise CapabilityError(f"{context}.sha256 must be a lowercase SHA-256")
        result = file_sha256(path) == expected
        detail = str(path.relative_to(packet))
    elif kind in NEUTRAL_INVARIANTS:
        path = _packet_file(packet, path_value)
        original = _packet_file(source, path_value)
        if kind == "json_semantically_equal_to_source":
            result = _read_json(path) == _read_json(original)
        elif kind == "whitespace_equivalent_to_source":
            result = _normalized_whitespace(path.read_text(encoding="utf-8")) == _normalized_whitespace(
                original.read_text(encoding="utf-8")
            )
        elif kind == "line_multiset_equal_to_source":
            result = sorted(path.read_text(encoding="utf-8").splitlines()) == sorted(
                original.read_text(encoding="utf-8").splitlines()
            )
        else:
            patterns = assertion.get("patterns")
            placeholder = assertion.get("placeholder", "[ANON]")
            if not isinstance(patterns, list) or not patterns or not all(
                isinstance(pattern, str) and pattern for pattern in patterns
            ):
                raise CapabilityError(f"{context}.patterns must be a non-empty string list")
            if not isinstance(placeholder, str):
                raise CapabilityError(f"{context}.placeholder must be a string")

            def redact(text: str) -> str:
                for pattern in patterns:
                    try:
                        text = re.sub(pattern, placeholder, text)
                    except re.error as exc:
                        raise CapabilityError(f"{context}.patterns contains invalid regex: {exc}") from exc
                return text

            result = redact(path.read_text(encoding="utf-8")) == redact(
                original.read_text(encoding="utf-8")
            )
        detail = str(path.relative_to(packet))
    elif kind == "different_from_source":
        path = _packet_file(packet, path_value)
        original = _packet_file(source, path_value)
        result = file_sha256(path) != file_sha256(original)
        detail = str(path.relative_to(packet))
    else:
        raise CapabilityError(f"unsupported oracle assertion: {kind}")
    return {"type": kind, "passed": result, "detail": detail}


def _run_oracles(
    assertions: Any, packet: Path, source: Path, context: str
) -> list[dict[str, Any]]:
    if not isinstance(assertions, list) or not assertions:
        raise CapabilityError(f"{context} must contain at least one oracle assertion")
    results: list[dict[str, Any]] = []
    for index, assertion in enumerate(assertions):
        if not isinstance(assertion, dict):
            raise CapabilityError(f"{context}[{index}] must be an object")
        result = _oracle_assertion(assertion, packet, source, f"{context}[{index}]")
        results.append(result)
        if not result["passed"]:
            raise CapabilityError(f"{context}[{index}] failed: {result['type']} {result['detail']}")
    return results


def _case_metadata(case: dict[str, Any], index: int) -> dict[str, str]:
    context = f"cases[{index}]"
    result = {
        field: _required_text(case, field, context)
        for field in ("id", "project_id", "problem_id", "mutation_family", "role", "kind", "split")
    }
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", result["id"]):
        raise CapabilityError(f"{context}.id contains unsafe characters")
    if result["kind"] not in CASE_KINDS:
        raise CapabilityError(f"{context}.kind must be one of {sorted(CASE_KINDS)}")
    if result["split"] not in SPLITS:
        raise CapabilityError(f"{context}.split must be train or test")
    return result


def _validate_holdout(cases: list[dict[str, Any]], axes: Any) -> dict[str, Any]:
    if not isinstance(axes, list) or not axes or any(axis not in HOLDOUT_AXES for axis in axes):
        raise CapabilityError(f"holdout_axes must be a non-empty subset of {list(HOLDOUT_AXES)}")
    audit: dict[str, Any] = {"axes": axes, "partitions": {}, "leakage": {}}
    for axis in axes:
        train = {str(case[axis]) for case in cases if case["split"] == "train"}
        test = {str(case[axis]) for case in cases if case["split"] == "test"}
        overlap = sorted(train & test)
        audit["partitions"][axis] = {"train": sorted(train), "test": sorted(test)}
        audit["leakage"][axis] = overlap
        if overlap:
            raise CapabilityError(f"train/test leakage on {axis}: {', '.join(overlap)}")
    audit["passed"] = True
    return audit


def prepare_manifest(manifest: dict[str, Any], manifest_root: Path, output_dir: Path) -> dict[str, Any]:
    """Materialize mutations only after both sides of the oracle contract pass."""
    if manifest.get("schema") != MANIFEST_SCHEMA:
        raise CapabilityError(f"manifest.schema must be {MANIFEST_SCHEMA}")
    evaluator_value = manifest.get("evaluator")
    if not isinstance(evaluator_value, dict):
        raise CapabilityError("manifest.evaluator must be an object")
    evaluator = evaluator_identity(evaluator_value)
    raw_cases = manifest.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise CapabilityError("manifest.cases must be a non-empty list")
    metadata: list[dict[str, str]] = []
    for index, case in enumerate(raw_cases):
        if not isinstance(case, dict):
            raise CapabilityError(f"cases[{index}] must be an object")
        metadata.append(_case_metadata(case, index))
    if len({case["id"] for case in metadata}) != len(metadata):
        raise CapabilityError("case ids must be unique")
    holdout = _validate_holdout(metadata, manifest.get("holdout_axes", list(HOLDOUT_AXES)))
    evaluator_artifacts = _bind_evaluator_artifacts(evaluator_value, manifest_root, evaluator)

    output_dir.mkdir(parents=True, exist_ok=True)
    prepared_cases: list[dict[str, Any]] = []
    for index, (case, meta) in enumerate(zip(raw_cases, metadata)):
        source = _safe_relative(manifest_root, case.get("source_packet"))
        if not source.is_dir():
            raise CapabilityError(f"cases[{index}].source_packet must be a directory")
        try:
            output_dir.resolve().relative_to(source.resolve())
        except ValueError:
            pass
        else:
            raise CapabilityError(f"output directory must not be inside source packet: {source}")
        destination = output_dir / "cases" / meta["id"] / "packet"
        if destination.exists():
            raise CapabilityError(f"prepared case already exists: {destination}")
        staging = destination.parent / ".packet.preparing"
        if staging.exists():
            raise CapabilityError(f"stale preparation directory exists: {staging}")
        oracles = case.get("oracles")
        if not isinstance(oracles, dict):
            raise CapabilityError(f"cases[{index}].oracles must be an object")
        preconditions = oracles.get("preconditions")
        postconditions = oracles.get("postconditions")
        if meta["kind"] == "neutral_transform":
            post_types = {
                item.get("type") for item in postconditions if isinstance(item, dict)
            } if isinstance(postconditions, list) else set()
            if not post_types & NEUTRAL_INVARIANTS:
                raise CapabilityError(
                    f"neutral case {meta['id']} needs a machine-checkable source-equivalence postcondition"
                )
        source_hash = tree_sha256(source)
        pre_results = _run_oracles(preconditions, source, source, f"cases[{index}].oracles.preconditions")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, staging, symlinks=False)
        try:
            mutation = case.get("mutation")
            if not isinstance(mutation, dict):
                raise CapabilityError(f"cases[{index}].mutation must be an object")
            _apply_mutation(staging, mutation)
            packet_hash = tree_sha256(staging)
            if source_hash == packet_hash:
                raise CapabilityError(f"mutation made no byte-level change: {meta['id']}")
            post_results = _run_oracles(
                postconditions, staging, source, f"cases[{index}].oracles.postconditions"
            )
            staging.rename(destination)
        except Exception:
            if staging.exists():
                shutil.rmtree(staging)
            raise
        identity = exact_runtime_identity(evaluator, packet_hash)
        prepared_cases.append(
            {
                **meta,
                "source_packet_sha256": source_hash,
                "packet_path": f"cases/{meta['id']}/packet",
                "packet_sha256": packet_hash,
                "observation_path": f"observations/{meta['id']}.json",
                "runtime_identity": identity,
                "runtime_identity_fingerprint": runtime_fingerprint(identity),
                "oracle_validation": {
                    "passed": True,
                    "preconditions": pre_results,
                    "postconditions": post_results,
                },
            }
        )
    return {
        "schema": PREPARED_SCHEMA,
        "source_manifest_sha256": canonical_hash(manifest),
        "evaluator": evaluator,
        "evaluator_artifacts": evaluator_artifacts,
        "evaluator_identity_fingerprint": evaluator_fingerprint(evaluator),
        "holdout_audit": holdout,
        "cases": prepared_cases,
        "claim_limit": "ORACLE_BACKED_MUTATION_CAPABILITY_ONLY",
        "human_score_validity": "UNAVAILABLE_WITHOUT_HUMAN_CALIBRATION",
    }


def wilson_interval(successes: int, total: int, z: float = 1.959963984540054) -> dict[str, float | None]:
    if total <= 0:
        return {"low": None, "high": None}
    proportion = successes / total
    denominator = 1 + z * z / total
    centre = (proportion + z * z / (2 * total)) / denominator
    margin = z * math.sqrt(proportion * (1 - proportion) / total + z * z / (4 * total * total)) / denominator
    return {"low": max(0.0, centre - margin), "high": min(1.0, centre + margin)}


def binomial_metric(successes: int, total: int) -> dict[str, Any]:
    return {
        "numerator": successes,
        "denominator": total,
        "estimate": successes / total if total else None,
        "wilson_95": wilson_interval(successes, total),
    }


def _grounding_refs(
    observation: dict[str, Any], case: dict[str, Any], root: Path, packet: Path
) -> tuple[bool, set[str], str]:
    binding = observation.get("grounding_receipt")
    if not isinstance(binding, dict) or set(binding) != {"path", "sha256"}:
        raise CapabilityError(f"observation {case['id']} needs a pinned grounding_receipt")
    expected_hash = binding.get("sha256")
    if not _is_sha256(expected_hash):
        raise CapabilityError(f"observation {case['id']} grounding receipt hash is invalid")
    receipt_path = _safe_relative(root, binding.get("path"))
    if not receipt_path.is_file() or file_sha256(receipt_path) != expected_hash:
        raise CapabilityError(f"observation {case['id']} grounding receipt hash mismatch")
    receipt = _read_json(receipt_path)
    if receipt.get("schema_version") != GROUNDING_SCHEMA or receipt.get("role") != case["role"]:
        raise CapabilityError(f"observation {case['id']} grounding receipt schema/role mismatch")
    valid = receipt.get("valid")
    refs = receipt.get("refs")
    errors = receipt.get("errors")
    if not isinstance(valid, bool) or not isinstance(refs, list) or not isinstance(errors, list):
        raise CapabilityError(f"observation {case['id']} grounding receipt has invalid fields")
    if valid and errors:
        raise CapabilityError(f"observation {case['id']} valid grounding receipt cannot contain errors")
    packet_bindings = (("manifest", "manifest.json"), ("context", "context.txt"))
    for field, packet_name in packet_bindings:
        value = receipt.get(field)
        packet_file = packet / packet_name
        if (
            not isinstance(value, dict)
            or not packet_file.is_file()
            or value.get("sha256") != file_sha256(packet_file)
            or not isinstance(value.get("path"), str)
            or not value.get("path")
        ):
            raise CapabilityError(f"observation {case['id']} grounding receipt {field} is not packet-bound")
    verified: set[str] = set()
    for index, ref in enumerate(refs):
        if not isinstance(ref, dict):
            raise CapabilityError(f"observation {case['id']} grounding refs[{index}] must be an object")
        ref_id = _required_text(ref, "ref_id", f"grounding refs[{index}]")
        if ref_id in verified:
            raise CapabilityError(f"observation {case['id']} grounding receipt has duplicate ref_id")
        for hash_field in ("chunk_id", "quote_sha256"):
            if not _is_sha256(ref.get(hash_field)):
                raise CapabilityError(f"grounding refs[{index}].{hash_field} must be a SHA-256")
        _required_text(ref, "resolved_path", f"grounding refs[{index}]")
        for line_field in ("line_start", "line_end", "context_line_start", "context_line_end"):
            line = ref.get(line_field)
            if not isinstance(line, int) or isinstance(line, bool) or line < 1:
                raise CapabilityError(f"grounding refs[{index}].{line_field} must be positive")
        if ref["line_end"] < ref["line_start"] or ref["context_line_end"] < ref["context_line_start"]:
            raise CapabilityError(f"grounding refs[{index}] has a reversed line range")
        verified.add(ref_id)
    return valid, verified, expected_hash


def _load_observation(case: dict[str, Any], root: Path, packet: Path) -> dict[str, Any]:
    path = _safe_relative(root, case.get("observation_path"))
    observation = _read_json(path)
    if observation.get("schema") != OBSERVATION_SCHEMA:
        raise CapabilityError(f"observation {case['id']} has unsupported schema")
    if observation.get("case_id") != case["id"]:
        raise CapabilityError(f"observation case id mismatch for {case['id']}")
    _validate_exact_identity(observation.get("runtime_identity"), case["runtime_identity"], f"observation {case['id']}")
    decision = observation.get("decision")
    if decision not in DECISIONS:
        raise CapabilityError(f"observation {case['id']} has invalid decision: {decision!r}")
    baseline = observation.get("baseline_decision")
    if case["kind"] == "neutral_transform" and baseline not in DECISIONS:
        raise CapabilityError(f"neutral observation {case['id']} needs baseline_decision")
    findings = observation.get("findings")
    if not isinstance(findings, list):
        raise CapabilityError(f"observation {case['id']}.findings must be a list")
    for index, finding in enumerate(findings):
        if not isinstance(finding, dict):
            raise CapabilityError(f"observation {case['id']}.findings[{index}] must be an object")
        _required_text(finding, "mutation_family", f"observation {case['id']}.findings[{index}]")
        _required_text(finding, "ref_id", f"observation {case['id']}.findings[{index}]")
    if decision_class(decision).startswith("BLOCK_") and not findings:
        raise CapabilityError(f"blocking observation {case['id']} needs at least one finding")
    trials = observation.get("position_trials", [])
    if not isinstance(trials, list):
        raise CapabilityError(f"observation {case['id']}.position_trials must be a list")
    seen_trials: set[tuple[str, str]] = set()
    for index, trial in enumerate(trials):
        if not isinstance(trial, dict):
            raise CapabilityError(f"position trial {case['id']}[{index}] must be an object")
        pair_id = _required_text(trial, "pair_id", f"position trial {case['id']}[{index}]")
        orientation, winner = trial.get("orientation"), trial.get("winner")
        if orientation not in {"AB", "BA"} or winner not in {"A", "B", "TIE"}:
            raise CapabilityError(f"invalid position trial for {case['id']}: {trial}")
        key = (pair_id, orientation)
        if key in seen_trials:
            raise CapabilityError(f"duplicate position trial for {case['id']}: {key}")
        seen_trials.add(key)
    receipt_valid, verified_refs, receipt_hash = _grounding_refs(observation, case, root, packet)
    for finding in findings:
        finding["_grounded"] = receipt_valid and finding["ref_id"] in verified_refs
    observation["_grounding_receipt_sha256"] = receipt_hash
    observation["_path"] = str(path)
    observation["_sha256"] = file_sha256(path)
    return observation


def _normalized_winner(orientation: str, winner: str) -> str:
    if winner == "TIE":
        return "TIE"
    if orientation == "AB":
        return "BASE" if winner == "A" else "CANDIDATE"
    return "CANDIDATE" if winner == "A" else "BASE"


def _metrics(rows: Iterable[tuple[dict[str, Any], dict[str, Any]]]) -> dict[str, Any]:
    materialized = list(rows)
    hard = [(case, obs) for case, obs in materialized if case["kind"] == "hard_defect"]
    neutral = [(case, obs) for case, obs in materialized if case["kind"] == "neutral_transform"]
    is_blocking = lambda obs: decision_class(obs["decision"]).startswith("BLOCK_")
    detected_target = lambda case, obs: is_blocking(obs) and any(
        finding["_grounded"] and finding["mutation_family"] == case["mutation_family"]
        for finding in obs["findings"]
    )
    true_positive = sum(detected_target(case, obs) for case, obs in hard)
    # Neutral transforms over an already failing baseline test invariance only;
    # they are not clean negatives for specificity or false-reopen estimates.
    neutral_clean = [(case, obs) for case, obs in neutral if obs["baseline_decision"] == "PASS"]
    true_negative = sum(obs["decision"] == "PASS" for _, obs in neutral_clean)
    false_positive = sum(is_blocking(obs) for _, obs in neutral_clean)
    cited = sum(len(obs["findings"]) for _, obs in materialized)
    grounded = sum(finding["_grounded"] for _, obs in materialized for finding in obs["findings"])

    pair_trials: dict[tuple[str, str], dict[str, str]] = defaultdict(dict)
    a_wins = decisive = 0
    for case, obs in materialized:
        for trial in obs.get("position_trials", []):
            key = (case["id"], trial["pair_id"])
            pair_trials[key][trial["orientation"]] = _normalized_winner(trial["orientation"], trial["winner"])
            if trial["winner"] != "TIE":
                decisive += 1
                a_wins += trial["winner"] == "A"
    complete_pairs = [values for values in pair_trials.values() if set(values) == {"AB", "BA"}]
    flips = sum(values["AB"] != values["BA"] for values in complete_pairs)

    return {
        "sensitivity": binomial_metric(true_positive, len(hard)),
        "specificity": binomial_metric(true_negative, len(neutral_clean)),
        "precision": binomial_metric(true_positive, true_positive + false_positive),
        "neutral_flip_rate": binomial_metric(
            sum(
                decision_class(obs["decision"]) != decision_class(obs["baseline_decision"])
                for _, obs in neutral
            ),
            len(neutral),
        ),
        "position_bias_rate": binomial_metric(flips, len(complete_pairs)),
        "a_selection_rate": binomial_metric(a_wins, decisive),
        "evidence_grounding_rate": binomial_metric(grounded, cited),
        "indeterminate_rate": binomial_metric(
            sum(decision_class(obs["decision"]) == "INDETERMINATE" for _, obs in materialized),
            len(materialized),
        ),
        "false_reopen_rate": binomial_metric(false_positive, len(neutral_clean)),
    }


def evaluate_prepared(prepared: dict[str, Any], root: Path) -> dict[str, Any]:
    if prepared.get("schema") != PREPARED_SCHEMA:
        raise CapabilityError(f"prepared manifest schema must be {PREPARED_SCHEMA}")
    cases = prepared.get("cases")
    if not isinstance(cases, list) or not cases:
        raise CapabilityError("prepared manifest has no cases")
    if prepared.get("claim_limit") != "ORACLE_BACKED_MUTATION_CAPABILITY_ONLY":
        raise CapabilityError("prepared manifest lacks the capability claim limit")
    holdout = _validate_holdout(cases, prepared.get("holdout_audit", {}).get("axes"))
    evaluator = evaluator_identity(prepared.get("evaluator") if isinstance(prepared.get("evaluator"), dict) else {})
    if prepared.get("evaluator_identity_fingerprint") != evaluator_fingerprint(evaluator):
        raise CapabilityError("prepared evaluator identity fingerprint mismatch")
    evaluator_artifacts = prepared.get("evaluator_artifacts")
    if not isinstance(evaluator_artifacts, dict) or set(evaluator_artifacts) != {"prompt", "schema"}:
        raise CapabilityError("prepared manifest lacks prompt/schema artifact receipts")
    for name, hash_field in (("prompt", "prompt_sha256"), ("schema", "schema_sha256")):
        receipt = evaluator_artifacts[name]
        if not isinstance(receipt, dict) or receipt.get("sha256") != evaluator[hash_field]:
            raise CapabilityError(f"prepared {name} artifact receipt mismatch")

    rows: list[tuple[dict[str, Any], dict[str, Any]]] = []
    observation_receipts: list[dict[str, str]] = []
    for index, case in enumerate(cases):
        if not isinstance(case, dict):
            raise CapabilityError(f"prepared cases[{index}] must be an object")
        _case_metadata(case, index)
        validation = case.get("oracle_validation")
        if not isinstance(validation, dict) or validation.get("passed") is not True:
            raise CapabilityError("every capability case must have passed pre/postcondition oracles")
        preconditions = validation.get("preconditions")
        postconditions = validation.get("postconditions")
        if not isinstance(preconditions, list) or not preconditions or not isinstance(postconditions, list) or not postconditions:
            raise CapabilityError(f"prepared case {case['id']} has no complete oracle receipt")
        if any(not isinstance(item, dict) or item.get("passed") is not True for item in preconditions + postconditions):
            raise CapabilityError(f"prepared case {case['id']} contains a failed oracle receipt")
        if case["kind"] == "neutral_transform" and not any(
            item.get("type") in NEUTRAL_INVARIANTS for item in postconditions
        ):
            raise CapabilityError(f"prepared neutral case {case['id']} lacks an equivalence receipt")
        packet = _safe_relative(root, case.get("packet_path"))
        packet_hash = tree_sha256(packet)
        if packet_hash != case.get("packet_sha256"):
            raise CapabilityError(f"prepared packet changed after oracle validation: {case.get('id')}")
        expected_identity = exact_runtime_identity(evaluator, packet_hash)
        if case.get("runtime_identity") != expected_identity:
            raise CapabilityError(f"prepared runtime identity mismatch: {case.get('id')}")
        if case.get("runtime_identity_fingerprint") != runtime_fingerprint(expected_identity):
            raise CapabilityError(f"prepared runtime identity fingerprint mismatch: {case.get('id')}")
        observation = _load_observation(case, root, packet)
        rows.append((case, observation))
        observation_receipts.append(
            {
                "case_id": case["id"],
                "observation_sha256": observation["_sha256"],
                "grounding_receipt_sha256": observation["_grounding_receipt_sha256"],
                "runtime_identity_fingerprint": case["runtime_identity_fingerprint"],
            }
        )

    test_rows = [(case, obs) for case, obs in rows if case["split"] == "test"]
    if not test_rows:
        raise CapabilityError("at least one held-out test case is required")
    grouped: dict[tuple[str, str], list[tuple[dict[str, Any], dict[str, Any]]]] = defaultdict(list)
    for case, observation in test_rows:
        grouped[(case["role"], evaluator_fingerprint(case["runtime_identity"]))].append((case, observation))
    matrix = []
    for (role, fingerprint), group in sorted(grouped.items()):
        matrix.append(
            {
                "role": role,
                "evaluator_identity_fingerprint": fingerprint,
                "scope": {
                    "project_ids": sorted({case["project_id"] for case, _ in group}),
                    "problem_ids": sorted({case["problem_id"] for case, _ in group}),
                    "mutation_families": sorted({case["mutation_family"] for case, _ in group}),
                    "packet_sha256": sorted({case["packet_sha256"] for case, _ in group}),
                },
                "test_cases": len(group),
                "metrics": _metrics(group),
                "permitted_use": "ROLE_ROUTING_AND_SHADOW_ELIGIBILITY_ONLY",
                "truth_claim": "NONE",
            }
        )
    return {
        "schema": REPORT_SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "prepared_manifest_sha256": canonical_hash(prepared),
        "evaluator": evaluator,
        "evaluator_artifacts": evaluator_artifacts,
        "evaluator_identity_fingerprint": evaluator_fingerprint(evaluator),
        "holdout_audit": holdout,
        "case_counts": {
            "total": len(rows),
            "train": sum(case["split"] == "train" for case, _ in rows),
            "test": len(test_rows),
            "hard_defect_test": sum(case["kind"] == "hard_defect" for case, _ in test_rows),
            "neutral_transform_test": sum(case["kind"] == "neutral_transform" for case, _ in test_rows),
        },
        "metrics": _metrics(test_rows),
        "capability_matrix": matrix,
        "observation_receipts": observation_receipts,
        "claim_limit": "ORACLE_BACKED_MUTATION_CAPABILITY_ONLY",
        "human_score_validity": "UNAVAILABLE_WITHOUT_HUMAN_CALIBRATION",
        "award_prediction": "UNAVAILABLE_WITHOUT_HUMAN_CALIBRATION",
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare", help="materialize and oracle-check mutation packets")
    prepare.add_argument("manifest", type=Path)
    prepare.add_argument("--output-dir", required=True, type=Path)
    report = commands.add_parser("report", help="evaluate bound judge observations")
    report.add_argument("prepared_manifest", type=Path)
    report.add_argument("--json-output", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "prepare":
            manifest_path = args.manifest.resolve()
            output_dir = args.output_dir.resolve()
            prepared = prepare_manifest(_read_json(manifest_path), manifest_path.parent, output_dir)
            output = output_dir / "prepared_manifest.json"
            _write_json(output, prepared)
        else:
            manifest_path = args.prepared_manifest.resolve()
            report = evaluate_prepared(_read_json(manifest_path), manifest_path.parent)
            output = args.json_output.resolve()
            _write_json(output, report)
        print(output)
        return 0
    except CapabilityError as exc:
        print(f"capability harness rejected input: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
