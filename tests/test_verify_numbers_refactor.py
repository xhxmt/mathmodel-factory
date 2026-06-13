import os, subprocess, sys
from conftest import FIXTURE, SCRIPTS

GOLDEN = os.path.join(os.path.dirname(__file__), "golden", "verify_numbers_mini.txt")

def test_cli_output_byte_identical():
    out = subprocess.run(
        [sys.executable, os.path.join(SCRIPTS, "verify_numbers.py"), FIXTURE, "mini"],
        capture_output=True, text=True,
    )
    with open(GOLDEN) as f:
        assert out.stdout + out.stderr == f.read()

def test_collect_number_metrics_dict():
    from verify_numbers import collect_number_metrics
    m = collect_number_metrics(FIXTURE, "mini")
    assert m["numbers_unmatched"] >= 1   # 999.9 not in log
    assert m["numbers_matched"] >= 1     # 3.14 in log
