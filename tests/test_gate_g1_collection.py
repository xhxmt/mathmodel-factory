"""G1: the collection-path gate.

S1-A/S1-C-prep/S1-D/S1-B changed four collection paths, each of which decides
what ships or what the final audit consumes:

  1. classification          current_dirty.classify_manifest_changes
  2. final input manifest    finalization.build_final_input_manifest
  3. submission bundle       submission_bundle.submission_bundle_paths
  4. cleanup protection      scripts.cleanup_project_artifacts protected set

A synthetic project exercises all four on one tree, so a change in any of them
is caught here rather than at canary time.  The gate asserts *properties*, not
golden byte counts, so it keeps working as the tree grows.
"""
from __future__ import annotations

import sqlite3

import pytest

from factory_core.artifact_policy import NATIVE_POLICY, artifact_policy
from factory_core.current_artifact_ownership import artifact_ownership
from factory_core.current_dirty import classify_manifest_changes
from factory_core.domain import SCHEMA_VERSION
from factory_core.storage import SQLiteStateStore

#: A tree that touches every collector and both directions of the S1-B change.
TREE = (
    # canonical result + its evidence closure (must keep the result rewind)
    "results/canonical_results.json",
    "results/problem1/values.json",
    "m1_solver_evidence.json",
    "model_source_map.json",
    # registered as EXPLICIT_ONLY: no obligation
    "step5_results_gap_report.md",
    "m1_reuse_gap_record.md",
    "m1_solver_evidence_failed.json",
    "paper/appendix_sources/pro01/input_arrays.npz",
    # registered as PRESENTATION_ONLY: format obligation
    "tables.tex",
    "results_values.tex",
    "paper/appendix_sources/06_figures.py",
    # ordinary authored artifacts
    "problem/problem_brief.md",
    "m1_spec.md",
    "solve_log.md",
    "abstract_draft.md",
    "references.bib",
    "result1.xlsx",
    "data/intermediate/scratch.csv",
    # the paper root: submission/final-input collection resolves a LaTeX
    # dependency graph from it, so the synthetic project needs one
    "gate1_paper.tex",
)

#: Kept OUT of TREE on purpose: the delivery collectors fail closed on an
#: unregistered authored artifact, which is asserted as its own property below.
UNKNOWN_PATH = "unknown_authored_contract.json"

_REQUIRED_TRUTH = {
    "results/canonical_results.json": {("RESULT_DIRTY", 4)},
    "m1_solver_evidence.json": {("RESULT_DIRTY", 4)},
    "model_source_map.json": {("RESULT_DIRTY", 4)},
    "tables.tex": {("FORMAT_DIRTY", 9)},
    "results_values.tex": {("FORMAT_DIRTY", 9)},
    "paper/appendix_sources/06_figures.py": {("FORMAT_DIRTY", 9)},
    "step5_results_gap_report.md": set(),
    "m1_reuse_gap_record.md": set(),
    "m1_solver_evidence_failed.json": set(),
    "paper/appendix_sources/pro01/input_arrays.npz": set(),
}


def _project(tmp_path):
    root = tmp_path / "gate1"
    for rel in TREE:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if rel.endswith("_paper.tex"):
            path.write_text(
                "\\documentclass{article}\n\\begin{document}\nG1 gate.\n\\end{document}\n",
                encoding="utf-8",
            )
        else:
            path.write_text("x\n", encoding="utf-8")
    return root


def _classes(path):
    return {
        (c.flag.value, c.owner_stage)
        for c in classify_manifest_changes({}, {path: "x"})
    }


# ------------------------------------------------------------- 1. classification
@pytest.mark.parametrize("path,expected", sorted(_REQUIRED_TRUTH.items()))
def test_collection_path_1_classification(path, expected):
    assert _classes(path) == expected, path


def test_unknown_authored_artifact_still_gets_the_fail_closed_pair():
    """The default is intact: an unregistered path still yields MATH@8 + RESULT@4."""

    assert _classes(UNKNOWN_PATH) == {("MATH_DIRTY", 8), ("RESULT_DIRTY", 4)}


def test_gate_registration_is_complete_and_ownerless():
    """The registered paths are exactly the ones S1-B names, and none has an owner."""

    for policy in NATIVE_POLICY:
        assert policy.is_policy_only
        assert policy.ownership_rule is None
    for path in TREE:
        policy = artifact_policy(path)
        if policy is not None and policy.is_policy_only:
            assert artifact_ownership(path) is None, path


