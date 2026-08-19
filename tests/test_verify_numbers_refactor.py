import os, subprocess, sys
from conftest import FIXTURE, SCRIPTS

GOLDEN = os.path.join(os.path.dirname(__file__), "golden", "verify_numbers_mini.txt")


def _display_fixture_path(path):
    worktree_marker = f"{os.sep}.worktrees{os.sep}"
    if worktree_marker not in path:
        return path
    repo_root, after = path.split(worktree_marker, 1)
    _, relative_path = after.split(os.sep, 1)
    return os.path.join(repo_root, relative_path)


def test_cli_output_byte_identical():
    out = subprocess.run(
        [sys.executable, os.path.join(SCRIPTS, "verify_numbers.py"), FIXTURE, "mini"],
        capture_output=True, text=True,
    )
    with open(GOLDEN) as f:
        expected = f.read().replace("{FIXTURE}", _display_fixture_path(FIXTURE))
    assert out.stdout + out.stderr == expected

def test_collect_number_metrics_dict():
    from verify_numbers import collect_number_metrics
    m = collect_number_metrics(FIXTURE, "mini")
    assert m["numbers_unmatched"] >= 1   # 999.9 not in log
    assert m["numbers_matched"] >= 1     # 3.14 in log
