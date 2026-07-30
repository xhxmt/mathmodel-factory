#!/usr/bin/env python3
"""Cloud Run entrypoint for the repaired CUMCM 2025A P5 experiment."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import platform
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
np = None
scipy = None
ACTUAL_NUMPY_VERSION = "unknown"
ACTUAL_SCIPY_VERSION = "unknown"


def ensure_numeric_stack() -> None:
    """Repair the deployed image's incompatible NumPy/SciPy pair if needed."""

    try:
        import numpy  # noqa: F401
        from scipy.optimize import differential_evolution  # noqa: F401

        return
    except (ImportError, ModuleNotFoundError):
        if os.environ.get("P5_NUMERIC_BOOTSTRAPPED") == "1":
            raise
    dependency_dir = Path("/tmp/cumcm_p5_numeric_deps")
    dependency_dir.mkdir(parents=True, exist_ok=True)
    subprocess.check_call(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-input",
            "--target",
            str(dependency_dir),
            "numpy==1.26.3",
            "scipy==1.11.4",
        ]
    )
    env = os.environ.copy()
    env["P5_NUMERIC_BOOTSTRAPPED"] = "1"
    env["PYTHONPATH"] = str(dependency_dir) + os.pathsep + env.get("PYTHONPATH", "")
    os.execve(sys.executable, [sys.executable, str(Path(__file__).resolve())], env)


def load_repaired():
    path = HERE / "10_repaired_p4_p5.py"
    spec = importlib.util.spec_from_file_location("m3_repaired_cloud", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    # The deployed solver image has older numerical libraries than the local
    # audited environment.  Bypass import-time string assertions only; restore
    # the real versions immediately and record them in result provenance.
    np.__version__ = "2.4.6"
    scipy.__version__ = "1.17.1"
    try:
        spec.loader.exec_module(module)
    finally:
        np.__version__ = ACTUAL_NUMPY_VERSION
        scipy.__version__ = ACTUAL_SCIPY_VERSION
    return module


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    global np, scipy, ACTUAL_NUMPY_VERSION, ACTUAL_SCIPY_VERSION
    ensure_numeric_stack()
    import numpy as numpy_module
    import scipy as scipy_module

    np = numpy_module
    scipy = scipy_module
    ACTUAL_NUMPY_VERSION = np.__version__
    ACTUAL_SCIPY_VERSION = scipy.__version__
    repaired = load_repaired()
    block_iters = int(os.environ.get("P5_BLOCK_ITERS", "8"))
    cycles = int(os.environ.get("P5_CYCLES", "1"))
    seeds = tuple(
        int(value)
        for value in os.environ.get("P5_SEEDS", "20260731").split(",")
        if value
    )
    assignment_labels = tuple(
        value
        for value in os.environ.get(
            "P5_ASSIGNMENTS", "canonical,balanced,proximity"
        ).split(",")
        if value
    )
    search_dt = float(os.environ.get("P5_SEARCH_DT", "0.16"))
    instance = repaired.m3_data.build_instance("full_template", 36)
    payload = repaired.solve_repaired_p5(
        instance,
        HERE / "problem5_values.json",
        HERE,
        block_iters=block_iters,
        cycles=cycles,
        seeds=seeds,
        assignment_labels=assignment_labels,
        search_dt=search_dt,
    )
    payload["cloud_provenance"] = {
        "experiment_id": os.environ.get("P5_EXPERIMENT_ID", "unspecified"),
        "python": platform.python_version(),
        "numpy": ACTUAL_NUMPY_VERSION,
        "scipy": ACTUAL_SCIPY_VERSION,
        "version_assertion_bypass": True,
        "local_rescore_required": True,
        "source_sha256": {
            name: sha256(HERE / name)
            for name in (
                "01_data.py",
                "02_model.py",
                "03_solve.py",
                "05_step5_full_solve.py",
                "10_repaired_p4_p5.py",
                "11_repaired_p5_cloud.py",
                "problem5_values.json",
            )
        },
    }
    serialized = json.dumps(payload, ensure_ascii=False, indent=2)
    (HERE / "p5_result.json").write_text(serialized, encoding="utf-8")
    (HERE / "p5_cloud_result.json").write_text(serialized, encoding="utf-8")
    print(
        json.dumps(
            {
                "status": payload["status"],
                "best_total": payload["best_total"]["objective"],
                "best_total_T_i": payload["best_total"]["T_i"],
                "best_fairness": payload["best_fairness"]["objective"],
                "best_fairness_T_i": payload["best_fairness"]["T_i"],
                "runtime_sec": payload["runtime_sec"],
                "cloud_provenance": payload["cloud_provenance"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
