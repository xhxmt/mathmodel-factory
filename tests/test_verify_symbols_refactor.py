import os, subprocess, sys
from conftest import FIXTURE, REPO_ROOT, SCRIPTS

GOLDEN = os.path.join(os.path.dirname(__file__), "golden", "verify_symbols_mini.txt")

def test_cli_output_byte_identical():
    out = subprocess.run(
        [sys.executable, os.path.join(SCRIPTS, "verify_symbols.py"), FIXTURE, "mini"],
        capture_output=True, text=True,
    )
    combined = out.stdout + out.stderr
    with open(GOLDEN) as f:
        assert combined == f.read()

def test_collect_symbol_metrics_dict():
    from verify_symbols import collect_symbol_metrics
    m = collect_symbol_metrics(FIXTURE, "mini")
    assert m["symbols_undefined"] == 1   # \beta used, not in table
    assert m["symbols_used"] >= 2
    assert "use_before_def" in m
