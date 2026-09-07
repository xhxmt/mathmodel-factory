"""Native v10 classifier extension; historical v9 identities remain verifiable."""
import hashlib
from pathlib import Path
from . import dirty as frozen
from .current_artifact_ownership import ADDITIONAL_OWNERSHIP, artifact_pattern_matches
from .dirty import (
    DirtyChange, DirtyFlag, capture_artifact_manifest, manifest_fingerprint,
    semantic_flags, solver_receipt_job_id,
)

DIRTY_CLASSIFIER_SCHEMA = "factory-native-dirty-classifier-v10"


def classifier_contract_sha256():
    root = Path(__file__).parent
    members = ("current_dirty.py", "current_artifact_ownership.py", "paper_sources.py")
    return hashlib.sha256(b"\0".join([
        DIRTY_CLASSIFIER_SCHEMA.encode(), frozen.classifier_contract_sha256().encode(),
        *(name.encode() + b"\0" + (root / name).read_bytes() for name in members),
    ])).hexdigest()


def classify_manifest_changes(before, after):
    additions = {}
    for path in sorted(set(before) | set(after)):
        if before.get(path) == after.get(path):
            continue
        owner = next((rule for rule in ADDITIONAL_OWNERSHIP
                      if artifact_pattern_matches(rule.pattern, path)), None)
        if owner is not None:
            additions[path] = DirtyChange(DirtyFlag(owner.dirty_flag), owner.owner_stage,
                                         path, before.get(path, "MISSING"), after.get(path, "MISSING"))
    # Strip the frozen unknown-path fallback only for the two exact new rules.
    changes = [change for change in frozen.classify_manifest_changes(before, after)
               if change.cause_artifact not in additions]
    return changes + list(additions.values())
