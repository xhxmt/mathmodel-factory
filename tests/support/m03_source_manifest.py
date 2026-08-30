"""Build-time source-byte identities used by M0.3 conformance evidence."""

from __future__ import annotations

from dataclasses import replace
import hashlib
from pathlib import Path

from factory_core.classifier_implementation_manifest import (
    ClassifierOperationalImplementationManifestV1,
    ClassifierOperationalSourceMemberV1,
    DIRTY_CLASSIFIER_IMPLEMENTATION_MANIFEST_SCHEMA,
)
from factory_core.canonical import canonical_sha256


CLASSIFIER_MEMBER_ROLES = {
    "factory_core/artifact_ownership.py": "ordered ownership registry and matcher implementation",
    "factory_core/dirty.py": "manifest capture and pure dirty classification implementation",
    "factory_core/paper_sources.py": "inactive-LaTeX masking and dependency graph implementation",
}

CONTRACT_COMPILER_MEMBERS = (
    "factory_core/canonical.py",
    "factory_core/classifier_identity.py",
    "factory_core/classifier_implementation_manifest.py",
    "factory_core/contract_pins.py",
    "factory_core/owner_compiler.py",
    "factory_core/persisted_dirty_owner_implementation_manifest.py",
    "factory_core/workflow_contract.py",
    "factory_core/workflow_contract_v2.py",
)


def _regular_bytes(root: Path, relative: str) -> bytes:
    path = root / relative
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"source manifest member is not an ordinary file: {relative}")
    return path.read_bytes()


def rebuild_classifier_operational_manifest(
    root: Path,
) -> ClassifierOperationalImplementationManifestV1:
    members = []
    for relative, role in sorted(CLASSIFIER_MEMBER_ROLES.items()):
        data = _regular_bytes(root, relative)
        members.append(
            ClassifierOperationalSourceMemberV1(
                relative_path=relative,
                byte_size=len(data),
                sha256=hashlib.sha256(data).hexdigest(),
                role=role,
            )
        )
    return ClassifierOperationalImplementationManifestV1(
        schema_version=DIRTY_CLASSIFIER_IMPLEMENTATION_MANIFEST_SCHEMA,
        members=tuple(members),
    )


def classifier_operational_manifest_sha256(root: Path) -> str:
    return canonical_sha256(rebuild_classifier_operational_manifest(root))


def contract_compiler_implementation_manifest(root: Path) -> tuple[tuple[str, int, str], ...]:
    return tuple(
        (relative, len(data), hashlib.sha256(data).hexdigest())
        for relative in sorted(CONTRACT_COMPILER_MEMBERS)
        for data in (_regular_bytes(root, relative),)
    )


def contract_compiler_implementation_sha256(root: Path) -> str:
    return canonical_sha256(
        (
            "contract-compiler-operational-source-manifest-v1",
            contract_compiler_implementation_manifest(root),
        )
    )


def with_synthetic_member_bytes(
    manifest: ClassifierOperationalImplementationManifestV1,
    relative_path: str,
    data: bytes,
) -> ClassifierOperationalImplementationManifestV1:
    members = tuple(
        replace(
            member,
            byte_size=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
        )
        if member.relative_path == relative_path
        else member
        for member in manifest.members
    )
    return replace(manifest, members=members)
