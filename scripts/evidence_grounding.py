#!/usr/bin/env python3
"""Verify judge citations against immutable packet chunks and exact quotes."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Iterable


SCHEMA_VERSION = "evidence-grounding-v1"
HARD_ROLE_SCHEMA = "judge-hard-role-v2"
PAPER_ROLE_SCHEMA = "judge-paper-role-v3"
ROLES = ("math", "execution", "paper")
try:
    from scripts.packet_context import (
        GroundingError, SHA256_RE, HEADER_RE, OMITTED_MARKER,
        sha256_bytes, _active_chunks, _read_role_asset, _context_sections,
    )
except ModuleNotFoundError:  # Direct execution from scripts/.
    from packet_context import (
        GroundingError, SHA256_RE, HEADER_RE, OMITTED_MARKER,
        sha256_bytes, _active_chunks, _read_role_asset, _context_sections,
    )


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def _read_bytes(path: Path, code: str, label: str) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise GroundingError(code, f"{label} is unreadable: {path}") from exc


def _decode_object(raw: bytes, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GroundingError(
            "MANIFEST_JSON_INVALID", f"packet manifest JSON is invalid: {label}"
        ) from exc
    if not isinstance(value, dict):
        raise GroundingError(
            "MANIFEST_JSON_INVALID",
            f"packet manifest root must be an object: {label}",
        )
    return value


def _decode_role_payload(raw: bytes, role: str) -> dict[str, Any]:
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise GroundingError(
            "ROLE_OUTPUT_UTF8_INVALID", "role output is not valid UTF-8"
        ) from exc
    if len(lines) < 2:
        raise GroundingError(
            "ROLE_ENVELOPE_INVALID", "role output has no strict verdict envelope"
        )
    header = re.fullmatch(r"VERDICT: ([A-Z_]+)", lines[0])
    if header is None:
        raise GroundingError(
            "ROLE_ENVELOPE_INVALID", "role output has no strict verdict envelope"
        )
    try:
        payload = json.loads("\n".join(lines[1:]))
    except json.JSONDecodeError as exc:
        raise GroundingError("ROLE_OUTPUT_JSON_INVALID", "role output JSON is invalid") from exc
    if not isinstance(payload, dict):
        raise GroundingError(
            "ROLE_OUTPUT_JSON_INVALID", "role output JSON root must be an object"
        )
    if payload.get("verdict") != header.group(1):
        raise GroundingError(
            "ROLE_VERDICT_MISMATCH", "role output header and JSON verdict differ"
        )
    allowed_verdicts = (
        {"PASS", "REVISE", "INDETERMINATE"}
        if role == "paper"
        else {"PASS", "FAIL", "INDETERMINATE"}
    )
    if payload.get("verdict") not in allowed_verdicts:
        raise GroundingError("ROLE_VERDICT_INVALID", f"invalid {role} verdict")
    return payload


def _references(payload: dict[str, Any], role: str) -> Iterable[tuple[str, dict[str, Any]]]:
    expected_schema = PAPER_ROLE_SCHEMA if role == "paper" else HARD_ROLE_SCHEMA
    if payload.get("schema_version") != expected_schema:
        raise GroundingError(
            "ROLE_SCHEMA_MISMATCH", f"{role} requires current schema {expected_schema}"
        )
    if payload.get("role") != role:
        raise GroundingError("ROLE_MISMATCH", "role output and requested role differ")
    if role != "paper":
        evidence = payload.get("evidence")
        if not isinstance(evidence, list):
            raise GroundingError(
                "ROLE_EVIDENCE_INVALID", "hard-role evidence must be an array"
            )
        for index, item in enumerate(evidence):
            if isinstance(item, dict):
                yield f"evidence[{index}]", item
            else:
                raise GroundingError(
                    "ROLE_EVIDENCE_INVALID", f"evidence[{index}] must be an object"
                )
        return

    dimensions = payload.get("dimensions")
    if isinstance(dimensions, dict):
        for dimension, value in dimensions.items():
            evidence = value.get("evidence") if isinstance(value, dict) else None
            if not isinstance(evidence, list):
                raise GroundingError(
                    "ROLE_EVIDENCE_INVALID",
                    f"dimensions.{dimension}.evidence must be an array",
                )
            for index, item in enumerate(evidence):
                if isinstance(item, dict):
                    yield f"dimensions.{dimension}.evidence[{index}]", item
                else:
                    raise GroundingError(
                        "ROLE_EVIDENCE_INVALID",
                        f"dimensions.{dimension}.evidence[{index}] must be an object"
                    )
    elif payload.get("verdict") != "INDETERMINATE":
        raise GroundingError(
            "ROLE_EVIDENCE_INVALID", "scored paper output has no dimensions"
        )
    issues = payload.get("issues")
    if not isinstance(issues, list):
        raise GroundingError("ROLE_EVIDENCE_INVALID", "paper issues must be an array")
    for index, item in enumerate(issues):
        if isinstance(item, dict):
            yield f"issues[{index}]", item
        else:
            raise GroundingError(
                "ROLE_EVIDENCE_INVALID", f"issues[{index}] must be an object"
            )


def _occurrence_count(text: str, quote: str) -> int:
    count = 0
    start = 0
    while True:
        offset = text.find(quote, start)
        if offset < 0:
            return count
        count += 1
        start = offset + 1


def _validate_grounding_payloads(
    *,
    role_output_bytes: bytes | None,
    manifest_bytes: bytes | None,
    context_bytes: bytes | None,
    requested_role: str,
    manifest_label: str,
    context_label: str,
    role_asset_loader: Callable[[str], bytes] | None = None,
    role_output_loader: Callable[[], bytes] | None = None,
    manifest_loader: Callable[[], bytes] | None = None,
    context_loader: Callable[[], bytes] | None = None,
    asset_loader: Callable[[str], bytes] | None = None,
    pdf_verifier: Callable[[bytes, dict], None] | None = None,
) -> dict[str, Any]:
    """Validate already-loaded packet bytes without filesystem access."""

    errors: list[dict[str, str]] = []
    refs: list[dict[str, Any]] = []
    manifest_record: dict[str, Any] = {
        "path": manifest_label,
        "sha256": None,
        "size": None,
    }
    context_record: dict[str, Any] = {
        "path": context_label,
        "sha256": None,
        "size": None,
    }
    try:
        if requested_role not in ROLES:
            raise GroundingError(
                "UNSUPPORTED_ROLE", f"unsupported role: {requested_role}"
            )
        if manifest_bytes is None:
            if manifest_loader is None:
                raise GroundingError(
                    "MANIFEST_UNREADABLE", "packet manifest bytes are unavailable"
                )
            manifest_bytes = manifest_loader()
        manifest = _decode_object(manifest_bytes, label=manifest_label)
        manifest_record.update(
            {
                "sha256": sha256_bytes(manifest_bytes),
                "size": len(manifest_bytes),
            }
        )
        if manifest.get("role") != requested_role:
            raise GroundingError(
                "MANIFEST_ROLE_MISMATCH", "packet manifest role mismatch"
            )
        files = manifest.get("files")
        if not isinstance(files, list):
            raise GroundingError(
                "MANIFEST_FILES_INVALID", "packet manifest files must be an array"
            )

        if context_bytes is None:
            if context_loader is None:
                raise GroundingError(
                    "CONTEXT_UNREADABLE", "packet context bytes are unavailable"
                )
            context_bytes = context_loader()
        actual_context_hash = sha256_bytes(context_bytes)
        actual_context_size = len(context_bytes)
        context_record.update(
            {"sha256": actual_context_hash, "size": actual_context_size}
        )
        declared_context = manifest.get("context")
        if not isinstance(declared_context, dict):
            raise GroundingError(
                "CONTEXT_DECLARATION_INVALID", "packet context declaration is invalid"
            )
        if declared_context.get("sha256") != actual_context_hash:
            raise GroundingError(
                "CONTEXT_HASH_MISMATCH", "packet context hash does not match manifest"
            )
        declared_size = declared_context.get("size")
        if (
            not isinstance(declared_size, int)
            or isinstance(declared_size, bool)
            or declared_size < 0
        ):
            raise GroundingError(
                "CONTEXT_SIZE_INVALID", "packet context size declaration is invalid"
            )
        if declared_size != actual_context_size:
            raise GroundingError(
                "CONTEXT_SIZE_MISMATCH", "packet context size does not match manifest"
            )
        try:
            context_text = context_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise GroundingError(
                "CONTEXT_UTF8_INVALID", "packet context is not valid UTF-8"
            ) from exc

        chunks_by_path, chunks = _active_chunks(files)
        try:
            from scripts.packet_evidence import PacketEvidence
        except ModuleNotFoundError:  # direct script execution
            from packet_evidence import PacketEvidence
        PacketEvidence(files)  # Validate aliases without inventing context chunks.
        sections = _context_sections(context_text, chunks_by_path, asset_loader=role_asset_loader)
        try:
            from scripts.numpy_evidence_view import SUFFIXES as NUMPY_SUFFIXES, verify_capsule
        except ModuleNotFoundError:  # direct script execution
            from numpy_evidence_view import SUFFIXES as NUMPY_SUFFIXES, verify_capsule
        for path, item in chunks_by_path.items():
            if item.get("content_location") == "asset":
                continue
            if "document_review" in item or path.lower().endswith((".xlsx", ".pdf")):
                try:
                    from scripts.document_evidence_view import verify_view as verify_document
                except ModuleNotFoundError:
                    from document_evidence_view import verify_view as verify_document
                if asset_loader is None:
                    raise GroundingError("DOCUMENT_ASSETS_MISSING", "exact packet asset bytes are required")
                try:
                    verify_document(sections[path]["text"], item, asset_loader, pdf_verifier=pdf_verifier)
                except (ValueError, KeyError, OSError) as exc:
                    raise GroundingError("DOCUMENT_REVIEW_INVALID", f"{path}: {exc}") from exc
            if "binary_review" in item or path.lower().endswith(tuple(NUMPY_SUFFIXES)):
                try:
                    verify_capsule(sections[path]["text"], item)
                except ValueError as exc:
                    raise GroundingError("BINARY_REVIEW_INVALID", f"{path}: {exc}") from exc
        if role_output_bytes is None:
            if role_output_loader is None:
                raise GroundingError(
                    "ROLE_OUTPUT_UNREADABLE", "role output bytes are unavailable"
                )
            role_output_bytes = role_output_loader()
        payload = _decode_role_payload(role_output_bytes, requested_role)
        seen_ids: set[str] = set()
        for fallback_id, reference in _references(payload, requested_role):
            raw_ref_id = reference.get("ref_id")
            if not isinstance(raw_ref_id, str) or not raw_ref_id.strip():
                errors.append(
                    {
                        "ref_id": fallback_id,
                        "code": "INVALID_REF_ID",
                        "message": "ref_id is missing",
                    }
                )
                continue
            ref_id = raw_ref_id.strip()
            if ref_id in seen_ids:
                errors.append(
                    {
                        "ref_id": ref_id,
                        "code": "DUPLICATE_REF_ID",
                        "message": "ref_id must be unique",
                    }
                )
                continue
            seen_ids.add(ref_id)
            chunk_id = reference.get("chunk_id")
            quote = reference.get("quote")
            quote_hash = reference.get("quote_sha256")
            if not isinstance(chunk_id, str) or chunk_id not in chunks:
                errors.append(
                    {
                        "ref_id": ref_id,
                        "code": "UNKNOWN_CHUNK",
                        "message": "chunk_id is not in this packet",
                    }
                )
                continue
            if not isinstance(quote, str) or not quote.strip():
                errors.append(
                    {
                        "ref_id": ref_id,
                        "code": "EMPTY_QUOTE",
                        "message": "quote must be non-empty",
                    }
                )
                continue
            actual_quote_hash = sha256_bytes(quote.encode("utf-8"))
            # API judges cannot reliably calculate cryptographic hashes.  The
            # verifier therefore computes the hash from the exact quote and
            # only checks a supplied value when one is present.  This keeps
            # the binding system-owned while still detecting tampering in
            # callers that choose to include the optional field.
            if quote_hash is not None and quote_hash != actual_quote_hash:
                errors.append(
                    {
                        "ref_id": ref_id,
                        "code": "QUOTE_HASH_MISMATCH",
                        "message": "quote_sha256 does not match quote",
                    }
                )
                continue
            chunk = chunks[chunk_id]
            resolved_path = chunk["path"]
            section = sections.get(resolved_path)
            if section is None:
                errors.append(
                    {
                        "ref_id": ref_id,
                        "code": "CHUNK_NOT_IN_CONTEXT",
                        "message": "chunk source is absent from context",
                    }
                )
                continue
            occurrences = _occurrence_count(section["text"], quote)
            if occurrences != 1:
                errors.append(
                    {
                        "ref_id": ref_id,
                        "code": "QUOTE_NOT_UNIQUE" if occurrences > 1 else "QUOTE_NOT_FOUND",
                        "message": f"quote occurrence count in chunk is {occurrences}",
                    }
                )
                continue
            offset = section["text"].find(quote)
            relative_line = section["text"].count("\n", 0, offset)
            line_start = chunk["source_line_start"] + relative_line
            line_end = line_start + quote.count("\n")
            context_line_start = (int(section["context_line_start"]) + relative_line
                                  if section["context_line_start"] is not None else None)
            refs.append(
                {
                    "ref_id": ref_id,
                    "chunk_id": chunk_id,
                    "quote_sha256": actual_quote_hash,
                    "resolved_path": resolved_path,
                    "line_start": line_start,
                    "line_end": line_end,
                    "source_line_start": line_start,
                    "source_line_end": line_end,
                    "context_line_start": context_line_start,
                    "context_line_end": (context_line_start + quote.count("\n")
                                         if context_line_start is not None else None),
                    **({"asset_path": section["asset_path"]} if "asset_path" in section else {}),
                }
            )
    except GroundingError as exc:
        errors.append(
            {"ref_id": "__packet__", "code": exc.code, "message": str(exc)}
        )
    except (OSError, UnicodeError, TypeError, ValueError, KeyError) as exc:
        errors.append(
            {
                "ref_id": "__packet__",
                "code": "GROUNDING_INPUT_INVALID",
                "message": str(exc) or exc.__class__.__name__,
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "role": requested_role,
        "valid": not errors,
        "manifest": manifest_record,
        "context": context_record,
        "refs": refs,
        "errors": errors,
    }


def validate_grounding_bytes(
    role_output_bytes: bytes,
    manifest_bytes: bytes,
    context_bytes: bytes,
    *,
    role: str,
    assets: dict[str, bytes] | None = None,
) -> dict[str, Any]:
    """Validate exact in-memory packet bytes and return a path-free report.

    The byte API is the trusted boundary for durable shadow runtimes.  It does
    not construct, resolve, stat, or open a path.  Fixed logical labels retain
    the existing report shape without introducing host-dependent identity.
    """

    if any(type(value) is not bytes for value in (
        role_output_bytes,
        manifest_bytes,
        context_bytes,
    )):
        return {
            "schema_version": SCHEMA_VERSION,
            "role": role,
            "valid": False,
            "manifest": {"path": "manifest", "sha256": None, "size": None},
            "context": {"path": "context", "sha256": None, "size": None},
            "refs": [],
            "errors": [
                {
                    "ref_id": "__packet__",
                    "code": "GROUNDING_INPUT_INVALID",
                    "message": "role output, manifest, and context must be exact bytes",
                }
            ],
        }
    return _validate_grounding_payloads(
        role_output_bytes=role_output_bytes,
        manifest_bytes=manifest_bytes,
        context_bytes=context_bytes,
        requested_role=role,
        manifest_label="manifest",
        context_label="context",
        role_asset_loader=assets.__getitem__ if assets is not None else None,
        asset_loader=assets.__getitem__ if assets is not None else None,
    )


def validate_grounding(
    role_path: Path,
    manifest_path: Path,
    context_path: Path | None = None,
    *,
    role: str | None = None,
) -> dict[str, Any]:
    """Filesystem adapter preserving the established CLI/library contract."""

    role_asset_root = manifest_path.parent
    manifest_path = manifest_path.resolve()
    context_path = (context_path or manifest_path.with_name("context.txt")).resolve()
    role_path = role_path.resolve()
    requested_role = role or role_path.stem
    try:
        from scripts.document_evidence_view import read_asset, verify_pdf_source
    except ModuleNotFoundError:
        from document_evidence_view import read_asset, verify_pdf_source
    project = manifest_path.parents[2]
    return _validate_grounding_payloads(
        role_output_bytes=None,
        manifest_bytes=None,
        context_bytes=None,
        requested_role=requested_role,
        manifest_label=str(manifest_path),
        context_label=str(context_path),
        role_asset_loader=lambda relative: _read_role_asset(role_asset_root, relative),
        asset_loader=lambda relative: read_asset(project, relative),
        pdf_verifier=verify_pdf_source,
        manifest_loader=lambda: _read_bytes(
            manifest_path, "MANIFEST_UNREADABLE", "packet manifest"
        ),
        context_loader=lambda: _read_bytes(
            context_path, "CONTEXT_UNREADABLE", "packet context"
        ),
        role_output_loader=lambda: _read_bytes(
            role_path, "ROLE_OUTPUT_UNREADABLE", "role output"
        ),
    )


def atomic_write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role-output", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--context")
    parser.add_argument("--role", choices=ROLES)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    report = validate_grounding(
        Path(args.role_output),
        Path(args.manifest),
        Path(args.context) if args.context else None,
        role=args.role,
    )
    try:
        atomic_write_report(Path(args.output), report)
    except OSError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
