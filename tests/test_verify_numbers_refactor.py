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


def test_factory_run_logs_are_not_a_number_source(tmp_path):
    # A Judge or model-call log quotes the paper, so treating it as a source
    # would trace every paper value back to the paper itself.
    import shutil
    from verify_numbers import collect_number_metrics

    project = tmp_path / "mini"
    shutil.copytree(FIXTURE, project)
    before = collect_number_metrics(project, "mini")
    assert before["numbers_unmatched"] >= 1  # 999.9 has no source
    for name in (
        "step_16_claude_20261109_065153_2791855_cfd2c767a76b41fe.log",
        "worker_20261109_064756.log",
        "native_judge_packet_20261109_065116_2791855.log",
    ):
        (project / "logs" / name).write_text("the paper reports 999.9\n", encoding="utf-8")
    assert collect_number_metrics(project, "mini")["numbers_unmatched"] == before["numbers_unmatched"]

    (project / "logs" / "solve_extra.log").write_text("objective 999.9\n", encoding="utf-8")
    assert collect_number_metrics(project, "mini")["numbers_unmatched"] < before["numbers_unmatched"]
