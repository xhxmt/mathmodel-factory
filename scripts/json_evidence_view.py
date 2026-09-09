"""Deterministic JSON evidence views with explicit, hash-bound array references.

Views retain every object key and every scalar. Only explicitly named numeric
arrays may be represented by references; their complete bytes remain in the
original source. A view is not evidence that those arrays were read by a judge.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path, PurePosixPath

SCHEMA = "json-evidence-view-v1"
SUFFIX = ".evidence-view.json"


def canonical_bytes(value) -> bytes:
    """Scientific JSON encoding; unlike Authority JSON, finite floats are valid."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _source(project: Path, relative: str) -> Path:
    parts = PurePosixPath(relative).parts
    if not parts or relative != PurePosixPath(relative).as_posix() or any(
        part in {".", ".."} for part in parts
    ) or relative.startswith("/") or "\\" in relative:
        raise ValueError("evidence source must be a canonical project-relative path")
    path = project
    for part in parts:
        path = path / part
        if path.is_symlink():
            raise ValueError("evidence source must not contain symlinks")
    if not path.is_file() or path.stat().st_nlink != 1:
        raise ValueError("evidence source must be one regular file")
    return path


def _json(raw: bytes):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def constant(_value):
        raise ValueError("nonfinite JSON value")

    return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)


def build_view(project: Path, source: str, references: dict[str, str]) -> dict:
    """Replace only explicitly declared arrays, preserving all other values."""
    raw = _source(project, source).read_bytes()
    data = _json(raw)
    visited = set()

    def walk(value, pointer):
        if pointer in references:
            reason = references[pointer]
            if not isinstance(reason, str) or not reason.strip():
                raise ValueError("array reference needs an explicit limitation")
            if not isinstance(value, list) or not value or not all(
                type(item) in (int, float) and math.isfinite(item) for item in value
            ):
                raise ValueError("only nonempty finite numeric arrays may be referenced")
            visited.add(pointer)
            return {
                "schema": "json-numeric-array-reference-v1",
                "source_pointer": pointer,
                "count": len(value),
                "canonical_sha256": hashlib.sha256(canonical_bytes(value)).hexdigest(),
                "limitation": reason,
                "included_in_context": False,
            }
        if isinstance(value, dict):
            return {key: walk(item, pointer + "/" + key.replace("~", "~0").replace("/", "~1"))
                    for key, item in value.items()}
        if isinstance(value, list):
            return [walk(item, pointer + "/" + str(index)) for index, item in enumerate(value)]
        return value

    view = walk(data, "")
    if visited != set(references):
        raise ValueError("array reference pointer is absent or overlaps another reference")
    return {
        "schema": SCHEMA,
        "source": source,
        "source_sha256": hashlib.sha256(raw).hexdigest(),
        "source_byte_length": len(raw),
        "array_references": dict(sorted(references.items())),
        "coverage": "all keys and scalars retained; declared arrays referenced, not reviewed",
        "data": view,
    }


def verify_view(project: Path, raw: bytes) -> dict:
    view = _json(raw)
    if not isinstance(view, dict) or view.get("schema") != SCHEMA:
        raise ValueError("unsupported evidence view")
    if not isinstance(view.get("source"), str) or not isinstance(view.get("array_references"), dict):
        raise ValueError("malformed evidence view")
    expected = build_view(project, view["source"], view["array_references"])
    if canonical_bytes(view) != canonical_bytes(expected):
        raise ValueError("evidence view differs from its complete source")
    return {
        "schema": SCHEMA,
        "source": view["source"],
        "source_sha256": view["source_sha256"],
        "referenced_arrays": sorted(view["array_references"]),
        "limitations": view["array_references"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", type=Path)
    parser.add_argument("source")
    parser.add_argument("--references", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    view = build_view(args.project, args.source, _json(args.references.read_bytes()))
    with args.output.open("xb") as stream:
        stream.write(canonical_bytes(view))


if __name__ == "__main__":
    main()
