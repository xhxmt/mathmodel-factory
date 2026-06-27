from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ActionResult:
    ok: bool
    stdout: str
    stderr: str


def run_action(factory_root: Path, action: str, base_name: str) -> ActionResult:
    cmd = [
        "python3",
        str(factory_root / "scripts" / "project_ctl.py"),
        action,
        str(factory_root / "ongoing" / base_name),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=factory_root, check=False)
    return ActionResult(result.returncode == 0, result.stdout, result.stderr)
