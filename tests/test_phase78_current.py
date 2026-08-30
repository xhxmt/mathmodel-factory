from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from factory_core.phase6_snapshot_grants import GrantScope
from factory_core.phase78_config import load_phase78_settings
from factory_core.phase78_current import (
    Phase78CurrentHeadError,
    Phase78CurrentHeadVerifier,
)


def _settings(tmp_path: Path):
    return load_phase78_settings(
        {
            "PHASE78_ENABLED": "true",
            "PHASE78_AUTHORITY_DB_FILE": str(tmp_path / "authority.db"),
            "PHASE78_AUTHORITY_SOURCE_FENCE_SHA256": "a" * 64,
            "PHASE78_PHASE6_DB_FILE": str(tmp_path / "phase6.db"),
            "PHASE78_PHASE7_DB_FILE": str(tmp_path / "phase7.db"),
            "PHASE78_PHASE8_DB_FILE": str(tmp_path / "phase8.db"),
            "PHASE78_WORK_DB_FILE": str(tmp_path / "work.db"),
            "PHASE78_WORK_SPOOL": str(tmp_path / "spool"),
            "PHASE78_PROJECT_ROOT": str(tmp_path / "project"),
            "PHASE78_CAS_ROOT": str(tmp_path / "cas"),
            "PHASE78_SCRATCH_ROOT": str(tmp_path / "scratch"),
        }
    )


def _facts(scope: GrantScope = GrantScope.SNAPSHOT_VIEW):
    authority_wire = {
        "schema": "authority-workflow-coordinate-v1",
        "workflow_id": "workflow-1",
        "project_id": "project-1",
        "current_revision": 3,
    }
    coordinate = SimpleNamespace(
        workflow_id="workflow-1",
        current_revision=3,
        as_dict=lambda: authority_wire,
    )
    occurrence = SimpleNamespace(normalized_path="paper/final.pdf")
    state = SimpleNamespace(
        workflow_id="workflow-1",
        through_revision=3,
        state_sha256="b" * 64,
        occurrences=(occurrence,),
    )
    source = SimpleNamespace(
        authority_coordinate=authority_wire,
        phase3_artifact_state_sha256="b" * 64,
    )
    proof = SimpleNamespace(requested_scope=scope, source_binding=source)
    return coordinate, state, occurrence, proof


def test_current_verifier_rejects_disabled_settings() -> None:
    with pytest.raises(Phase78CurrentHeadError, match="disabled"):
        Phase78CurrentHeadVerifier(load_phase78_settings({}))


def test_current_verifier_rechecks_authority_state_occurrence_and_phase6(
    monkeypatch, tmp_path: Path
) -> None:
    coordinate, state, occurrence, proof = _facts()
    calls: list[str] = []

    class Repository:
        def __init__(self, *args, **kwargs):
            calls.append("repository")

        def workflow_coordinate(self, workflow_id):
            assert workflow_id == coordinate.workflow_id
            calls.append("coordinate")
            return coordinate

        def phase3_artifact_state(self, workflow_id, through_revision):
            assert workflow_id == coordinate.workflow_id
            assert through_revision == coordinate.current_revision
            calls.append("state")
            return state

    class ProofStore:
        def __init__(self, *args, **kwargs):
            calls.append("phase6")

        def verify_current_access_proof(self, value, *, deadline=None):
            assert deadline is None
            calls.append("proof")
            return value

    monkeypatch.setattr("factory_core.phase78_current._state", lambda value: state)
    monkeypatch.setattr(
        "factory_core.phase78_current._occurrence", lambda value: occurrence
    )
    monkeypatch.setattr(
        "factory_core.phase78_current.verify_shadow_access_proof", lambda value: proof
    )
    monkeypatch.setattr("factory_core.phase78_current.AuthorityReadRepository", Repository)
    monkeypatch.setattr("factory_core.phase78_current.Phase6SnapshotGrantStore", ProofStore)
    verified = Phase78CurrentHeadVerifier(_settings(tmp_path)).verify(
        phase3_artifact_state=state,
        phase3_artifact_occurrence=occurrence,
        phase6_access_proof=proof,
    )
    assert verified.artifact_occurrence == occurrence
    assert calls == ["repository", "coordinate", "state", "phase6", "proof"]
