from pathlib import Path


def test_backend_canary_is_defined_before_bootstrap_call():
    text = (Path(__file__).resolve().parents[1] / "run_paper.sh").read_text(
        encoding="utf-8"
    )
    definition = text.index("\nbackend_canary() {")
    bootstrap = text.index("if backend_canary claude")

    assert definition < bootstrap
