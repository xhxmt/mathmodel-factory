"""Compatibility-only access to the historical v9 classifier source hash.

The historical value is already persisted in production-era rows and receipts.
New M0.3 identities must not use it: it omits transitive ``paper_sources.py``
implementation bytes and conflates behavior with source implementation.
"""

from __future__ import annotations

from .dirty import classifier_contract_sha256 as _legacy_classifier_contract_sha256


LEGACY_CLASSIFIER_CONTRACT_SHA256_V9 = (
    "c451a9d0be64fabd185c7561b67093956e663db6e845915cd320fea1cbabc515"
)


def verify_legacy_classifier_contract_sha256_v9() -> str:
    """Return the fixed legacy identity after verifying the frozen implementation."""

    actual = _legacy_classifier_contract_sha256()
    if actual != LEGACY_CLASSIFIER_CONTRACT_SHA256_V9:
        raise RuntimeError("frozen legacy classifier contract identity drifted")
    return actual
