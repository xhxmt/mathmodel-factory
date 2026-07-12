#!/usr/bin/env python3
"""Independent geometry oracle for the fixed-strategy CUMCM 2025A problem.

This module intentionally does not import project solver code.  It provides a
second implementation for the load-bearing line-segment/sphere predicate and
the fixed Problem 1 reference calculation used by offline regression tests.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np


MISSILE_INITIAL = np.array([20000.0, 0.0, 2000.0])
MISSILE_SPEED = 300.0
TARGET_CENTER_XY = np.array([0.0, 200.0])
TARGET_RADIUS = 7.0
TARGET_HEIGHT = 10.0
SMOKE_BURST_TIME = 5.1
SMOKE_BURST_POINT = np.array([17188.0, 0.0, 1736.496])
SMOKE_RADIUS = 10.0
SMOKE_SINK_SPEED = 3.0
SMOKE_LIFETIME = 20.0


@dataclass(frozen=True)
class ReferenceInterval:
    start: float
    end: float
    duration: float
    sample_count: int
    endpoint_tol: float


def segment_intersects_sphere(
    start: np.ndarray,
    end: np.ndarray,
    center: np.ndarray,
    radius: float,
) -> bool:
    """Return whether the closed segment intersects or lies inside a sphere."""
    direction = np.asarray(end, dtype=float) - np.asarray(start, dtype=float)
    offset = np.asarray(start, dtype=float) - np.asarray(center, dtype=float)
    a = float(direction @ direction)
    if a == 0.0:
        return float(offset @ offset) <= radius * radius
    b = 2.0 * float(offset @ direction)
    c = float(offset @ offset) - radius * radius
    discriminant = b * b - 4.0 * a * c
    if discriminant < 0.0:
        return False
    root = discriminant**0.5
    s_low = (-b - root) / (2.0 * a)
    s_high = (-b + root) / (2.0 * a)
    return max(s_low, 0.0) <= min(s_high, 1.0)


def _target_boundary_points(points_per_circle: int) -> np.ndarray:
    angles = np.linspace(0.0, 2.0 * np.pi, points_per_circle, endpoint=False)
    circle_xy = TARGET_CENTER_XY + TARGET_RADIUS * np.column_stack(
        (np.cos(angles), np.sin(angles))
    )
    bottom = np.column_stack((circle_xy, np.zeros(points_per_circle)))
    top = np.column_stack(
        (circle_xy, np.full(points_per_circle, TARGET_HEIGHT))
    )
    return np.vstack((bottom, top))


def _missile_position(t: float) -> np.ndarray:
    direction = -MISSILE_INITIAL / np.linalg.norm(MISSILE_INITIAL)
    return MISSILE_INITIAL + MISSILE_SPEED * t * direction


def _smoke_center(t: float) -> np.ndarray:
    center = SMOKE_BURST_POINT.copy()
    center[2] -= SMOKE_SINK_SPEED * (t - SMOKE_BURST_TIME)
    return center


def _fully_occluded(t: float, targets: np.ndarray) -> bool:
    if not SMOKE_BURST_TIME <= t <= SMOKE_BURST_TIME + SMOKE_LIFETIME:
        return False
    missile = _missile_position(t)
    smoke = _smoke_center(t)
    return all(
        segment_intersects_sphere(missile, target, smoke, SMOKE_RADIUS)
        for target in targets
    )


def _bisect_transition(
    left: float,
    right: float,
    targets: np.ndarray,
    endpoint_tol: float,
    entering: bool,
) -> float:
    while right - left > endpoint_tol:
        mid = (left + right) / 2.0
        state = _fully_occluded(mid, targets)
        if state == entering:
            right = mid
        else:
            left = mid
    return (left + right) / 2.0


def solve_problem1_reference(
    *,
    points_per_circle: int = 300,
    scan_step: float = 0.01,
    endpoint_tol: float = 1e-7,
) -> ReferenceInterval:
    """Compute the fixed-strategy full-occlusion interval independently."""
    targets = _target_boundary_points(points_per_circle)
    times = np.arange(
        SMOKE_BURST_TIME,
        SMOKE_BURST_TIME + SMOKE_LIFETIME + scan_step,
        scan_step,
    )
    states = np.array([_fully_occluded(float(t), targets) for t in times])
    true_indices = np.flatnonzero(states)
    if true_indices.size == 0:
        raise RuntimeError("fixed Problem 1 strategy produced no occlusion interval")

    first = int(true_indices[0])
    last = int(true_indices[-1])
    if first == 0 or last + 1 >= len(times):
        raise RuntimeError("occlusion transition was not bracketed by the scan")

    start = _bisect_transition(
        float(times[first - 1]),
        float(times[first]),
        targets,
        endpoint_tol,
        entering=True,
    )
    end = _bisect_transition(
        float(times[last]),
        float(times[last + 1]),
        targets,
        endpoint_tol,
        entering=False,
    )
    return ReferenceInterval(
        start=start,
        end=end,
        duration=end - start,
        sample_count=len(targets),
        endpoint_tol=endpoint_tol,
    )


def verify_project_result(project: Path, tolerance: float = 1e-5) -> dict[str, object]:
    project = Path(project).resolve()
    values_path = project / "results" / "problem1" / "values.json"
    values = json.loads(values_path.read_text(encoding="utf-8"))
    intervals = (values.get("decision") or {}).get("intervals") or []
    if len(intervals) != 1 or len(intervals[0]) != 2:
        return {
            "passed": False,
            "reason": "project result must contain exactly one Problem 1 interval",
            "values_path": str(values_path),
        }
    reference = solve_problem1_reference(endpoint_tol=min(1e-7, tolerance / 10.0))
    objective = float(values["objective"])
    start, end = (float(value) for value in intervals[0])
    duration_error = abs(objective - reference.duration)
    start_error = abs(start - reference.start)
    end_error = abs(end - reference.end)
    return {
        "passed": max(duration_error, start_error, end_error) <= tolerance,
        "values_path": str(values_path),
        "tolerance": tolerance,
        "project": {"start": start, "end": end, "duration": objective},
        "reference": {
            "start": reference.start,
            "end": reference.end,
            "duration": reference.duration,
        },
        "duration_error": duration_error,
        "start_error": start_error,
        "end_error": end_error,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify-project", type=Path)
    parser.add_argument("--tolerance", type=float, default=1e-5)
    args = parser.parse_args()
    if args.verify_project:
        result = verify_project_result(args.verify_project, args.tolerance)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["passed"] else 1
    result = solve_problem1_reference()
    print(json.dumps(result.__dict__, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
