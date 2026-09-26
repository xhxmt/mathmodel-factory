"""Append-only provenance for dirty causes (workflow schema v10).

Schema 9 -> 10 adds exactly two things: this table (plus its append-only
triggers) and one new ``_domain_effect_hashes()`` domain key.  No existing
table changes shape, because every existing domain hash is a canonical hash
over ``SELECT *`` rows and would otherwise fail historical `aggregate_valid`
checks.

Why a side table rather than a column on ``dirty_causes``: the compatibility
branch in ``SQLiteStateStore.status_snapshot()`` only tolerates a *different
set of domain keys*.  Adding a column keeps the key set identical, so the
strict aggregate comparison applies and every historical project would report
``aggregate_valid = False``.  A new domain key takes the tolerant branch for
pre-v10 events and the strict branch from the first v10 event onward -- which
is exactly the behaviour we want.

Historical causes are never back-filled: the classification path a v9 cause
took cannot be reconstructed from today's classifier, so it reads back as
``legacy_unrecorded`` instead of being guessed.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

DIRTY_CAUSE_CLASSIFICATION_SCHEMA = "factory-dirty-cause-classification-v1"
CLASSIFICATION_CONTRACT_SCHEMA = "factory-dirty-cause-classification-contract-v1"

#: A cause whose provenance predates this table, or whose writer did not record
#: one.  Never back-filled and never inferred.
LEGACY_UNRECORDED = "legacy_unrecorded"

#: Closed set of recorded provenances.
CLASSIFICATION_SOURCES = (
    "frozen_rule",           # matched the frozen v1 ARTIFACT_OWNERSHIP_REGISTRY
    "current_rule",          # matched ADDITIONAL_OWNERSHIP (current generation)
    "paper_semantic",        # produced by the @paper:<path>:<domain> expansion
    "protected",             # produced by the @protected: early branch
    "fallback",              # matched no rule -> fail-closed MATH@8 + RESULT@4
    "explicit_fail_closed",  # constructed deliberately as a fail-closed cause
    "bespoke_recovery",      # created by the bespoke final-evidence recovery path
)

#: Recordable values.  ``legacy_unrecorded`` is included so that a v10 cause
#: whose provenance could not be derived is *recorded as underived* (visible by
#: query) instead of being indistinguishable from a pre-v10 row:
#:   - no row            -> the cause predates this table
#:   - row = legacy...   -> a v10 cause the derivation did not cover
#: The test suite asserts the derivation covers every change for representative
#: manifests, so the second case should not occur in practice.
ALL_RECORDABLE_SOURCES = CLASSIFICATION_SOURCES + (LEGACY_UNRECORDED,)

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS dirty_cause_classification (
    cause_id                 TEXT PRIMARY KEY REFERENCES dirty_causes(cause_id),
    classification_source    TEXT NOT NULL,
    policy_schema            TEXT NOT NULL,
    policy_contract_sha256   TEXT NOT NULL
)
"""

_CREATE_TRIGGERS = (
    """
    CREATE TRIGGER IF NOT EXISTS dirty_cause_classification_append_only_update
    BEFORE UPDATE ON dirty_cause_classification
    BEGIN
        SELECT RAISE(ABORT, 'dirty cause classification is append-only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS dirty_cause_classification_append_only_delete
    BEFORE DELETE ON dirty_cause_classification
    BEGIN
        SELECT RAISE(ABORT, 'dirty cause classification is append-only');
    END
    """,
)

_PAPER_DOMAIN_FLAGS = {
    "math": "MATH_DIRTY",
    "citation": "CITATION_DIRTY",
    "prose": "PROSE_DIRTY",
    "format": "FORMAT_DIRTY",
}
_PAPER_DOMAINS = ("math", "citation", "prose", "format")


def ensure_dirty_cause_classification_schema(connection) -> None:
    """Install the v10 side table and its append-only triggers (idempotent).

    Individual statements, matching ``ensure_dirty_rebase_schema`` and
    ``ensure_prompt_receipt_schema``: ``executescript`` would implicitly commit
    the caller's migration transaction.
    """

    connection.execute(_CREATE_TABLE)
    for trigger in _CREATE_TRIGGERS:
        connection.execute(trigger)


def classification_contract_sha256() -> str:
    """Identity of the provenance contract (schema name + this module's bytes)."""

    digest = hashlib.sha256()
    digest.update(CLASSIFICATION_CONTRACT_SCHEMA.encode("ascii"))
    digest.update(b"\0")
    digest.update((Path(__file__).parent / "dirty_classification.py").read_bytes())
    return digest.hexdigest()


