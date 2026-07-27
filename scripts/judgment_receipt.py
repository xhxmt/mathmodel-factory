#!/usr/bin/env python3
"""Create and verify immutable receipts for the isolated judge pipeline."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


RECEIPT_SCHEMA = "judgment-receipt-v1"
ROLE_METADATA_SCHEMA = "judge-role-call-v1"
ROLE_CONFIGURATION_SCHEMA = "judge-role-configuration-v1"
CONFIGURATION_GROUP_SCHEMA = "judge-configuration-group-v1"
ROLE_NAMES = ("math", "execution", "paper")
RECEIPT_RELATIVE_PATH = Path("judge_outputs/judgment_receipt.json")
RECEIPT_CONTENT_HASH_FIELD = "content_sha256"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
DERIVED_ARTIFACTS = {
    "aggregate": "judge_outputs/aggregate.json",
    "report": "judge_evaluation.md",
    "objective_evidence": "judge_packets/objective_evidence.json",
    "visual_gate": "judge_outputs/visual_gate.json",
    "decision_route": "judge_outputs/decision_route.json",
}
RECEIPT_KEYS = {
    "schema",
    "created_at",
    "base",
    "input_fingerprint",
    "status",
    "errors",
    "roles",
    "derived_artifacts",
    "decision",
    "configuration_binding",
    RECEIPT_CONTENT_HASH_FIELD,
}
ROLE_RECORD_KEYS = {
    "role",
    "actual_call",
    "rendered_prompt",
    "packet_context",
    "packet_manifest",
    "response",
    "call_metadata",
}
ACTUAL_CALL_KEYS = {
    "registry_model_id",
    "backend",
    "model",
    "transport",
    "configuration_fingerprint",
    "evaluator_configuration_fingerprint",
    "configuration_group",
}
RECORD_KEYS = {"path", "bytes", "sha256"}
DECISION_KEYS = {
    "aggregate_verdict",
    "visual_status",
    "policy_mode",
    "new_decision",
    "effective_decision",
    "automatic_cutover",
}
CONFIGURATION_BINDING_KEYS = {
    "mode",
    "fingerprint",
    "group_fingerprint",
    "role_fingerprints",
}


class ReceiptError(ValueError):
    """Raised when a receipt cannot be trusted or constructed."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_path(project: Path, value: str | Path, *, must_exist: bool = True) -> Path:
    relative = Path(value)
    if relative.is_absolute():
        candidate = relative.resolve()
    else:
        candidate = (project / relative).resolve()
    try:
        candidate.relative_to(project)
    except ValueError as exc:
        raise ReceiptError(f"path escapes project: {value}") from exc
    if must_exist and not candidate.is_file():
        raise ReceiptError(f"required file is missing: {value}")
    return candidate


def _record(project: Path, path: Path) -> dict[str, Any]:
    resolved = _safe_path(project, path)
    return {
        "path": resolved.relative_to(project).as_posix(),
        "bytes": resolved.stat().st_size,
        "sha256": _sha256(resolved),
    }


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReceiptError(f"invalid JSON file: {path}") from exc
    if not isinstance(value, dict):
        raise ReceiptError(f"JSON root must be an object: {path}")
    return value


