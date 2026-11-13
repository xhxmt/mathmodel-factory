"""Native v10 classifier extension; historical v9 identities remain verifiable."""
import hashlib
from . import dirty as frozen
from .artifact_policy import NATIVE_POLICY, artifact_policy
from .current_artifact_ownership import ADDITIONAL_OWNERSHIP, artifact_pattern_matches
from .dirty import (
    DirtyChange, DirtyFlag, capture_artifact_manifest, manifest_fingerprint,
    semantic_flags, solver_receipt_job_id,
)

DIRTY_CLASSIFIER_SCHEMA = "factory-native-dirty-classifier-v10"


#: The classifier's identity.  Dirty causes, checkpoint receipts, reopen
#: budgets and rebases are keyed by it.  Bump it when a change makes some path
#: produce a different ``DirtyChange`` (this module, ``dirty.py``, the ownership
#: registries, ``artifact_policy.py`` or ``paper_sources.py``).  Editing those
#: files without changing a classification -- a comment, a refactor, a helper --
#: leaves the identity alone, so it no longer invalidates checkpoints.
CLASSIFIER_CONTRACT_VERSION = 1


def classifier_contract_sha256():
    return hashlib.sha256(
        f"{DIRTY_CLASSIFIER_SCHEMA}\0{CLASSIFIER_CONTRACT_VERSION}".encode()
    ).hexdigest()


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
