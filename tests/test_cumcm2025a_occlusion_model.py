import numpy as np
import pytest

from scripts.domain_oracles.cumcm2025a_occlusion import (
    SMOKE_BURST_POINT,
    solve_problem1_reference,
)


def test_problem1_uses_full_target_occlusion_reference_values():
    result = solve_problem1_reference(endpoint_tol=1e-7)

    np.testing.assert_allclose(SMOKE_BURST_POINT, [17188.0, 0.0, 1736.496], atol=1e-6)
    assert result.duration == pytest.approx(1.391643, abs=1e-5)
    assert result.start == pytest.approx(8.056445, abs=1e-5)
    assert result.end == pytest.approx(9.448088, abs=1e-5)


def test_problem1_solver_reports_reference_precision():
    result = solve_problem1_reference(endpoint_tol=1e-7)

    assert result.duration == pytest.approx(1.391643, abs=1e-5)
