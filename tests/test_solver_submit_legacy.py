from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_solver_receipt_preserves_dash_prefixed_job_arguments():
    wrapper = (ROOT / "legacy" / "shell" / "solver_submit_legacy.sh").read_text(
        encoding="utf-8"
    )

    assert 'receipt_args+=(--argv="$value")' in wrapper
    assert 'receipt_args+=(--argv "$value")' not in wrapper
