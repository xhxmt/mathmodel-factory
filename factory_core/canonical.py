from __future__ import annotations

import hashlib
import json
from dataclasses import fields, is_dataclass
from enum import Enum
from typing import Any, Mapping


CANONICAL_JSON_SCHEMA = "factory-canonical-json-utf8-v1"


class CanonicalizationError(ValueError):
    """Raised when a value is outside the canonical JSON domain."""


def _canonical_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return _canonical_value(value.value)
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _canonical_value(getattr(value, field.name))
            for field in fields(value)
        }
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        raise CanonicalizationError(
            "float values are outside factory-canonical-json-utf8-v1; "
            "use an integer or an explicitly versioned decimal string"
        )
    if isinstance(value, str):
        try:
            value.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise CanonicalizationError("strings must be valid UTF-8 scalar values") from exc
        return value
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise CanonicalizationError("canonical mappings require string keys")
        return {
            key: _canonical_value(value[key])
            for key in sorted(value)
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        converted = [_canonical_value(item) for item in value]
        return sorted(converted, key=canonical_bytes)
    raise CanonicalizationError(
        f"unsupported canonical value type: {type(value).__qualname__}"
    )


def canonical_bytes(value: Any) -> bytes:
    """Serialize the supported value domain to deterministic UTF-8 JSON.

    Mapping keys are strings sorted by Unicode code point.  Sets are encoded as
    arrays sorted by each member's canonical bytes.  Dataclass field names are
    mapping keys; Enum members use their values; ``None`` becomes JSON null.
    Lists and tuples preserve order because their order is semantic.  Strings
    preserve their exact Unicode scalar sequence and are emitted as UTF-8.
    Floats are rejected so cross-runtime number formatting cannot affect an
    identity hash.
    """

    normalized = _canonical_value(value)
    return json.dumps(
        normalized,
        ensure_ascii=False,
        allow_nan=False,
        check_circular=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()
