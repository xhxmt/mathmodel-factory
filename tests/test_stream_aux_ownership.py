"""Step 2 stream auxiliary outputs and the early ownership-coverage check.

Acceptance A: the m2 demo solve declared ``m2_field_0_3h.csv`` as a solver
output at the project root.  No ownership rule or policy described it, so the
v2 final Judge failed its snapshot ownership coverage twice after Gate 2
(``TRANSIENT_JUDGE_PACKET``) - the first check that looked at the file.
"""

from pathlib import Path

import pytest

from factory_core.artifact_policy import (
    FALLBACK_POLICY,
    NON_BLOCKING_BY_POLICY,
    POLICY_ORDER,
    artifact_policy,
    shadowed_ownership_rules,
)
from factory_core.current_artifact_ownership import artifact_ownership
from factory_core.current_dirty import classify_manifest_changes
from factory_core.steps.validators import NativeArtifactValidator

ROOT = Path(__file__).resolve().parents[1]


def test_stream_aux_output_is_described_without_gaining_an_owner():
    policy = artifact_policy("m2_field_0_3h.csv")
    assert policy is not None
    assert policy in FALLBACK_POLICY
    assert policy.is_policy_only and policy.is_non_blocking
    assert not policy.final_input and not policy.submission_member
    assert artifact_ownership("m2_field_0_3h.csv") is None
    assert NON_BLOCKING_BY_POLICY  # the declaration the finalizer honours


@pytest.mark.parametrize(
    "path, pattern",
    [
        ("m1_spec.md", "m*_spec.md"),
        ("m2_critique.md", "m*_critique.md"),
        ("m2_demo_result.json", "m*_demo_result.*"),
        ("m1_solver_evidence.json", "m1_solver_evidence.json"),
        ("m4_reuse_gap_record.md", "m4_reuse_gap_record.md"),
    ],
)
def test_fallback_never_shadows_an_owned_or_native_path(path, pattern):
    assert artifact_policy(path).pattern == pattern


def test_fallback_is_last_and_adds_no_shadow():
    assert POLICY_ORDER[-len(FALLBACK_POLICY):] == FALLBACK_POLICY
    assert {hit[0] for hit in shadowed_ownership_rules()} == {
        "scripts/submit_*.sh",
        "result*.xlsx",
    }
    for name in ("method_decision.md", "model.md", "misc_notes.md"):
        policy = artifact_policy(name)
        assert policy is None or policy not in FALLBACK_POLICY, name


def test_stream_aux_output_change_creates_no_obligation():
    changes = classify_manifest_changes({}, {"m2_field_0_3h.csv": "abc"})
    assert changes == []


def _validated_streams(project: Path) -> None:
    (project / "viable_streams.md").write_text(
        "## Stream m1: a\n\n## Stream m2: b\n", encoding="utf-8"
    )
    for stream in (1, 2):
        (project / f"m{stream}_spec.md").write_text("line\n" * 40, encoding="utf-8")
        (project / f"m{stream}_demo_result.json").write_text("{}", encoding="utf-8")
        (project / f"m{stream}_critique.md").write_text(
            "VERDICT: VALIDATED\n", encoding="utf-8"
        )


def test_step_2_accepts_stream_aux_outputs(tmp_path):
    _validated_streams(tmp_path)
    (tmp_path / "m2_field_0_3h.csv").write_text("t,C\n0,1\n", encoding="utf-8")
    valid, _, evidence, _ = NativeArtifactValidator(ROOT, 2)._step_2(tmp_path)
    assert valid
    assert evidence == ("m1_critique.md", "m2_critique.md")


def test_step_2_reports_an_unregistered_artifact_before_gate_2(tmp_path):
    _validated_streams(tmp_path)
    (tmp_path / "field_snapshot.csv").write_text("t,C\n0,1\n", encoding="utf-8")
    valid, reason, evidence, metadata = NativeArtifactValidator(ROOT, 2)._step_2(tmp_path)
    assert not valid
    assert "field_snapshot.csv" in reason and "ownership coverage" in reason
    assert evidence == ("field_snapshot.csv",)
    assert metadata["unowned_artifacts"] == ["field_snapshot.csv"]


def test_step_2_still_requires_two_validated_streams(tmp_path):
    (tmp_path / "viable_streams.md").write_text("## Stream m1: a\n", encoding="utf-8")
    valid, reason, _, _ = NativeArtifactValidator(ROOT, 2)._step_2(tmp_path)
    assert not valid and reason == "fewer than two validated modeling streams"
