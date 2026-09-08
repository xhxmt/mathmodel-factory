"""Bounded, lossless NumPy review capsules with independently checked decoding.

The embedded source is an immutable packet snapshot, not a summary assertion.
Grounding regenerates every dataset/index/value line from those exact bytes.
Only primitive numeric NPY arrays and NPZ collections are supported. No pickle,
array code, filesystem extraction, or optional NumPy dependency is used here.
Format reference: https://numpy.org/doc/stable/reference/generated/numpy.lib.format.html
"""
from __future__ import annotations

import ast
import base64
import hashlib
import io
import json
import math
from pathlib import Path
import re
import struct
import zipfile

CONTRACT = "numpy-review-capsule-v1"
SUFFIXES = {".npy", ".npz"}
MAX_RAW_BYTES = 131_072
MAX_EXPANDED_BYTES = 131_072
MAX_VIEW_BYTES = 262_144
MAX_HEADER_BYTES = 4096
MAX_ELEMENTS = 4096
MAX_MEMBERS = 32
MAX_DIMENSIONS = 8
_FORMATS = {"b1": "?", "i1": "b", "u1": "B", "i2": "h", "u2": "H",
            "i4": "i", "u4": "I", "i8": "q", "u8": "Q", "f2": "e",
            "f4": "f", "f8": "d", "c8": "ff", "c16": "dd"}


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _line(value: dict) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"


def _array(raw: bytes, member: str, element_budget: int = MAX_ELEMENTS) -> tuple[dict, list[dict]]:
    if len(raw) < 10 or raw[:6] != b"\x93NUMPY" or raw[6:8] not in {b"\x01\x00", b"\x02\x00", b"\x03\x00"}:
        raise ValueError("numpy_format_unsupported")
    width = 2 if raw[6] == 1 else 4
    length = int.from_bytes(raw[8:8 + width], "little")
    offset = 8 + width + length
    if not 0 < length <= MAX_HEADER_BYTES or offset > len(raw):
        raise ValueError("numpy_header_limit_or_incomplete")
    try:
        header = ast.literal_eval(raw[8 + width:offset].decode("utf-8" if raw[6] == 3 else "latin1").strip())
    except (SyntaxError, ValueError, TypeError, RecursionError) as exc:
        raise ValueError("numpy_header_invalid") from exc
    if not isinstance(header, dict) or set(header) != {"descr", "fortran_order", "shape"}:
        raise ValueError("numpy_header_invalid")
    dtype, shape, fortran = header["descr"], header["shape"], header["fortran_order"]
    if (not isinstance(dtype, str) or not re.fullmatch(r"[<>|][biufc][0-9]+", dtype)
            or dtype[1:] not in _FORMATS or (dtype[0] == "|" and dtype[1:] not in {"b1", "i1", "u1"})):
        raise ValueError("numpy_dtype_unsupported")
    if (not isinstance(shape, tuple) or len(shape) > MAX_DIMENSIONS
            or any(type(n) is not int or n < 0 or n > MAX_ELEMENTS for n in shape)
            or type(fortran) is not bool):
        raise ValueError("numpy_shape_unsupported")
    count = math.prod(shape)
    if count > element_budget:
        raise ValueError("numpy_element_limit")
    size = int(dtype[2:])
    if len(raw) - offset != count * size:
        raise ValueError("numpy_data_size_mismatch")
    description = dict(member=member, dtype=dtype, shape=list(shape),
                       storage_order="F" if fortran else "C", elements=count,
                       npy_sha256=_digest(raw), npy_size=len(raw), data_offset=offset)
    values = []
    for flat in range(count):
        remaining, indices = flat, [0] * len(shape)
        for axis in (range(len(shape)) if fortran else reversed(range(len(shape)))):
            indices[axis] = remaining % shape[axis]
            remaining //= shape[axis]
        start = offset + flat * size
        encoded = raw[start:start + size]
        decoded = struct.unpack((">" if dtype[0] == ">" else "<") + _FORMATS[dtype[1:]], encoded)
        # Decimal strings avoid JSON number precision loss. Hex preserves exact
        # floating values, while scalar bytes also distinguish NaN payloads.
        scalar = dict(member=member, index=indices, byte_offset=start, scalar_hex=encoded.hex())
        if dtype[1] in "fc":
            scalar.update(value=[repr(v) for v in decoded], float_hex=[v.hex() for v in decoded])
        else:
            scalar["value"] = str(decoded[0])
        values.append(scalar)
    return description, values


