import json
import subprocess

from conftest import REPO_ROOT


def test_diag_status_shell_wrapper_writes_snapshot(tmp_path):
    cmd = f'''
set -euo pipefail
FACTORY="{REPO_ROOT}"
source "{REPO_ROOT}/scripts/runner_diagnostics.sh"
diag_status "{tmp_path}" waiting 8 step8_5_gate_review AWAITING_STEP8_5 "Step 8.5 未通过" "open_entry_gate,refresh_status" "file:entry_gate.md"
'''
    subprocess.run(["bash", "-lc", cmd], check=True)
    status = json.loads((tmp_path / "diagnostics" / "status.json").read_text(encoding="utf-8"))
    assert status["reason_code"] == "AWAITING_STEP8_5"
    assert status["suggested_actions"] == ["open_entry_gate", "refresh_status"]
    assert status["evidence"] == [{"kind": "file", "path": "entry_gate.md"}]


def test_diag_event_shell_wrapper_appends_jsonl(tmp_path):
    cmd = f'''
set -euo pipefail
FACTORY="{REPO_ROOT}"
source "{REPO_ROOT}/scripts/runner_diagnostics.sh"
diag_event "{tmp_path}" 6 verification_failed VERIFY_OUTPUT_FAILED "Step 6 output invalid" "solve_log.md"
'''
    subprocess.run(["bash", "-lc", cmd], check=True)
    rows = (tmp_path / "diagnostics" / "events.jsonl").read_text(encoding="utf-8").splitlines()
    payload = json.loads(rows[-1])
    assert payload["type"] == "verification_failed"
    assert payload["reason_code"] == "VERIFY_OUTPUT_FAILED"
    assert payload["files"] == ["solve_log.md"]


def test_diag_status_shell_wrapper_warns_to_stderr_on_helper_failure(tmp_path):
    cmd = f'''
set -euo pipefail
FACTORY="{REPO_ROOT}"
source "{REPO_ROOT}/scripts/runner_diagnostics.sh"
diag_status "{tmp_path}" waiting nope step8_5_gate_review AWAITING_STEP8_5 "Step 8.5 未通过"
'''
    result = subprocess.run(["bash", "-lc", cmd], capture_output=True, text=True, check=True)
    assert result.stdout == ""
    assert "runner_diagnostics.sh: write-status failed" in result.stderr