def _canonical_hash(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _role_configuration_payload(
    *,
    role: str,
    registry_model_id: str,
    backend: str,
    model: str,
    transport: str,
    rendered_prompt_sha256: str,
    packet_context_sha256: str,
    packet_manifest_sha256: str,
    backend_configuration_fingerprint: str,
) -> dict[str, Any]:
    """Return the exact role-call inputs used for configuration hashing.

    The role prompt is intentionally part of this fingerprint.  The three
    judge roles have different rubrics and therefore must not be represented
    by a single prompt-excluding "shared" hash.
    """

    return {
        "schema": ROLE_CONFIGURATION_SCHEMA,
        "role": role,
        "registry_model_id": registry_model_id,
        "backend": backend,
        "model": model,
        "transport": transport,
        "rendered_prompt_sha256": rendered_prompt_sha256,
        "packet_context_sha256": packet_context_sha256,
        "packet_manifest_sha256": packet_manifest_sha256,
        "backend_configuration_fingerprint": backend_configuration_fingerprint,
    }


def _role_configuration_fingerprint(**kwargs: Any) -> str:
    return _canonical_hash(_role_configuration_payload(**kwargs))


def _configuration_group_fingerprint(roles: list[dict[str, Any]]) -> str:
    by_role = {
        str(item.get("role")): item.get("actual_call", {}).get("configuration_fingerprint")
        for item in roles
        if isinstance(item, dict)
    }
    return _canonical_hash(
        {
            "schema": CONFIGURATION_GROUP_SCHEMA,
            "roles": {role: by_role.get(role) for role in ROLE_NAMES},
        }
    )


def _valid_sha256(value: Any) -> bool:
    return isinstance(value, str) and bool(SHA256_RE.fullmatch(value))


def _exact_keys(value: Any, expected: set[str], where: str) -> list[str]:
    """Return schema errors while keeping receipt construction fail-closed."""

    if not isinstance(value, dict):
        return [f"{where} must be an object"]
    errors: list[str] = []
    missing = sorted(expected - set(value))
    extra = sorted(set(value) - expected)
    if missing:
        errors.append(f"{where} missing keys: {','.join(missing)}")
    if extra:
        errors.append(f"{where} has unexpected keys: {','.join(extra)}")
    return errors


def _unsigned_receipt(receipt: dict[str, Any]) -> dict[str, Any]:
    value = dict(receipt)
    value.pop(RECEIPT_CONTENT_HASH_FIELD, None)
    return value


def _receipt_content_hash(receipt: dict[str, Any]) -> str:
    return _canonical_hash(_unsigned_receipt(receipt))


def _validate_objective_evidence(path: Path) -> list[str]:
    """Validate the bundle's self-hash at receipt construction/verification."""

    errors: list[str] = []
    try:
        payload = _read_json(path)
    except ReceiptError as exc:
        return [f"objective evidence invalid: {exc}"]
    if payload.get("schema_version") != "objective-evidence-v1":
        errors.append("objective evidence schema mismatch")
    input_fingerprint = payload.get("input_fingerprint")
    if not _valid_sha256(input_fingerprint):
        errors.append("objective evidence input fingerprint is invalid")
    declared = payload.get("bundle_sha256")
    unsigned = dict(payload)
    unsigned.pop("bundle_sha256", None)
    if not _valid_sha256(declared) or declared != _canonical_hash(unsigned):
        errors.append("objective evidence bundle hash mismatch")
    if payload.get("decision_semantics") != "EVIDENCE_COLLECTION_ONLY":
        errors.append("objective evidence decision semantics are invalid")
    if payload.get("quality_verdict") != "UNAVAILABLE":
        errors.append("objective evidence quality verdict must be UNAVAILABLE")
    return errors


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
            temp_path = Path(handle.name)
        os.replace(temp_path, path)
        temp_path = None
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def annotate_role_metadata(
    project: Path,
    role: str,
    *,
    registry_model_id: str,
    backend: str,
    model: str,
    transport: str,
    prompt_file: str | Path | None = None,
    backend_configuration_fingerprint: str | None = None,
    call_parameters: dict[str, Any] | None = None,
    evaluator_configuration_fingerprint: str | None = None,
    configuration_group: str | None = None,
) -> dict[str, Any]:
    """Normalize actual-call metadata and bind it to the current role output."""

    project = project.resolve()
    if role not in ROLE_NAMES:
        raise ReceiptError(f"unsupported role: {role}")
    if prompt_file is None:
        raise ReceiptError("prompt_file is required for role-call provenance")
    output = _safe_path(project, f"judge_outputs/{role}.md")
    metadata_path = _safe_path(
        project, f"judge_outputs/{role}.md.llm-result.json", must_exist=False
    )
    metadata = _read_json(metadata_path) if metadata_path.is_file() else {}

    recorded_response = metadata.get("response_sha256")
    actual_response = _sha256(output)
    if recorded_response is not None and recorded_response != actual_response:
        raise ReceiptError(f"existing {role} metadata does not match the role output")

    context = _safe_path(project, f"judge_packets/{role}/context.txt")
    manifest = _safe_path(project, f"judge_packets/{role}/manifest.json")
    prompt = _safe_path(project, prompt_file)
    prompt_sha256 = _sha256(prompt)
    recorded_prompt = metadata.get("effective_prompt_sha256")
    if recorded_prompt is not None and recorded_prompt != prompt_sha256:
        raise ReceiptError(f"existing {role} metadata does not match the rendered prompt")
    existing_configuration = metadata.get("configuration_fingerprint")
    if metadata.get("configuration_schema") != ROLE_CONFIGURATION_SCHEMA:
        if existing_configuration is not None:
            if not _valid_sha256(existing_configuration):
                raise ReceiptError(
                    f"existing {role} configuration_fingerprint is not a SHA-256"
                )
            metadata["backend_configuration_fingerprint"] = existing_configuration
    backend_configuration = backend_configuration_fingerprint or metadata.get(
        "backend_configuration_fingerprint"
    )
    if backend_configuration is not None and not _valid_sha256(backend_configuration):
        raise ReceiptError("backend_configuration_fingerprint must be a lowercase SHA-256")
    if backend_configuration is None:
        backend_configuration = _canonical_hash(
            {
                "schema": "judge-backend-configuration-v1",
                "registry_model_id": registry_model_id,
                "backend": backend,
                "model": model,
                "transport": transport,
                "call_parameters": dict(call_parameters or {}),
            }
        )
    metadata["backend_configuration_fingerprint"] = backend_configuration
    context_sha256 = _sha256(context)
    manifest_sha256 = _sha256(manifest)
    role_configuration = _role_configuration_fingerprint(
        role=role,
        registry_model_id=registry_model_id,
        backend=backend,
        model=model,
        transport=transport,
        rendered_prompt_sha256=prompt_sha256,
        packet_context_sha256=context_sha256,
        packet_manifest_sha256=manifest_sha256,
        backend_configuration_fingerprint=backend_configuration,
    )
    metadata.update(
        {
            "receipt_schema": ROLE_METADATA_SCHEMA,
            "configuration_schema": ROLE_CONFIGURATION_SCHEMA,
            "role": role,
            "registry_model_id": registry_model_id,
            "actual_backend": backend,
            "actual_model": model,
            "transport": transport,
            "recorded_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
            "response_sha256": actual_response,
            "response_bytes": output.stat().st_size,
            "packet_context_sha256": context_sha256,
            "packet_manifest_sha256": manifest_sha256,
            "rendered_prompt_sha256": prompt_sha256,
            "rendered_prompt_path": prompt.relative_to(project).as_posix(),
            "configuration_fingerprint": role_configuration,
        }
    )
    if evaluator_configuration_fingerprint is not None:
        if not _valid_sha256(evaluator_configuration_fingerprint):
            raise ReceiptError(
                "evaluator_configuration_fingerprint must be a lowercase SHA-256"
            )
        metadata["evaluator_configuration_fingerprint"] = evaluator_configuration_fingerprint
    if configuration_group is not None:
        raise ReceiptError(
            "configuration_group cannot be assigned per role; use bind-group after all roles"
        )
    _atomic_write_json(metadata_path, metadata)
    return metadata


def _role_receipt(project: Path, role: str) -> tuple[dict[str, Any], list[str]]:
    output_path = _safe_path(project, f"judge_outputs/{role}.md")
    metadata_path = _safe_path(project, f"judge_outputs/{role}.md.llm-result.json")
    metadata = _read_json(metadata_path)
    errors: list[str] = []
    if metadata.get("receipt_schema") != ROLE_METADATA_SCHEMA:
        errors.append(f"{role}: metadata schema is not {ROLE_METADATA_SCHEMA}")
    if metadata.get("configuration_schema") != ROLE_CONFIGURATION_SCHEMA:
        errors.append(f"{role}: configuration schema is not {ROLE_CONFIGURATION_SCHEMA}")
    if metadata.get("role") != role:
        errors.append(f"{role}: metadata role mismatch")
    if metadata.get("response_sha256") != _sha256(output_path):
        errors.append(f"{role}: response hash mismatch")
    context = _safe_path(project, f"judge_packets/{role}/context.txt")
    manifest = _safe_path(project, f"judge_packets/{role}/manifest.json")
    prompt_value = metadata.get("rendered_prompt_path")
    if not isinstance(prompt_value, str) or not prompt_value.strip():
        raise ReceiptError(f"{role}: rendered prompt path is missing")
    rendered_prompt = _safe_path(project, prompt_value)
    if (
        rendered_prompt.relative_to(project).as_posix()
        != f"judge_outputs/{role}.rendered_prompt.txt"
    ):
        errors.append(f"{role}: rendered prompt path is not canonical")
    if metadata.get("rendered_prompt_sha256") != _sha256(rendered_prompt):
        errors.append(f"{role}: rendered prompt hash mismatch")
    if metadata.get("packet_context_sha256") != _sha256(context):
        errors.append(f"{role}: packet context hash mismatch")
    if metadata.get("packet_manifest_sha256") != _sha256(manifest):
        errors.append(f"{role}: packet manifest hash mismatch")
    backend_configuration = metadata.get("backend_configuration_fingerprint")
    if not _valid_sha256(backend_configuration):
        errors.append(f"{role}: backend configuration fingerprint is invalid")
    actual = {
        "registry_model_id": metadata.get("registry_model_id"),
        "backend": metadata.get("actual_backend"),
        "model": metadata.get("actual_model"),
        "transport": metadata.get("transport"),
        "configuration_fingerprint": metadata.get("configuration_fingerprint"),
        "evaluator_configuration_fingerprint": metadata.get(
            "evaluator_configuration_fingerprint"
        ),
        "configuration_group": metadata.get("configuration_group"),
    }
    for field in ("registry_model_id", "backend", "model", "transport"):
        if not isinstance(actual[field], str) or not actual[field].strip():
            errors.append(f"{role}: actual {field} is missing")
    for field in (
        "configuration_fingerprint",
        "evaluator_configuration_fingerprint",
        "configuration_group",
    ):
        value = actual[field]
        if value is not None and not _valid_sha256(value):
            errors.append(f"{role}: {field} must be a lowercase SHA-256")
    if actual["configuration_group"] is not None and metadata.get(
        "configuration_group_schema"
    ) != CONFIGURATION_GROUP_SCHEMA:
        errors.append(f"{role}: configuration group schema is invalid")
    expected_configuration = _role_configuration_fingerprint(
        role=role,
        registry_model_id=str(metadata.get("registry_model_id") or ""),
        backend=str(metadata.get("actual_backend") or ""),
        model=str(metadata.get("actual_model") or ""),
        transport=str(metadata.get("transport") or ""),
        rendered_prompt_sha256=_sha256(rendered_prompt),
        packet_context_sha256=_sha256(context),
        packet_manifest_sha256=_sha256(manifest),
        backend_configuration_fingerprint=str(backend_configuration or ""),
    )
    if actual["configuration_fingerprint"] != expected_configuration:
        errors.append(f"{role}: role configuration fingerprint mismatch")
    return {
        "role": role,
        "actual_call": actual,
        "packet_context": _record(project, context),
        "packet_manifest": _record(project, manifest),
        "response": _record(project, output_path),
        "rendered_prompt": _record(project, rendered_prompt),
        "call_metadata": _record(project, metadata_path),
    }, errors


def bind_configuration_group(project: Path) -> str:
    """Bind the canonical three-role configuration map into every metadata file.

    The group is derived from the already verified role configuration
    fingerprints.  Callers cannot supply an arbitrary common token and thereby
    conceal mixed prompts, packets, transports, or models.
    """

    project = project.resolve()
    roles: list[dict[str, Any]] = []
    metadata_by_role: dict[str, tuple[Path, dict[str, Any]]] = {}
    errors: list[str] = []
    for role in ROLE_NAMES:
        role_record, role_errors = _role_receipt(project, role)
        roles.append(role_record)
        errors.extend(role_errors)
        metadata_path = _safe_path(project, f"judge_outputs/{role}.md.llm-result.json")
        metadata_by_role[role] = (metadata_path, _read_json(metadata_path))
    if errors:
        raise ReceiptError("cannot bind configuration group: " + "; ".join(errors))
    group = _configuration_group_fingerprint(roles)
    for role in ROLE_NAMES:
        metadata_path, metadata = metadata_by_role[role]
        metadata["configuration_group_schema"] = CONFIGURATION_GROUP_SCHEMA
        metadata["configuration_group"] = group
        _atomic_write_json(metadata_path, metadata)
    return group


def _configuration_binding(
    roles: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[str]]:
    """Bind all role calls to one evaluator configuration group.

    ``configuration_fingerprint`` was historically emitted by the API caller
    and may be role-specific.  A new ``evaluator_configuration_fingerprint``
    (or ``configuration_group`` for intentionally heterogeneous judges) is the
    preferred binding.  The legacy field remains accepted only when all three
    roles provide the same non-empty value.  This prevents silently mixing
    calls made under different prompts/models while preserving an explicit,
    auditable escape hatch for heterogeneous role groups.
    """

    errors: list[str] = []
    by_role = {
        item.get("role"): item.get("actual_call", {})
        for item in roles
        if isinstance(item, dict)
    }
    if set(by_role) != set(ROLE_NAMES):
        errors.append("configuration binding requires exactly math, execution, and paper roles")
        return {
            "mode": "INVALID",
            "fingerprint": None,
            "group_fingerprint": None,
            "role_fingerprints": {},
        }, errors

    calls = {role: by_role[role] for role in ROLE_NAMES}
    explicit = {
        role: call.get("evaluator_configuration_fingerprint")
        for role, call in calls.items()
    }
    groups = {role: call.get("configuration_group") for role, call in calls.items()}
    legacy = {role: call.get("configuration_fingerprint") for role, call in calls.items()}

    def all_nonempty(values: dict[str, Any]) -> bool:
        return all(isinstance(value, str) and bool(value.strip()) for value in values.values())

    if any(value is not None for value in explicit.values()) and not any(
        value is not None for value in groups.values()
    ):
        if not all_nonempty(explicit):
            errors.append(
                "evaluator_configuration_fingerprint must be present for every role"
            )
        elif len(set(explicit.values())) != 1:
            errors.append("roles use inconsistent evaluator configuration fingerprints")
        role_fingerprints = {
            call.get("configuration_fingerprint") for call in calls.values()
        }
        if len(role_fingerprints) > 1:
            errors.append(
                "shared evaluator fingerprint cannot mask differing role configurations; "
                "bind an explicit heterogeneous configuration group"
            )
        return {
            "mode": "SHARED_EVALUATOR",
            "fingerprint": next(iter(explicit.values()), None),
            "group_fingerprint": next(iter(explicit.values()), None),
            "role_fingerprints": dict(explicit),
        }, errors

    if any(value is not None for value in groups.values()):
        if not all_nonempty(groups):
            errors.append("configuration_group must be present for every role")
        elif len(set(groups.values())) != 1:
            errors.append("roles use inconsistent configuration groups")
        expected_group = _configuration_group_fingerprint(roles)
        if all_nonempty(groups) and next(iter(groups.values()), None) != expected_group:
            errors.append("configuration group fingerprint does not match role configurations")
        if not all_nonempty(legacy):
            errors.append(
                "heterogeneous configuration groups require a role configuration fingerprint"
            )
        return {
            "mode": "HETEROGENEOUS_GROUP",
            "fingerprint": None,
            "group_fingerprint": next(iter(groups.values()), None),
            "role_fingerprints": dict(legacy),
        }, errors

    if not all_nonempty(legacy):
        errors.append("configuration_fingerprint is missing for one or more roles")
        return {
            "mode": "INVALID",
            "fingerprint": None,
            "group_fingerprint": None,
            "role_fingerprints": dict(legacy),
        }, errors
    if len(set(legacy.values())) != 1:
        errors.append("roles use inconsistent configuration fingerprints")
    value = next(iter(legacy.values()), None)
    return {
        "mode": "LEGACY_SHARED",
        "fingerprint": value,
        "group_fingerprint": value,
        "role_fingerprints": dict(legacy),
    }, errors


def _record_schema_errors(
    project: Path,
    record: Any,
    *,
    label: str,
    expected_path: str | None = None,
    verify_content: bool = True,
) -> list[str]:
    """Validate a receipt file record and, optionally, its current bytes."""

    errors = _exact_keys(record, RECORD_KEYS, label)
    if errors:
        return errors
    path_value = record.get("path")
    if not isinstance(path_value, str) or not path_value or Path(path_value).is_absolute():
        return [f"{label}.path is invalid"]
    if expected_path is not None and path_value != expected_path:
        errors.append(f"{label}.path must be {expected_path}")
    bytes_value = record.get("bytes")
    if not isinstance(bytes_value, int) or isinstance(bytes_value, bool) or bytes_value < 0:
        errors.append(f"{label}.bytes is invalid")
    if not _valid_sha256(record.get("sha256")):
        errors.append(f"{label}.sha256 must be a lowercase SHA-256")
    try:
        artifact = _safe_path(project, path_value)
    except ReceiptError as exc:
        errors.append(f"{label}: {exc}")
        return errors
    if verify_content and not errors:
        if record.get("bytes") != artifact.stat().st_size or record.get("sha256") != _sha256(artifact):
            errors.append(f"receipt artifact changed: {path_value}")
    return errors


def _validate_aggregate_contract(
    aggregate: dict[str, Any],
) -> list[str]:
    """Check the aggregate envelope before its verdict enters the receipt."""

    errors: list[str] = []
    if aggregate.get("schema_version") != "judge-aggregate-v3":
        errors.append("aggregate schema_version is not judge-aggregate-v3")
    roles_raw = aggregate.get("roles")
    role_statuses = aggregate.get("role_statuses")
    if not isinstance(roles_raw, list) or len(roles_raw) != len(ROLE_NAMES):
        errors.append("aggregate roles must contain exactly three role records")
        roles_raw = []
    if not isinstance(role_statuses, dict) or set(role_statuses) != set(ROLE_NAMES):
        errors.append("aggregate role_statuses must contain exactly math, execution, and paper")
        role_statuses = {}
    by_role: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(roles_raw):
        if not isinstance(item, dict):
            errors.append(f"aggregate roles[{index}] is not an object")
            continue
        role = item.get("role")
        if role not in ROLE_NAMES or role in by_role:
            errors.append(f"aggregate roles[{index}] has an invalid or duplicate role")
            continue
        by_role[role] = item
        status = item.get("status")
        verdict = item.get("verdict")
        if status not in {"PASS", "FAIL", "REVISE", "INDETERMINATE", "LEGACY_UNVERIFIED"}:
            errors.append(f"aggregate role {role} status is invalid")
        if verdict is not None and verdict not in {"PASS", "FAIL", "REVISE", "INDETERMINATE"}:
            errors.append(f"aggregate role {role} verdict is invalid")
        if isinstance(role_statuses, dict) and role in role_statuses and role_statuses[role] != status:
            errors.append(f"aggregate role_statuses disagrees with role {role}")
        if status in {"PASS", "FAIL", "REVISE"} and verdict != status:
            errors.append(f"aggregate role {role} status/verdict mismatch")
        if status in {"INDETERMINATE", "LEGACY_UNVERIFIED"} and verdict not in {
            None,
            "INDETERMINATE",
        }:
            errors.append(f"aggregate role {role} indeterminate status has a decision verdict")
    if set(by_role) != set(ROLE_NAMES):
        errors.append("aggregate roles are not the canonical three roles")

    expected_verdict: str | None = None
    statuses = [by_role.get(role, {}).get("status") for role in ROLE_NAMES]
    if any(status == "FAIL" for status in statuses[:2]):
        expected_verdict = "REOPEN_REVISION_MODEL"
    elif any(status in {"INDETERMINATE", "LEGACY_UNVERIFIED"} for status in statuses):
        expected_verdict = "INDETERMINATE_REVIEW"
    elif statuses[2] == "REVISE":
        expected_verdict = "REOPEN_REVISION_TEXT"
    elif all(status == "PASS" for status in statuses):
        expected_verdict = "PASS"
    if expected_verdict is not None and aggregate.get("verdict") != expected_verdict:
        errors.append("aggregate verdict is inconsistent with role statuses")
    expected_status = {
        "PASS": "PASS",
        "REOPEN_REVISION_MODEL": "FAIL",
        "REOPEN_REVISION_TEXT": "REVISE",
        "INDETERMINATE_REVIEW": "INDETERMINATE",
    }.get(expected_verdict)
    if expected_status is not None and aggregate.get("status") != expected_status:
        errors.append("aggregate status is inconsistent with role statuses")
    score_available = aggregate.get("score_available")
    if not isinstance(score_available, bool):
        errors.append("aggregate score_available must be boolean")
    if score_available is True and aggregate.get("overall_score") is None:
        errors.append("aggregate score_available is true but overall_score is missing")
    return errors


def _validate_manifest_bindings(
    project: Path,
    roles: list[dict[str, Any]],
    objective_record: dict[str, Any],
) -> list[str]:
    """Ensure every packet manifest points at the exact objective bundle."""

    errors: list[str] = []
    expected_objective = {
        key: objective_record.get(key)
        for key in ("path", "bytes", "sha256")
    }
    for role_record in roles:
        role = role_record.get("role")
        manifest_record = role_record.get("packet_manifest")
        context_record = role_record.get("packet_context")
        if not isinstance(manifest_record, dict) or not isinstance(context_record, dict):
            errors.append(f"{role}: packet records are invalid")
            continue
        try:
            manifest_path = _safe_path(project, manifest_record.get("path"))
            manifest = _read_json(manifest_path)
        except (ReceiptError, TypeError) as exc:
            errors.append(f"{role}: packet manifest cannot be read: {exc}")
            continue
        if manifest.get("role") != role:
            errors.append(f"{role}: packet manifest role mismatch")
        packet_fingerprint = manifest.get("packet_fingerprint")
        if not _valid_sha256(packet_fingerprint):
            errors.append(f"{role}: packet manifest fingerprint is invalid")
        context = manifest.get("context")
        if not isinstance(context, dict) or context.get("sha256") != context_record.get("sha256"):
            errors.append(f"{role}: packet context is not bound to manifest")
        manifest_objective = manifest.get("objective_evidence")
        if not isinstance(manifest_objective, dict):
            errors.append(f"{role}: packet manifest objective evidence is missing")
            continue
        actual_objective = {
            "path": manifest_objective.get("path"),
            "bytes": manifest_objective.get("bytes", manifest_objective.get("size")),
            "sha256": manifest_objective.get("sha256"),
        }
        if actual_objective != expected_objective:
            errors.append(f"{role}: packet manifest objective evidence mismatch")
    return errors


def _validate_routing_contract(
    aggregate: dict[str, Any],
    visual: dict[str, Any],
    route: dict[str, Any],
) -> list[str]:
    """Recompute the route so a hand-authored decision cannot enter a receipt."""

    errors: list[str] = []
    if visual.get("schema") != "pdf-visual-gate-v1":
        errors.append("visual gate schema is not pdf-visual-gate-v1")
    if route.get("schema") != "judge-decision-route-v1":
        errors.append("decision route schema is not judge-decision-route-v1")
    policy_mode = route.get("policy_mode")
    if policy_mode not in {"shadow", "enforce"}:
        errors.append("decision route policy_mode is invalid")
        return errors
    try:
        try:
            from scripts.judge_decision_router import route_decision
        except ModuleNotFoundError:  # direct execution from scripts/
            from judge_decision_router import route_decision  # type: ignore

        expected = route_decision(
            aggregate,
            visual,
            policy_mode=policy_mode,
        )
    except (ImportError, ValueError) as exc:
        errors.append(f"decision route cannot be recomputed: {exc}")
        return errors
    for field in (
        "legacy_decision",
        "new_decision",
        "effective_decision",
        "decision_changed",
        "reasons",
        "score_policy",
        "human_alignment",
        "award_prediction",
        "automatic_cutover",
    ):
        if route.get(field) != expected.get(field):
            errors.append(f"decision route {field} does not match recomputation")
    return errors


def build_receipt(
    project: Path,
    base: str | None = None,
    *,
    input_fingerprint: str | None = None,
    output: Path | None = None,
) -> dict[str, Any]:
    project = project.resolve()
    resolved_base = base or project.name
    if input_fingerprint is None:
        from scripts.submission_fingerprint import submission_fingerprint

        input_fingerprint = submission_fingerprint(project, resolved_base)
    if not _valid_sha256(input_fingerprint):
        raise ReceiptError("input_fingerprint must be a SHA-256")

    roles: list[dict[str, Any]] = []
    errors: list[str] = []
    for role in ROLE_NAMES:
        try:
            role_record, role_errors = _role_receipt(project, role)
            roles.append(role_record)
            errors.extend(role_errors)
        except ReceiptError as exc:
            errors.append(str(exc))

    derived: dict[str, dict[str, Any]] = {}
    for name, relative in DERIVED_ARTIFACTS.items():
        try:
            artifact_path = _safe_path(project, relative)
            derived[name] = _record(project, artifact_path)
            if name == "objective_evidence":
                errors.extend(_validate_objective_evidence(artifact_path))
        except ReceiptError as exc:
            errors.append(str(exc))

    decision: dict[str, Any] = {}
    try:
        aggregate_value = _read_json(project / DERIVED_ARTIFACTS["aggregate"])
        visual_value = _read_json(project / DERIVED_ARTIFACTS["visual_gate"])
        route_value = _read_json(project / DERIVED_ARTIFACTS["decision_route"])
        errors.extend(_validate_aggregate_contract(aggregate_value))
        errors.extend(
            _validate_routing_contract(aggregate_value, visual_value, route_value)
        )
        if "objective_evidence" in derived:
            errors.extend(
                _validate_manifest_bindings(
                    project,
                    roles,
                    derived["objective_evidence"],
                )
            )
        decision = {
            "aggregate_verdict": aggregate_value.get("verdict"),
            "visual_status": visual_value.get("status"),
            "policy_mode": route_value.get("policy_mode"),
            "new_decision": route_value.get("new_decision"),
            "effective_decision": route_value.get("effective_decision"),
            "automatic_cutover": route_value.get("automatic_cutover"),
        }
        if route_value.get("legacy_decision") != aggregate_value.get("verdict"):
            errors.append("decision route is not bound to the aggregate verdict")
        if visual_value.get("status") not in {"PASS", "FAIL", "INDETERMINATE"}:
            errors.append("visual gate status is invalid")
        if route_value.get("effective_decision") not in {
            "PASS",
            "REOPEN_REVISION_MODEL",
            "REOPEN_REVISION_TEXT",
            "PACKET_REBUILD",
            "INFRA_RETRY",
            "INDETERMINATE_REVIEW",
        }:
            errors.append("effective decision is invalid")
        if route_value.get("automatic_cutover") is not False:
            errors.append("decision route permits automatic cutover")
    except ReceiptError as exc:
        errors.append(str(exc))

    configuration_binding, configuration_errors = _configuration_binding(roles)
    errors.extend(configuration_errors)
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "created_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "base": resolved_base,
        "input_fingerprint": input_fingerprint,
        "status": "VALID" if not errors else "INVALID",
        "errors": errors,
        "roles": roles,
        "derived_artifacts": derived,
        "decision": decision,
        "configuration_binding": configuration_binding,
    }
    receipt[RECEIPT_CONTENT_HASH_FIELD] = _receipt_content_hash(receipt)
    target = output or (project / RECEIPT_RELATIVE_PATH)
    target = _safe_path(project, target, must_exist=False)
    _atomic_write_json(target, receipt)
    return receipt


