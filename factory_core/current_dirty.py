"""Native v10 classifier extension; historical v9 identities remain verifiable."""
import hashlib
from pathlib import Path
from . import dirty as frozen
from .artifact_policy import NATIVE_POLICY, artifact_policy
from .current_artifact_ownership import ADDITIONAL_OWNERSHIP, artifact_pattern_matches
from .dirty import (
    DirtyChange, DirtyFlag, capture_artifact_manifest, manifest_fingerprint,
    semantic_flags, solver_receipt_job_id,
)

DIRTY_CLASSIFIER_SCHEMA = "factory-native-dirty-classifier-v10"


def classifier_contract_sha256():
    root = Path(__file__).parent
    # S1-B: NATIVE_POLICY decides which DirtyChange a path now produces, so the
    # registry that holds it is part of the classifier identity - the contract
    # that answers "what does this change produce" (Q14: the provenance contract
    # that answers "why was it attributed so" stays separate).
    members = ("current_dirty.py", "current_artifact_ownership.py", "paper_sources.py",
               "artifact_policy.py")
    return hashlib.sha256(b"\0".join([
        DIRTY_CLASSIFIER_SCHEMA.encode(), frozen.classifier_contract_sha256().encode(),
        *(name.encode() + b"\0" + (root / name).read_bytes() for name in members),
    ])).hexdigest()


def classify_manifest_changes(before, after):
    """Classify a manifest delta, routing through the policy layer (S1-B).

    Two registries can resolve a path, and both supersede the frozen fallback:

      * ``ADDITIONAL_OWNERSHIP`` - an ownership rule (unchanged behaviour)
      * ``NATIVE_POLICY`` - a policy-only entry, which decides the consequence
        from its ``invalidation_mode``:
          EXPLICIT_ONLY      -> no change at all; the artifact's own modification
                                creates no upstream obligation
          otherwise          -> the single change the policy names
                                (``route_stage`` / ``route_flag``)

    The frozen classifier's fail-closed pair is stripped for every path resolved
    here, so nothing produces the spurious ``MATH@8 + RESULT@4`` unless it is
    genuinely unregistered.
    """

    resolved = {}
    for path in sorted(set(before) | set(after)):
        if before.get(path) == after.get(path):
            continue
        baseline = before.get(path, "MISSING")
        current = after.get(path, "MISSING")
        owner = next((rule for rule in ADDITIONAL_OWNERSHIP
                      if artifact_pattern_matches(rule.pattern, path)), None)
        if owner is not None:
            resolved[path] = DirtyChange(
                DirtyFlag(owner.dirty_flag), owner.owner_stage, path, baseline, current
            )
            continue
        policy = artifact_policy(path)
        if policy is None or policy.ownership_rule is not None:
            # ownership_rule would already have matched above; None here means
            # the path is genuinely unregistered and keeps the fail-closed pair
            continue
        resolved[path] = policy
    # Strip the frozen fallback for every path the policy layer resolves, then
    # lay down each resolved consequence (EXPLICIT_ONLY contributes none).
    changes = [change for change in frozen.classify_manifest_changes(before, after)
               if change.cause_artifact not in resolved]
    for path, outcome in sorted(resolved.items()):
        if isinstance(outcome, DirtyChange):
            changes.append(outcome)
            continue
        if outcome.route_flag is not None and outcome.route_stage is not None:
            changes.append(DirtyChange(
                DirtyFlag(outcome.route_flag), int(outcome.route_stage), path,
                before.get(path, "MISSING"), after.get(path, "MISSING"),
            ))
    return changes