def render_bytes(raw: bytes, suffix: str) -> tuple[str, dict]:
    if suffix not in SUFFIXES:
        raise ValueError("numpy_format_unsupported")
    if len(raw) > MAX_RAW_BYTES:
        raise ValueError("numpy_source_byte_limit")
    arrays = []
    if suffix == ".npy":
        arrays.append(_array(raw, "array"))
    else:
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                members = archive.infolist()
                if (len(members) > MAX_MEMBERS or len({i.filename for i in members}) != len(members)
                        or sum(i.file_size for i in members) > MAX_EXPANDED_BYTES):
                    raise ValueError("numpy_archive_limit_or_duplicate")
                element_budget = MAX_ELEMENTS
                for item in sorted(members, key=lambda i: i.filename):
                    if (item.is_dir() or not item.filename.endswith(".npy")
                            or item.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}):
                        raise ValueError("numpy_archive_member_unsupported")
                    with archive.open(item) as handle:
                        data = handle.read(MAX_EXPANDED_BYTES + 1)
                    if len(data) != item.file_size or len(data) > MAX_EXPANDED_BYTES:
                        raise ValueError("numpy_archive_expanded_limit")
                    parsed = _array(data, item.filename, element_budget)
                    arrays.append(parsed)
                    element_budget -= parsed[0]["elements"]
        except (zipfile.BadZipFile, RuntimeError, NotImplementedError) as exc:
            raise ValueError("numpy_archive_invalid") from exc
    if sum(info["elements"] for info, _ in arrays) > MAX_ELEMENTS:
        raise ValueError("numpy_total_element_limit")
    header = dict(contract=CONTRACT, format=suffix[1:], source_sha256=_digest(raw),
                  source_size=len(raw), coverage="full", datasets=len(arrays),
                  source_base64=base64.b64encode(raw).decode("ascii"))
    lines = [_line(header)]
    used = len(lines[0])
    for info, values in arrays:
        for value in (info, *values):
            line = _line(value)
            used += len(line)
            if used > MAX_VIEW_BYTES:
                raise ValueError("numpy_review_byte_limit")
            lines.append(line)
    text = "".join(lines)
    if used > MAX_VIEW_BYTES:
        raise ValueError("numpy_review_byte_limit")
    binding = {k: header[k] for k in ("contract", "format", "source_sha256", "source_size", "coverage")}
    binding.update(view_sha256=_digest(text.encode()), view_size=used,
                   datasets=[info for info, _ in arrays])
    return text, binding


def render_file(path: Path) -> tuple[str, dict]:
    with path.open("rb") as handle:
        raw = handle.read(MAX_RAW_BYTES + 1)
    return render_bytes(raw, path.suffix.lower())


def complete_binding(item: dict) -> bool:
    value = item.get("binary_review")
    return (isinstance(value, dict) and value.get("contract") == CONTRACT
            and value.get("format") in {"npy", "npz"} and value.get("coverage") == "full"
            and value.get("source_sha256") == item.get("sha256")
            and value.get("source_size") == item.get("size")
            and value.get("view_sha256") == item.get("included_sha256")
            and value.get("view_size") == item.get("included_bytes"))


def verify_capsule(text: str, item: dict) -> None:
    if len(text.encode()) > MAX_VIEW_BYTES or not complete_binding(item):
        raise ValueError("numpy_review_binding_invalid")
    try:
        header = json.loads(text.split("\n", 1)[0])
        raw = base64.b64decode(header["source_base64"], validate=True)
        expected, binding = render_bytes(raw, "." + item["binary_review"]["format"])
    except (ValueError, TypeError, KeyError) as exc:
        raise ValueError("numpy_review_source_invalid") from exc
    if expected != text or binding != item["binary_review"]:
        raise ValueError("numpy_review_decoding_mismatch")
