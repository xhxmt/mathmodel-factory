from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _prompt(name: str) -> str:
    return (ROOT / "prompts" / name).read_text(encoding="utf-8")


def test_step4_declares_quality_contract_v4_competitiveness_and_derivation():
    text = _prompt("step4_model_construction.txt")

    for required in (
        '"version": 4',
        '"competitiveness_checks"',
        '"objective_sense"',
        '"upper_bound"',
        "lower_bound",
        '"cross_check"',
        '"derived_artifacts"',
    ):
        assert required in text


def test_step5_requires_direction_aware_evidence_and_two_stage_receipt():
    text = _prompt("step5_full_solve.txt")

    for required in (
        "--input",
        "--output",
        "--seed",
        "FACTORY_SOLVER_JOB_ID",
        "solver-job-evidence-v2",
        "receipt_ready=true",
        "bound.json",
        "convergence.json",
        "cross_check.json",
        "最大化 gap = upper − objective",
        "最小化 gap = objective − lower",
    ):
        assert required in text
    assert "run_state/solver_jobs/<jobid>.meta" not in text


def test_paper_steps_create_and_verify_deterministic_derivatives():
    draft = _prompt("step9_paper_draft.txt")
    gate = _prompt("step10_gate1_numerical.txt")

    assert "create_derived_manifest.py" in draft
    assert "verify_derived_artifacts.py" in draft
    assert "tables/headline_values.tex" in draft
    assert "verify_derived_artifacts.py" in gate
    assert "canonical → generator → tables/headline/xlsx" in gate
