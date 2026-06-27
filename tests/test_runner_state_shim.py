import json
import subprocess

from conftest import REPO_ROOT


def test_runner_mark_consultation_writes_display_status_and_gate(tmp_path):
    cmd = f'''
set -euo pipefail
FACTORY="{REPO_ROOT}"
source "{REPO_ROOT}/scripts/runner_state.sh"
runner_mark_consultation "{tmp_path}" 4 step4
'''
    subprocess.run(["bash", "-lc", cmd], check=True)
    status = json.loads((tmp_path / "diagnostics" / "status.json").read_text(encoding="utf-8"))
    assert status["display_status"] == "awaiting_consultation"
    assert status["consultation_gate"] == "step4"
    assert status["reason_code"] == "CONSULTATION_PENDING"
