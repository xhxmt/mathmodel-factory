import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from factory_core.adapters.solvers.local import LocalSolverBackend
from factory_core.adapters.solvers.types import SolverRequest


@pytest.mark.parametrize("declared,hidden,expected", [(True, False, 0), (False, False, 2), (True, True, 2)])
def test_solver_input_configuration_drives_guard_and_argv(tmp_path, declared, hidden, expected):
    a, b = tmp_path / "attachment3.txt", tmp_path / "attachment4.txt"
    a.write_text("3")
    b.write_text("4")
    script = tmp_path / "solve.py"
    script.write_text("from pathlib import Path\nimport sys\n"
        "for name in sys.argv[1:]:\n    Path(name).read_text()\n"
        + ("Path('hidden.txt').read_text()\n" if hidden else ""))
    (tmp_path / "hidden.txt").write_text("hidden")
    request = SolverRequest("test", tmp_path, "python", script,
        args=(str(a), str(b)), input_paths=(a, b) if declared else (a,))
    command = LocalSolverBackend._command(request)
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(command, cwd=tmp_path, env={**os.environ, "PYTHONPATH": str(root),
                             "PYTHONDONTWRITEBYTECODE": "1"}, capture_output=True, timeout=10)
    assert result.returncode == expected, result.stderr.decode()
    report = json.loads((tmp_path / ".factory/solver_jobs/test.inputs.json").read_text())
    if expected == 0:
        assert set(report["observed_inputs"]) >= {a.name, b.name}
    else:
        assert report["undeclared_inputs"] == (["hidden.txt"] if hidden else [b.name])
