"""Trusted symbol-span manifest for persisted dirty-owner postprocessing."""

from __future__ import annotations

from dataclasses import dataclass, fields
import re

from .canonical import CanonicalizationError, canonical_bytes, canonical_sha256


PERSISTED_DIRTY_OWNER_IMPLEMENTATION_MANIFEST_SCHEMA = (
    "persisted-dirty-owner-symbol-manifest-v1"
)
PYTHON_AST_SOURCE_SPAN_SCHEMA = "python-ast-source-span-v1"
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


class PersistedDirtyOwnerManifestError(ValueError):
    """Raised for malformed or untrusted symbol-span identities."""


@dataclass(frozen=True)
class PythonSymbolSourceSpanV1:
    relative_path: str
    qualified_symbol: str
    source_span_schema: str
    start_line: int
    end_line: int
    byte_size: int
    source_sha256: str


@dataclass(frozen=True)
class PersistedDirtyOwnerImplementationManifestV1:
    schema_version: str
    symbols: tuple[PythonSymbolSourceSpanV1, ...]


TRUSTED_PERSISTED_DIRTY_OWNER_SYMBOLS_V1 = (
    PythonSymbolSourceSpanV1(
        "factory_core/dirty.py",
        "_SOLVER_RECEIPT_RE",
        PYTHON_AST_SOURCE_SPAN_SCHEMA,
        85,
        87,
        116,
        "9d2d29107e621a4091e11a5c1857c91ded83f879b4a4e3583822b7a8ea25c425",
    ),
    PythonSymbolSourceSpanV1(
        "factory_core/dirty.py",
        "solver_receipt_job_id",
        PYTHON_AST_SOURCE_SPAN_SCHEMA,
        153,
        157,
        262,
        "f90dcd0d746a2f1bfceeef9ae839166e2c7faf11ffe6f8047a495747c37c9b98",
    ),
    PythonSymbolSourceSpanV1(
        "factory_core/engine.py",
        "FactoryEngine._solver_receipt_owner_stage",
        PYTHON_AST_SOURCE_SPAN_SCHEMA,
        909,
        920,
        475,
        "935d124f1b3ad646e173601ee267353c8fa70a7e03d2c6cf88e50a1d41a3b5ef",
    ),
    PythonSymbolSourceSpanV1(
        "factory_core/engine.py",
        "FactoryEngine._stage_manifest_delta",
        PYTHON_AST_SOURCE_SPAN_SCHEMA,
        860,
        907,
        2125,
        "0fb1180a78fca5d4eea900b5d58c70b97e5cb3c3d74fc6b7f1c0c959b77dca89",
    ),
    PythonSymbolSourceSpanV1(
        "factory_core/storage.py",
        "SQLiteStateStore._solver_job_from_row",
        PYTHON_AST_SOURCE_SPAN_SCHEMA,
        3834,
        3857,
        1122,
        "e6d8ada2957c8c010ca493a13417bcd2c15f53d8f1f0b4bddc796d8a14ecd4b7",
    ),
    PythonSymbolSourceSpanV1(
        "factory_core/storage.py",
        "SQLiteStateStore.solver_job",
        PYTHON_AST_SOURCE_SPAN_SCHEMA,
        3804,
        3812,
        411,
        "37c3f90266defd8b623f5d52bb55f11250422f4604e18453313687caf6b125a9",
    ),
)

TRUSTED_PERSISTED_DIRTY_OWNER_IMPLEMENTATION_MANIFEST_V1 = (
    PersistedDirtyOwnerImplementationManifestV1(
        schema_version=PERSISTED_DIRTY_OWNER_IMPLEMENTATION_MANIFEST_SCHEMA,
        symbols=TRUSTED_PERSISTED_DIRTY_OWNER_SYMBOLS_V1,
    )
)


def _text(value: object, path: str) -> str:
    if type(value) is not str or not value:
        raise PersistedDirtyOwnerManifestError(f"{path} must be a non-empty plain string")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise PersistedDirtyOwnerManifestError(
            f"{path} must contain valid UTF-8 scalar values"
        ) from exc
    return value


