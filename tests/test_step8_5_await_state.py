"""Regression tests for the Step 8.5 AWAITING pause state.

Covers the two fixes that make `AWAITING_STEP8_5` a first-class paused state
instead of looking like a crash:

1. `launch_agents.sh status` must show `AWAIT_8.5` (not `EXITED`) for a
   project whose runner stopped at the Step 8.5 editorial gate.
2. The stale-lock heartbeat parser in `run_paper.sh` must not throw a bash
   arithmetic syntax error on the `AWAITING_STEP8_5:8` heartbeat prefix.
"""

import os
import subprocess
import sys
from pathlib import Path

from conftest import REPO_ROOT

LAUNCH = os.path.join(REPO_ROOT, "launch_agents.sh")
RUN_PAPER = os.path.join(REPO_ROOT, "run_paper.sh")

# A heartbeat the runner writes when Step 8.5 blocks paper drafting
# (run_paper.sh, `if (( NEXT == 9 )) && (( STEP_RC == 42 ))` branch).
AWAIT_HEARTBEAT = "AWAITING_STEP8_5:8 1700000000\n"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _make_step8_project(root: Path, name: str) -> Path:
    """Create a minimal project sitting at Step 8 with 8.5 gate artifacts.

    Mirrors the fixtures in test_run_paper_step8_5_infer.py so that
    `run_paper.sh --infer-step` returns 8 cleanly (the status command calls
    infer-step for every ongoing project).
    """
    proj = root / name
    _write(proj / "checkpoint.md", "- **Last completed step**: 8\n")
    _write(proj / "problem" / "problem_brief.md", "# brief\n")
    _write(proj / "visualization_log.md", "\n".join(["viz"] * 20) + "\n")
    _write(proj / "figures" / "demo.pdf", "fake\n")
    _write(proj / "reviewer_entry_map.md", "# map\n")
    _write(proj / "anchor_figure_plan.md", "# anchors\n")
    _write(proj / "entry_gate.md", "# gate\n\nVERDICT: REVISE\n")
    _write(proj / ".heartbeat", AWAIT_HEARTBEAT)
    return proj


def test_status_shows_await_8_5_for_blocked_project():
    """launch_agents.sh status must surface AWAIT_8.5, not EXITED.

    Previously a project paused at the Step 8.5 gate wrote only a heartbeat
    (no .paused/.killed/.awaiting_consultation marker), so the status table
    fell through to the pid branch and displayed `EXITED` — indistinguishable
    from a crash. The AWAIT_8.5 elif makes the deliberate pause visible.
    """
    ongoing = Path(REPO_ROOT) / "ongoing"
    name = "_await85_status_test"
    proj = _make_step8_project(ongoing, name)
    try:
        out = subprocess.run(
            [LAUNCH, "status"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert out.returncode == 0, out.stderr

        # Find this project's row and confirm the PROCESS column is AWAIT_8.5.
        line = next((ln for ln in out.stdout.splitlines() if name in ln), None)
        assert line is not None, f"project {name} missing from status output:\n{out.stdout}"
        assert "AWAIT_8.5" in line, f"expected AWAIT_8.5 in row:\n{line}"
        assert "EXITED" not in line, f"must not look like a crash:\n{line}"
    finally:
        import shutil
        shutil.rmtree(proj, ignore_errors=True)


def test_heartbeat_parser_handles_awaiting_prefix():
    """The stale-lock parser must not crash on `AWAITING_STEP8_5:8`.

    Verbatim copy of the strip + numeric-guard logic in run_paper.sh
    (heartbeat parse block near `HB_FILE="$PROJECT/.heartbeat"`). If you
    change the parser there, keep this block in sync. Before the guard,
    `$(( AWAITING_STEP8_5:8 + 1 ))` threw `syntax error in expression`.
    """
    script = r"""
set -euo pipefail
parse() {
    local hb_step="$1" hb_ts="$2"
    hb_step="${hb_step#STUCK:}"
    hb_step="${hb_step#ACTIVE:}"
    hb_step="${hb_step#AWAITING_STEP8_5:}"
    if [[ -n "$hb_ts" && "$hb_ts" =~ ^[0-9]+$ && "$hb_step" =~ ^[0-9]+$ ]]; then
        echo "numeric:$(( hb_step + 1 ))"
    else
        echo "fallback"
    fi
}
parse "AWAITING_STEP8_5:8" "1700000000"
parse "STUCK:5"            "1700000000"
parse "ACTIVE:3"           "1700000000"
parse "8"                  "1700000000"
parse "UNKNOWN_MARKER:8"   "1700000000"
"""
    out = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode == 0, f"parser crashed:\n{out.stderr}"
    lines = out.stdout.splitlines()
    assert lines == [
        "numeric:9",   # AWAITING_STEP8_5:8  -> strip -> 8 -> +1
        "numeric:6",   # STUCK:5             -> strip -> 5 -> +1
        "numeric:4",   # ACTIVE:3            -> strip -> 3 -> +1
        "numeric:9",   # bare 8              -> +1
        "fallback",    # unknown prefix      -> numeric guard -> fallback (no crash)
    ], f"unexpected parser output:\n{out.stdout}"


def test_run_paper_status_surfaces_await_heartbeat(tmp_path):
    """`run_paper.sh --status` prints the raw AWAITING heartbeat (regression
    guard for fix 2: the runner exits 0 and leaves this heartbeat behind)."""
    proj = tmp_path / "demo"
    _make_step8_project(tmp_path, "demo")

    out = subprocess.run(
        [RUN_PAPER, "--status", str(proj)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert out.returncode == 0, out.stderr
    assert "AWAITING_STEP8_5:8" in out.stdout, out.stdout
