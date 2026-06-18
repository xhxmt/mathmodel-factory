import os, re, subprocess, sys
from conftest import FIXTURE, SCRIPTS

GOLDEN = os.path.join(os.path.dirname(__file__), "golden", "verify_numbers_mini.txt")

def test_cli_output_byte_identical():
    out = subprocess.run(
        [sys.executable, os.path.join(SCRIPTS, "verify_numbers.py"), FIXTURE, "mini"],
        capture_output=True, text=True,
    )
    combined = out.stdout + out.stderr
    combined = combined.replace(FIXTURE + os.sep, "tests/fixtures/mini_proj/")
    combined = combined.replace(FIXTURE + "/", "tests/fixtures/mini_proj/")
    with open(GOLDEN, encoding='utf-8') as f:
        assert combined == f.read()

def test_collect_number_metrics_dict():
    from verify_numbers import collect_number_metrics
    m = collect_number_metrics(FIXTURE, "mini")
    assert m["numbers_unmatched"] >= 1   # 999.9 not in log
    assert m["numbers_matched"] >= 1     # 3.14 in log
