"""Checked-in operational source manifest for the pure dirty classifier.

This module deliberately contains values only. Runtime validation consumes the
trusted manifest without opening source files. Build and evidence tests rebuild
the values from the exact source bytes.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
import re

from .canonical import CanonicalizationError, canonical_bytes, canonical_sha256


DIRTY_CLASSIFIER_IMPLEMENTATION_MANIFEST_SCHEMA = (
    "dirty-classifier-operational-source-manifest-v1"
)
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


class ClassifierImplementationManifestError(ValueError):
    """Raised for a malformed or untrusted operational source manifest."""


@dataclass(frozen=True)
class ClassifierOperationalSourceMemberV1:
    relative_path: str
    byte_size: int
    sha256: str
    role: str


@dataclass(frozen=True)
class ClassifierOperationalImplementationManifestV1:
    schema_version: str
    members: tuple[ClassifierOperationalSourceMemberV1, ...]


TRUSTED_DIRTY_CLASSIFIER_OPERATIONAL_MEMBERS_V1 = (
    ClassifierOperationalSourceMemberV1(
        relative_path="factory_core/artifact_ownership.py",
        byte_size=15825,
        sha256="9f2671f5efc2854e02581d6e621b4caf5071c307660076027c5bce90eb5e5c55",
        role="ordered ownership registry and matcher implementation",
    ),
    ClassifierOperationalSourceMemberV1(
        relative_path="factory_core/dirty.py",
        byte_size=15447,
        sha256="256770269ec2a4ec4344e67b04b59b2f6c8e50ffac66da92e4932ad9c487c8a5",
        role="manifest capture and pure dirty classification implementation",
    ),
    ClassifierOperationalSourceMemberV1(
        relative_path="factory_core/paper_sources.py",
        byte_size=30115,
        sha256="e4594bbb467bacf2183c55f5bbc3aa017e5b38f6ddd330b2ebd0f7c7d1571bc5",
        role="inactive-LaTeX masking and dependency graph implementation",
    ),
)

TRUSTED_DIRTY_CLASSIFIER_OPERATIONAL_MANIFEST_V1 = (
    ClassifierOperationalImplementationManifestV1(
        schema_version=DIRTY_CLASSIFIER_IMPLEMENTATION_MANIFEST_SCHEMA,
        members=TRUSTED_DIRTY_CLASSIFIER_OPERATIONAL_MEMBERS_V1,
    )
)


def _text(value: object, path: str) -> str:
    if type(value) is not str:
        raise ClassifierImplementationManifestError(f"{path} must be a plain string")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ClassifierImplementationManifestError(
            f"{path} must contain valid UTF-8 scalar values"
        ) from exc
    if not value:
        raise ClassifierImplementationManifestError(f"{path} must not be empty")
    return value


def validate_classifier_operational_manifest(
    value: ClassifierOperationalImplementationManifestV1,
    *,
    require_trusted: bool = True,
) -> ClassifierOperationalImplementationManifestV1:
    if type(value) is not ClassifierOperationalImplementationManifestV1:
        raise ClassifierImplementationManifestError(
            "classifier operational manifest has an unsupported runtime type"
        )
    for item in fields(ClassifierOperationalImplementationManifestV1):
        try:
            object.__getattribute__(value, item.name)
        except AttributeError as exc:
            raise ClassifierImplementationManifestError(
                f"classifier operational manifest field {item.name} is missing"
            ) from exc
    if _text(value.schema_version, "manifest.schema_version") != (
        DIRTY_CLASSIFIER_IMPLEMENTATION_MANIFEST_SCHEMA
    ):
        raise ClassifierImplementationManifestError(
            "classifier operational manifest schema is unsupported"
        )
    if type(value.members) is not tuple or not value.members:
        raise ClassifierImplementationManifestError(
            "classifier operational manifest members must be a non-empty tuple"
        )
    paths: list[str] = []
    for index, member in enumerate(value.members):
        path = f"manifest.members[{index}]"
        if type(member) is not ClassifierOperationalSourceMemberV1:
            raise ClassifierImplementationManifestError(
                f"{path} has an unsupported runtime type"
            )
        for field in fields(ClassifierOperationalSourceMemberV1):
            try:
                object.__getattribute__(member, field.name)
            except AttributeError as exc:
                raise ClassifierImplementationManifestError(
                    f"{path}.{field.name} is missing"
                ) from exc
        relative = _text(member.relative_path, f"{path}.relative_path")
        if relative.startswith("/") or "\\" in relative or ".." in relative.split("/"):
            raise ClassifierImplementationManifestError(
                f"{path}.relative_path is not a safe repository-relative path"
            )
        if type(member.byte_size) is not int or member.byte_size < 0:
            raise ClassifierImplementationManifestError(
                f"{path}.byte_size must be a non-negative plain integer"
            )
        digest = _text(member.sha256, f"{path}.sha256")
        if _SHA256_RE.fullmatch(digest) is None:
            raise ClassifierImplementationManifestError(
                f"{path}.sha256 must be lowercase SHA-256"
            )
        _text(member.role, f"{path}.role")
        paths.append(relative)
    if paths != sorted(paths) or len(paths) != len(set(paths)):
        raise ClassifierImplementationManifestError(
            "classifier operational manifest paths must be unique and sorted"
        )
    if require_trusted and value != TRUSTED_DIRTY_CLASSIFIER_OPERATIONAL_MANIFEST_V1:
        raise ClassifierImplementationManifestError(
            "classifier operational manifest differs from checked-in trusted values"
        )
    return value


def dirty_classifier_operational_implementation_bytes() -> bytes:
    value = validate_classifier_operational_manifest(
        TRUSTED_DIRTY_CLASSIFIER_OPERATIONAL_MANIFEST_V1
    )
    try:
        return canonical_bytes(value)
    except CanonicalizationError as exc:  # pragma: no cover - trusted constant
        raise ClassifierImplementationManifestError(
            "trusted classifier operational manifest is not canonical"
        ) from exc


def dirty_classifier_operational_implementation_sha256() -> str:
    value = validate_classifier_operational_manifest(
        TRUSTED_DIRTY_CLASSIFIER_OPERATIONAL_MANIFEST_V1
    )
    try:
        return canonical_sha256(value)
    except CanonicalizationError as exc:  # pragma: no cover - trusted constant
        raise ClassifierImplementationManifestError(
            "trusted classifier operational manifest is not canonical"
        ) from exc
