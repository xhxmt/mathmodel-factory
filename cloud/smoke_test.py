#!/usr/bin/env python3
"""Build-time smoke test for the declared solver-python capability manifest."""

from __future__ import annotations

import importlib

import numpy as np
from scipy.optimize import linprog

from runtime_capabilities import assert_enabled_runtimes_installed, enabled_solver_types


def main() -> None:
    assert enabled_solver_types() == ("python",)
    assert_enabled_runtimes_installed()
    for module_name in ("numpy", "scipy", "pandas", "sympy", "matplotlib", "pyscipopt", "cylp", "mip"):
        importlib.import_module(module_name)

    solution = np.linalg.solve(np.array([[2.0, 0.0], [0.0, 4.0]]), np.array([4.0, 8.0]))
    assert np.allclose(solution, [2.0, 2.0])
    lp = linprog([1.0], bounds=[(1.0, None)], method="highs")
    assert lp.success and abs(float(lp.fun) - 1.0) < 1e-9


if __name__ == "__main__":
    main()