def verify_receipt(
    project: Path,
    base: str | None = None,
    *,
    expected_input_fingerprint: str | None = None,
    require_pass: bool = False,
) -> tuple[bool, list[str]]:
    project = project.resolve()
    resolved_base = base or project.name
    path = _safe_path(project, RECEIPT_RELATIVE_PATH, must_exist=False)
    if not path.is_file():
        return False, ["judgment receipt is missing"]
    try:
        receipt = _read_json(path)
    except ReceiptError as exc:
        return False, [str(exc)]
    errors: list[str] = []
    errors.extend(_exact_keys(receipt, RECEIPT_KEYS, "judgment receipt"))
    if receipt.get("schema") != RECEIPT_SCHEMA:
        errors.append("judgment receipt schema mismatch")
    if receipt.get("base") != resolved_base:
        errors.append("judgment receipt base mismatch")
    if not _valid_sha256(receipt.get("input_fingerprint")):
        errors.append("judgment receipt input fingerprint is invalid")
    content_hash = receipt.get(RECEIPT_CONTENT_HASH_FIELD)
    if not _valid_sha256(content_hash) or content_hash != _receipt_content_hash(receipt):
        errors.append("judgment receipt content hash mismatch")
    if receipt.get("status") != "VALID" or receipt.get("errors") != []:
        errors.append("judgment receipt was not created as VALID")
    if not isinstance(receipt.get("errors"), list) or any(
        not isinstance(item, str) for item in receipt.get("errors", [])
    ):
        errors.append("judgment receipt errors must be a string array")
    if expected_input_fingerprint is not None and receipt.get("input_fingerprint") != expected_input_fingerprint:
        errors.append("judgment receipt input fingerprint mismatch")

    roles_raw = receipt.get("roles")
    roles: list[dict[str, Any]] = []
    if not isinstance(roles_raw, list) or len(roles_raw) != len(ROLE_NAMES):
        errors.append("judgment receipt roles must contain exactly three records")
    else:
        seen_roles: set[str] = set()
        for index, role in enumerate(roles_raw):
            errors.extend(_exact_keys(role, ROLE_RECORD_KEYS, f"roles[{index}]"))
            if not isinstance(role, dict):
                continue
            role_name = role.get("role")
            if role_name not in ROLE_NAMES or role_name in seen_roles:
                errors.append(f"roles[{index}] has an invalid or duplicate role")
            else:
                seen_roles.add(role_name)
                roles.append(role)
            actual_call = role.get("actual_call")
            errors.extend(_exact_keys(actual_call, ACTUAL_CALL_KEYS, f"roles[{index}].actual_call"))
            for key in ("registry_model_id", "backend", "model", "transport"):
                if not isinstance(actual_call, dict) or not isinstance(actual_call.get(key), str) or not actual_call.get(key, "").strip():
                    errors.append(f"roles[{index}].actual_call.{key} is invalid")
            if isinstance(actual_call, dict):
                for key in (
                    "configuration_fingerprint",
                    "evaluator_configuration_fingerprint",
                    "configuration_group",
                ):
                    value = actual_call.get(key)
                    if value is not None and (not isinstance(value, str) or not value.strip()):
                        errors.append(f"roles[{index}].actual_call.{key} is invalid")
            expected_role_paths = (
                {
                    "packet_context": f"judge_packets/{role_name}/context.txt",
                    "packet_manifest": f"judge_packets/{role_name}/manifest.json",
                    "response": f"judge_outputs/{role_name}.md",
                    "rendered_prompt": f"judge_outputs/{role_name}.rendered_prompt.txt",
                    "call_metadata": f"judge_outputs/{role_name}.md.llm-result.json",
                }
                if role_name in ROLE_NAMES
                else {}
            )
            for key in (
                "rendered_prompt",
                "packet_context",
                "packet_manifest",
                "response",
                "call_metadata",
            ):
                errors.extend(
                    _record_schema_errors(
                        project,
                        role.get(key),
                        label=f"roles[{index}].{key}",
                        expected_path=expected_role_paths.get(key),
                    )
                )
            if role_name in ROLE_NAMES:
                try:
                    current_role, role_errors = _role_receipt(project, role_name)
                    errors.extend(role_errors)
                    if role != current_role:
                        errors.append(f"roles[{index}] is stale for current role artifacts")
                except ReceiptError as exc:
                    errors.append(f"roles[{index}] cannot be recomputed: {exc}")
        if seen_roles != set(ROLE_NAMES):
            errors.append("judgment receipt roles are not the canonical three roles")
        if len(roles) == len(ROLE_NAMES):
            expected_binding, config_errors = _configuration_binding(roles)
            errors.extend(config_errors)
            binding = receipt.get("configuration_binding")
            errors.extend(
                _exact_keys(
                    binding,
                    CONFIGURATION_BINDING_KEYS,
                    "judgment receipt configuration_binding",
                )
            )
            if binding != expected_binding:
                errors.append("judgment receipt configuration binding is stale")

    decision = receipt.get("decision")
    errors.extend(_exact_keys(decision, DECISION_KEYS, "judgment receipt decision"))
    if not isinstance(decision, dict):
        errors.append("judgment receipt decision is invalid")
    elif require_pass and decision.get("effective_decision") != "PASS":
        errors.append("judgment receipt does not approve delivery")

    derived = receipt.get("derived_artifacts")
    if not isinstance(derived, dict) or set(derived) != set(DERIVED_ARTIFACTS):
        errors.append("judgment receipt derived_artifacts is invalid")

    seen_paths: set[str] = set()
    if isinstance(derived, dict):
        for name, relative in DERIVED_ARTIFACTS.items():
            record = derived.get(name)
            errors.extend(
                _record_schema_errors(
                    project,
                    record,
                    label=f"derived_artifacts.{name}",
                    expected_path=relative,
                )
            )
            if isinstance(record, dict) and isinstance(record.get("path"), str):
                if record["path"] in seen_paths:
                    errors.append(f"receipt artifact path is duplicated: {record['path']}")
                seen_paths.add(record["path"])
        objective_record = derived.get("objective_evidence")
        if isinstance(objective_record, dict):
            try:
                objective_path = _safe_path(project, objective_record.get("path"))
                errors.extend(_validate_objective_evidence(objective_path))
            except (ReceiptError, TypeError) as exc:
                errors.append(f"objective evidence cannot be read: {exc}")
            if roles:
                errors.extend(_validate_manifest_bindings(project, roles, objective_record))

    # Recompute the aggregate contract from the bytes represented by the
    # receipt; a tampered role_statuses map or route can never remain VALID.
    if isinstance(derived, dict) and isinstance(derived.get("aggregate"), dict):
        try:
            aggregate_path = _safe_path(project, derived["aggregate"].get("path"))
            aggregate_value = _read_json(aggregate_path)
            errors.extend(_validate_aggregate_contract(aggregate_value))
            if isinstance(decision, dict):
                if decision.get("aggregate_verdict") != aggregate_value.get("verdict"):
                    errors.append("receipt decision aggregate verdict is stale")
        except (ReceiptError, TypeError) as exc:
            errors.append(f"aggregate cannot be read: {exc}")

    # Ensure no record points at the receipt itself or at an untracked path.
    for role in roles:
        for key in (
            "rendered_prompt",
            "packet_context",
            "packet_manifest",
            "response",
            "call_metadata",
        ):
            record = role.get(key)
            if isinstance(record, dict) and isinstance(record.get("path"), str):
                if record["path"] in seen_paths:
                    errors.append(f"receipt artifact path is duplicated: {record['path']}")
                seen_paths.add(record["path"])
                if record["path"] == RECEIPT_RELATIVE_PATH.as_posix():
                    errors.append("receipt cannot include itself as a bound artifact")
        metadata_record = role.get("call_metadata")
        if isinstance(metadata_record, dict):
            try:
                metadata_path = _safe_path(project, metadata_record.get("path"))
                metadata = _read_json(metadata_path)
                expected_actual_call = {
                    "registry_model_id": metadata.get("registry_model_id"),
                    "backend": metadata.get("actual_backend"),
                    "model": metadata.get("actual_model"),
                    "transport": metadata.get("transport"),
                    "configuration_fingerprint": metadata.get("configuration_fingerprint"),
                    "evaluator_configuration_fingerprint": metadata.get(
                        "evaluator_configuration_fingerprint"
                    ),
                    "configuration_group": metadata.get("configuration_group"),
                }
                if role.get("actual_call") != expected_actual_call:
                    errors.append(f"{role.get('role')}: actual call disagrees with metadata")
                prompt_record = role.get("rendered_prompt")
                if not isinstance(prompt_record, dict):
                    errors.append(f"{role.get('role')}: rendered prompt record is invalid")
                elif (
                    metadata.get("rendered_prompt_path") != prompt_record.get("path")
                    or metadata.get("rendered_prompt_sha256") != prompt_record.get("sha256")
                ):
                    errors.append(
                        f"{role.get('role')}: rendered prompt disagrees with metadata"
                    )
            except (ReceiptError, TypeError) as exc:
                errors.append(f"{role.get('role')}: call metadata cannot be read: {exc}")

    if isinstance(derived, dict) and isinstance(decision, dict):
        try:
            visual_value = _read_json(
                _safe_path(project, derived["visual_gate"].get("path"))
            )
            route_value = _read_json(
                _safe_path(project, derived["decision_route"].get("path"))
            )
            aggregate_value = _read_json(
                _safe_path(project, derived["aggregate"].get("path"))
            )
            errors.extend(
                _validate_routing_contract(aggregate_value, visual_value, route_value)
            )
            expected_decision = {
                "aggregate_verdict": aggregate_value.get("verdict"),
                "visual_status": visual_value.get("status"),
                "policy_mode": route_value.get("policy_mode"),
                "new_decision": route_value.get("new_decision"),
                "effective_decision": route_value.get("effective_decision"),
                "automatic_cutover": route_value.get("automatic_cutover"),
            }
            if decision != expected_decision:
                errors.append("judgment receipt decision disagrees with routed artifacts")
            if route_value.get("legacy_decision") != aggregate_value.get("verdict"):
                errors.append("decision route is not bound to the aggregate verdict")
            if visual_value.get("status") not in {"PASS", "FAIL", "INDETERMINATE"}:
                errors.append("visual gate status is invalid")
            if route_value.get("automatic_cutover") is not False:
                errors.append("decision route permits automatic cutover")
        except (ReceiptError, KeyError, TypeError) as exc:
            errors.append(f"decision artifacts cannot be read: {exc}")
    return not errors, errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    annotate = subparsers.add_parser("annotate-role")
    annotate.add_argument("project")
    annotate.add_argument("--role", required=True, choices=ROLE_NAMES)
    annotate.add_argument("--registry-model-id", required=True)
    annotate.add_argument("--backend", required=True)
    annotate.add_argument("--model", required=True)
    annotate.add_argument("--transport", required=True)
    annotate.add_argument("--prompt-file", required=True)
    annotate.add_argument("--backend-configuration-fingerprint")
    annotate.add_argument("--effort")
    annotate.add_argument("--timeout-seconds", type=int)
    annotate.add_argument("--evaluator-configuration-fingerprint")
    annotate.add_argument("--configuration-group")

    bind_group = subparsers.add_parser("bind-group")
    bind_group.add_argument("project")

    build = subparsers.add_parser("build")
    build.add_argument("project")
    build.add_argument("--base")
    build.add_argument("--input-fingerprint")
    build.add_argument("--output")

    verify = subparsers.add_parser("verify")
    verify.add_argument("project")
    verify.add_argument("--base")
    verify.add_argument("--input-fingerprint")
    verify.add_argument("--require-pass", action="store_true")

    args = parser.parse_args()
    try:
        if args.command == "annotate-role":
            value = annotate_role_metadata(
                Path(args.project),
                args.role,
                registry_model_id=args.registry_model_id,
                backend=args.backend,
                model=args.model,
                transport=args.transport,
                prompt_file=args.prompt_file,
                backend_configuration_fingerprint=args.backend_configuration_fingerprint,
                call_parameters={
                    key: value
                    for key, value in {
                        "effort": args.effort,
                        "timeout_seconds": args.timeout_seconds,
                    }.items()
                    if value is not None
                },
                evaluator_configuration_fingerprint=args.evaluator_configuration_fingerprint,
                configuration_group=args.configuration_group,
            )
            print(json.dumps(value, ensure_ascii=False, indent=2))
            return 0
        if args.command == "bind-group":
            print(bind_configuration_group(Path(args.project)))
            return 0
        if args.command == "build":
            receipt = build_receipt(
                Path(args.project),
                args.base,
                input_fingerprint=args.input_fingerprint,
                output=Path(args.output) if args.output else None,
            )
            print(json.dumps(receipt, ensure_ascii=False, indent=2))
            return 0 if receipt["status"] == "VALID" else 1
        ok, errors = verify_receipt(
            Path(args.project),
            args.base,
            expected_input_fingerprint=args.input_fingerprint,
            require_pass=args.require_pass,
        )
        if not ok:
            print("; ".join(errors), file=sys.stderr)
        return 0 if ok else 1
    except (OSError, ReceiptError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