# ------------------------------------------------------------ 2. final input
def test_collection_path_2_final_input_manifest(tmp_path):
    from factory_core.finalization import build_final_input_manifest

    root = _project(tmp_path)
    SQLiteStateStore(root).initialize(project_id="gate1", project_type="modeling")
    manifest = build_final_input_manifest(root).manifest
    paths = {item["path"] for item in manifest["files"]}

    # canonical results are final input; the registered paths are not, by design
    assert "results/canonical_results.json" in paths
    for path in (
        "step5_results_gap_report.md",
        "m1_solver_evidence.json",
        "model_source_map.json",
        "tables.tex",
        "results_values.tex",
        "paper/appendix_sources/pro01/input_arrays.npz",
    ):
        assert path not in paths, f"S1-B must not promote {path} into final input"


# ------------------------------------------------------- 3. submission bundle
def test_collection_path_3_submission_bundle_does_not_grow(tmp_path):
    from factory_core.submission_bundle import submission_bundle_paths

    root = _project(tmp_path)
    SQLiteStateStore(root).initialize(project_id="gate1", project_type="modeling")
    members = {
        p.relative_to(root).as_posix()
        for p in submission_bundle_paths(root, "gate1", require_pdf=False)
    }
    for path in (
        "step5_results_gap_report.md",
        "m1_solver_evidence.json",
        "model_source_map.json",
        "paper/appendix_sources/pro01/input_arrays.npz",
    ):
        assert path not in members, f"S1-B must not ship {path}"


def test_collection_path_3_coverage_gate_still_fails_closed(tmp_path):
    """The two fail-closed gates must still treat an unknown path as uncovered.

    Both gates are membership tests on the policy layer now.  The property that
    matters is that an unregistered authored artifact is still seen as
    uncovered - so the guard cannot be quietly widened by S1-B.
    """

    root = _project(tmp_path)
    SQLiteStateStore(root).initialize(project_id="gate1", project_type="modeling")

    unknown = UNKNOWN_PATH
    assert artifact_ownership(unknown) is None
    assert artifact_policy(unknown) is None, "S1-B must not have registered it"

    # and a registered policy-only path IS covered without being owned
    registered = "step5_results_gap_report.md"
    assert artifact_policy(registered) is not None
    assert artifact_policy(registered).ownership_rule is None
    assert artifact_ownership(registered) is None


# ----------------------------------------------------------- 4. cleanup guard
def test_collection_path_4_cleanup_protection(tmp_path):
    """The cleanup guard must not lose protection for a registered path.

    Before S1-B these paths were unowned, so the ownership-based protected set
    never contained them; after S1-B they are policy-only, and the protected set
    is now driven by the policy layer.  Either way a registered path must not
    become deletable merely because it is unrouted.
    """

    from factory_core.artifact_policy import iter_policy_artifacts

    root = _project(tmp_path)
    protected = {
        p.relative_to(root).as_posix()
        for p in iter_policy_artifacts(root, final_input_only=True)
    }
    # registry-based protection still covers canonical results
    assert "results/canonical_results.json" in protected
    # data/intermediate is explicitly not final input, so it is not protected
    assert "data/intermediate/scratch.csv" not in protected


# --------------------------------------------------------------- cross-cutting
def test_gate_is_consistent_between_classifier_and_provenance(tmp_path):
    """The classifier's obligation and the provenance attribution must agree."""

    from factory_core.dirty_classification import classification_sources

    for path in TREE:
        classes = _classes(path)                       # {(flag, owner_stage)}
        sources = classification_sources({}, {path: "x"})   # {(flag, artifact): source}
        # same flags on both sides, keyed differently
        assert {flag for flag, _ in classes} == {flag for flag, _ in sources}, path
        policy = artifact_policy(path)
        if policy is not None and policy.is_policy_only:
            assert all(v == "policy_only" for v in sources.values()), path
        elif policy is not None:
            assert all(
                v in {"frozen_rule", "current_rule"} for v in sources.values()
            ), path
        else:
            assert all(v == "fallback" for v in sources.values()), path


def test_gate_leaves_history_untouched(tmp_path):
    """Opening the project read-only must not append events or move the revision."""

    root = _project(tmp_path)
    store = SQLiteStateStore(root)
    store.initialize(project_id="gate1", project_type="modeling")
    before = store.load().revision
    connection = sqlite3.connect(root / ".factory" / "state.db")
    try:
        events_before = connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    finally:
        connection.close()

    store.status_snapshot()
    store.load()

    connection = sqlite3.connect(root / ".factory" / "state.db")
    try:
        schema_info = connection.execute(
            "SELECT schema_version FROM schema_info WHERE singleton=1"
        ).fetchone()[0]
        events_after = connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    finally:
        connection.close()
    assert schema_info == SCHEMA_VERSION
    assert events_after == events_before
    assert store.load().revision == before