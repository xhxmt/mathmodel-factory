#!/usr/bin/env python3
"""Verify judge citations against immutable packet chunks and exact quotes."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = "evidence-grounding-v1"
HARD_ROLE_SCHEMA = "judge-hard-role-v2"
PAPER_ROLE_SCHEMA = "judge-paper-role-v3"
ROLES = ("math", "execution", "paper")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
HEADER_RE = re.compile(r"\n----- FILE: ([^\n]+) -----\n")


class GroundingError(ValueError):
    """A packet or role envelope cannot be verified."""


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def _read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GroundingError(f"invalid JSON: {path}") from exc
    if not isinstance(value, dict):
        raise GroundingError(f"JSON root must be an object: {path}")
    return value


def _role_payload(path: Path) -> dict[str, Any]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise GroundingError(f"role output is unreadable: {path}") from exc
    if len(lines) < 2 or not lines[0].startswith("VERDICT: "):
        raise GroundingError("role output has no strict verdict envelope")
    try:
        payload = json.loads("\n".join(lines[1:]))
    except json.JSONDecodeError as exc:
        raise GroundingError("role output JSON is invalid") from exc
    if not isinstance(payload, dict):
        raise GroundingError("role output JSON root must be an object")
    return payload


def _references(payload: dict[str, Any], role: str) -> Iterable[tuple[str, dict[str, Any]]]:
    expected_schema = PAPER_ROLE_SCHEMA if role == "paper" else HARD_ROLE_SCHEMA
    if payload.get("schema_version") != expected_schema:
        raise GroundingError(f"{role} requires current schema {expected_schema}")
    if payload.get("role") != role:
        raise GroundingError("role output and requested role differ")
    if role != "paper":
        evidence = payload.get("evidence")
        if not isinstance(evidence, list):
            raise GroundingError("hard-role evidence must be an array")
        for index, item in enumerate(evidence):
            if isinstance(item, dict):
                yield f"evidence[{index}]", item
            else:
                raise GroundingError(f"evidence[{index}] must be an object")
        return

    dimensions = payload.get("dimensions")
    if isinstance(dimensions, dict):
        for dimension, value in dimensions.items():
            evidence = value.get("evidence") if isinstance(value, dict) else None
            if not isinstance(evidence, list):
                raise GroundingError(f"dimensions.{dimension}.evidence must be an array")
            for index, item in enumerate(evidence):
                if isinstance(item, dict):
                    yield f"dimensions.{dimension}.evidence[{index}]", item
                else:
                    raise GroundingError(
                        f"dimensions.{dimension}.evidence[{index}] must be an object"
                    )
    elif payload.get("verdict") != "INDETERMINATE":
        raise GroundingError("scored paper output has no dimensions")
    issues = payload.get("issues")
    if not isinstance(issues, list):
        raise GroundingError("paper issues must be an array")
    for index, item in enumerate(issues):
        if isinstance(item, dict):
            yield f"issues[{index}]", item
        else:
            raise GroundingError(f"issues[{index}] must be an object")


def _context_sections(text: str, files: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    matches = list(HEADER_RE.finditer(text))
    expected_by_path = {
        item.get("path"): item
        for item in files
        if isinstance(item, dict) and item.get("status") in {"included", "truncated"}
    }
    sections: dict[str, dict[str, Any]] = {}
    for index, match in enumerate(matches):
        path = match.group(1)
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        omitted = text.find("\n----- SOME SELECTED FILES OMITTED", match.end(), end)
        if omitted >= 0:
            end = omitted
        raw = text[match.end() : end]
        item = expected_by_path.get(path)
        if not isinstance(item, dict):
            continue
        expected_hash = item.get("included_sha256")
        candidates = [raw]
        if raw.endswith("\n"):
            candidates.append(raw[:-1])
        if raw.endswith("\n\n"):
            candidates.append(raw[:-2])
        content = next(
            (candidate for candidate in candidates if sha256_bytes(candidate.encode("utf-8")) == expected_hash),
            None,
        )
        if content is None:
            raise GroundingError(f"context section hash does not match manifest: {path}")
        sections[path] = {
            "text": content,
            "context_line_start": text.count("\n", 0, match.end()) + 1,
        }
    missing = sorted(set(expected_by_path) - set(sections))
    if missing:
        raise GroundingError("context omits manifest chunks: " + ", ".join(missing))
    return sections


def validate_grounding(
    role_path: Path,
    manifest_path: Path,
    context_path: Path | None = None,
    *,
    role: str | None = None,
) -> dict[str, Any]:
    manifest_path = manifest_path.resolve()
    context_path = (context_path or manifest_path.with_name("context.txt")).resolve()
    role_path = role_path.resolve()
    requested_role = role or role_path.stem
    errors: list[dict[str, str]] = []
    refs: list[dict[str, Any]] = []
    manifest_record = {"path": str(manifest_path), "sha256": None}
    context_record = {"path": str(context_path), "sha256": None}
    try:
        if requested_role not in ROLES:
            raise GroundingError(f"unsupported role: {requested_role}")
        manifest = _read_object(manifest_path)
        payload = _role_payload(role_path)
        if manifest.get("role") != requested_role:
            raise GroundingError("packet manifest role mismatch")
        files = manifest.get("files")
        if not isinstance(files, list):
            raise GroundingError("packet manifest files must be an array")
        manifest_record["sha256"] = sha256_file(manifest_path)
        context_text = context_path.read_text(encoding="utf-8")
        context_record["sha256"] = sha256_file(context_path)
        declared_context = manifest.get("context")
        if not isinstance(declared_context, dict) or declared_context.get("sha256") != context_record["sha256"]:
            raise GroundingError("packet context hash does not match manifest")
        chunks = {
            item.get("chunk_id"): item
            for item in files
            if isinstance(item, dict)
            and item.get("status") in {"included", "truncated"}
            and SHA256_RE.fullmatch(str(item.get("chunk_id") or ""))
        }
        sections = _context_sections(context_text, files)
        seen_ids: set[str] = set()
        for fallback_id, reference in _references(payload, requested_role):
            ref_id = reference.get("ref_id")
            if not isinstance(ref_id, str) or not ref_id.strip():
                errors.append({"ref_id": fallback_id, "code": "INVALID_REF_ID", "message": "ref_id is missing"})
                continue
            if ref_id in seen_ids:
                errors.append({"ref_id": ref_id, "code": "DUPLICATE_REF_ID", "message": "ref_id must be unique"})
                continue
            seen_ids.add(ref_id)
            chunk_id = reference.get("chunk_id")
            quote = reference.get("quote")
            quote_hash = reference.get("quote_sha256")
            if not isinstance(chunk_id, str) or chunk_id not in chunks:
                errors.append({"ref_id": ref_id, "code": "UNKNOWN_CHUNK", "message": "chunk_id is not in this packet"})
                continue
            if not isinstance(quote, str) or not quote.strip():
                errors.append({"ref_id": ref_id, "code": "EMPTY_QUOTE", "message": "quote must be non-empty"})
                continue
            actual_quote_hash = sha256_bytes(quote.encode("utf-8"))
            # API judges cannot reliably calculate cryptographic hashes.  The
            # verifier therefore computes the hash from the exact quote and
            # only checks a supplied value when one is present.  This keeps
            # the binding system-owned while still detecting tampering in
            # callers that choose to include the optional field.
            if quote_hash is not None and quote_hash != actual_quote_hash:
                errors.append({"ref_id": ref_id, "code": "QUOTE_HASH_MISMATCH", "message": "quote_sha256 does not match quote"})
                continue
            chunk = chunks[chunk_id]
            resolved_path = str(chunk["path"])
            section = sections.get(resolved_path)
            if section is None:
                errors.append({"ref_id": ref_id, "code": "CHUNK_NOT_IN_CONTEXT", "message": "chunk source is absent from context"})
                continue
            occurrences = section["text"].count(quote)
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
            line_start = int(chunk.get("source_line_start") or 1) + relative_line
            line_end = line_start + quote.count("\n")
            context_line_start = int(section["context_line_start"]) + relative_line
            refs.append(
                {
                    "ref_id": ref_id,
                    "chunk_id": chunk_id,
                    "quote_sha256": actual_quote_hash,
                    "resolved_path": resolved_path,
                    "line_start": line_start,
                    "line_end": line_end,
                    "context_line_start": context_line_start,
                    "context_line_end": context_line_start + quote.count("\n"),
                }
            )
    except (GroundingError, OSError) as exc:
        errors.append({"ref_id": "__packet__", "code": "GROUNDING_INPUT_INVALID", "message": str(exc)})
    return {
        "schema_version": SCHEMA_VERSION,
        "role": requested_role,
        "valid": not errors,
        "manifest": manifest_record,
        "context": context_record,
        "refs": refs,
        "errors": errors,
    }


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