def _paper_key_present(path: str, before: dict, after: dict) -> bool:
    return any(
        f"@paper:{path}:{domain}" in before or f"@paper:{path}:{domain}" in after
        for domain in _PAPER_DOMAINS
    )


def classification_sources(before: dict, after: dict) -> dict[tuple[str, str], str]:
    """Derive each change's provenance by mirroring the classifiers' branch order.

    Kept outside ``factory_core/dirty.py`` on purpose: that module's bytes are a
    frozen compatibility trust root and are hashed into
    ``classifier_contract_sha256()``.

    Keys are ``(flag_value, cause_artifact)`` -- the same key the frozen
    classifier uses for its ``remember()`` de-duplication, so every emitted
    change can be looked up.  ``@paper:`` keys record the *relative* path as
    their cause, so they are mapped onto that path here.
    """

    from .artifact_ownership import artifact_ownership
    from .current_dirty import ADDITIONAL_OWNERSHIP, artifact_pattern_matches

    changed = sorted(
        path for path in set(before) | set(after) if before.get(path) != after.get(path)
    )
    sources: dict[tuple[str, str], str] = {}

    # -- synthetic keys first: they carry the cause path they will be recorded as
    for artifact in changed:
        if artifact.startswith("@protected:"):
            sources[("MATH_DIRTY", artifact)] = "protected"
        elif artifact.startswith("@paper:"):
            _, relative, domain = artifact.split(":", 2)
            flag = _PAPER_DOMAIN_FLAGS.get(domain)
            if flag is not None:
                sources[(flag, relative)] = "paper_semantic"

    # -- authored paths
    for artifact in changed:
        if artifact.startswith("@"):
            continue
        if artifact.endswith(".tex") and _paper_key_present(artifact, before, after):
            # Diverted into paper_raw_changes by the frozen classifier.  Its own
            # flags come from the @paper: keys handled above; a FORMAT@9 is added
            # only when no @paper:<artifact>:* key itself changed.
            if not any(key.startswith(f"@paper:{artifact}:") for key in changed):
                sources[("FORMAT_DIRTY", artifact)] = "paper_semantic"
            continue
        current_rule = next(
            (rule for rule in ADDITIONAL_OWNERSHIP
             if artifact_pattern_matches(rule.pattern, artifact)),
            None,
        )
        if current_rule is not None:
            sources[(current_rule.dirty_flag, artifact)] = "current_rule"
            continue
        frozen_rule = artifact_ownership(artifact)
        if frozen_rule is not None:
            sources[(frozen_rule.dirty_flag, artifact)] = "frozen_rule"
        else:
            # The fail-closed default emits both flags for the same artifact.
            sources[("MATH_DIRTY", artifact)] = "fallback"
            sources[("RESULT_DIRTY", artifact)] = "fallback"

    return sources


def source_for(
    sources: dict[tuple[str, str], str], flag: str, cause_artifact: str
) -> str:
    """Look up one change's provenance, defaulting to ``legacy_unrecorded``."""

    return sources.get((str(flag), str(cause_artifact)), LEGACY_UNRECORDED)


def record_classification(
    connection,
    *,
    cause_id: str,
    classification_source: str,
    contract_sha256: str,
) -> None:
    """Insert one provenance row.  Must run in the cause's own transaction."""

    if classification_source not in ALL_RECORDABLE_SOURCES:
        raise ValueError(f"unknown classification source: {classification_source}")
    connection.execute(
        """
        INSERT OR IGNORE INTO dirty_cause_classification(
            cause_id, classification_source, policy_schema, policy_contract_sha256
        ) VALUES (?, ?, ?, ?)
        """,
        (
            str(cause_id),
            str(classification_source),
            DIRTY_CAUSE_CLASSIFICATION_SCHEMA,
            str(contract_sha256),
        ),
    )


def recorded_source(connection, cause_id: str) -> str:
    """Read a cause's provenance.  Absent metadata is ``legacy_unrecorded``.

    Deliberately does **not** fall back to re-deriving from the current
    classifier: a historical cause's path is a fact about the past, not a
    function of today's code.
    """

    row = connection.execute(
        "SELECT classification_source FROM dirty_cause_classification WHERE cause_id=?",
        (str(cause_id),),
    ).fetchone()
    if row is None:
        return LEGACY_UNRECORDED
    return str(row[0])