def validate_persisted_dirty_owner_implementation_manifest(
    value: PersistedDirtyOwnerImplementationManifestV1,
    *,
    require_trusted: bool = True,
) -> PersistedDirtyOwnerImplementationManifestV1:
    if type(value) is not PersistedDirtyOwnerImplementationManifestV1:
        raise PersistedDirtyOwnerManifestError(
            "persisted dirty-owner manifest has an unsupported runtime type"
        )
    for item in fields(PersistedDirtyOwnerImplementationManifestV1):
        try:
            object.__getattribute__(value, item.name)
        except AttributeError as exc:
            raise PersistedDirtyOwnerManifestError(
                f"persisted dirty-owner manifest field {item.name} is missing"
            ) from exc
    if value.schema_version != PERSISTED_DIRTY_OWNER_IMPLEMENTATION_MANIFEST_SCHEMA:
        raise PersistedDirtyOwnerManifestError("persisted dirty-owner manifest schema drift")
    if type(value.symbols) is not tuple or not value.symbols:
        raise PersistedDirtyOwnerManifestError(
            "persisted dirty-owner symbols must be a non-empty tuple"
        )
    keys: list[tuple[str, str]] = []
    for index, symbol in enumerate(value.symbols):
        path = f"manifest.symbols[{index}]"
        if type(symbol) is not PythonSymbolSourceSpanV1:
            raise PersistedDirtyOwnerManifestError(f"{path} has an unsupported runtime type")
        for item in fields(PythonSymbolSourceSpanV1):
            try:
                object.__getattribute__(symbol, item.name)
            except AttributeError as exc:
                raise PersistedDirtyOwnerManifestError(f"{path}.{item.name} is missing") from exc
        relative = _text(symbol.relative_path, f"{path}.relative_path")
        if relative.startswith("/") or "\\" in relative or ".." in relative.split("/"):
            raise PersistedDirtyOwnerManifestError(f"{path}.relative_path is unsafe")
        qualified = _text(symbol.qualified_symbol, f"{path}.qualified_symbol")
        if symbol.source_span_schema != PYTHON_AST_SOURCE_SPAN_SCHEMA:
            raise PersistedDirtyOwnerManifestError(f"{path}.source_span_schema is unsupported")
        for field_name in ("start_line", "end_line", "byte_size"):
            number = getattr(symbol, field_name)
            if type(number) is not int or number < (0 if field_name == "byte_size" else 1):
                raise PersistedDirtyOwnerManifestError(f"{path}.{field_name} is invalid")
        if symbol.end_line < symbol.start_line:
            raise PersistedDirtyOwnerManifestError(f"{path} line span is reversed")
        digest = _text(symbol.source_sha256, f"{path}.source_sha256")
        if _SHA256_RE.fullmatch(digest) is None:
            raise PersistedDirtyOwnerManifestError(f"{path}.source_sha256 is not lowercase SHA-256")
        keys.append((relative, qualified))
    if keys != sorted(keys) or len(keys) != len(set(keys)):
        raise PersistedDirtyOwnerManifestError(
            "persisted dirty-owner symbols must be unique and sorted by path and symbol"
        )
    if require_trusted and value != TRUSTED_PERSISTED_DIRTY_OWNER_IMPLEMENTATION_MANIFEST_V1:
        raise PersistedDirtyOwnerManifestError(
            "persisted dirty-owner manifest differs from checked-in trusted values"
        )
    return value


def persisted_dirty_owner_policy_implementation_bytes() -> bytes:
    manifest = validate_persisted_dirty_owner_implementation_manifest(
        TRUSTED_PERSISTED_DIRTY_OWNER_IMPLEMENTATION_MANIFEST_V1
    )
    try:
        return canonical_bytes(manifest)
    except CanonicalizationError as exc:  # pragma: no cover - trusted constant
        raise PersistedDirtyOwnerManifestError("trusted symbol manifest is not canonical") from exc


def persisted_dirty_owner_policy_implementation_sha256() -> str:
    manifest = validate_persisted_dirty_owner_implementation_manifest(
        TRUSTED_PERSISTED_DIRTY_OWNER_IMPLEMENTATION_MANIFEST_V1
    )
    try:
        return canonical_sha256(manifest)
    except CanonicalizationError as exc:  # pragma: no cover - trusted constant
        raise PersistedDirtyOwnerManifestError("trusted symbol manifest is not canonical") from exc
