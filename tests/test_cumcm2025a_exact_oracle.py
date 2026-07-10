import numpy as np
import pytest

from scripts.domain_oracles.cumcm2025a_occlusion import (
    segment_intersects_sphere,
    solve_problem1_reference,
)


def test_segment_sphere_detects_endpoint_intersection_with_external_projection():
    start = np.array([0.0, 0.0, 0.0])
    end = np.array([1.0, 0.0, 0.0])
    center = np.array([1.1, 0.0, 0.0])

    assert segment_intersects_sphere(start, end, center, radius=0.2)


def test_segment_sphere_rejects_sphere_beyond_segment():
    start = np.array([0.0, 0.0, 0.0])
    end = np.array([1.0, 0.0, 0.0])
    center = np.array([1.3, 0.0, 0.0])

    assert not segment_intersects_sphere(start, end, center, radius=0.2)


def test_segment_fully_inside_sphere_is_intersection():
    start = np.array([-0.1, 0.0, 0.0])
    end = np.array([0.1, 0.0, 0.0])
    center = np.array([0.0, 0.0, 0.0])

    assert segment_intersects_sphere(start, end, center, radius=1.0)


def test_problem1_reference_uses_refined_interval_boundaries():
    result = solve_problem1_reference(endpoint_tol=1e-7)

    assert result.duration == pytest.approx(1.391643, abs=1e-5)
    assert result.start < result.end
    assert result.sample_count >= 600
    assert result.endpoint_tol == pytest.approx(1e-7)
