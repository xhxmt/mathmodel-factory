#!/usr/bin/env python3
"""Experimental repaired P4/P5 solver for the CUMCM 2025A project.

Outputs are written below ``results/repaired`` and never replace canonical
CURRENT_PASS artifacts.  The module reuses the delivered motion and geometry
implementation while correcting search horizons and schedule decoding.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np
from scipy.optimize import differential_evolution


REPO_ROOT = Path(__file__).resolve().parents[3]
PROJECT_DIR = Path(
    os.environ.get(
        "CUMCM2025A_PROJECT",
        REPO_ROOT / "complete" / "cumcm_2025_a_current_pass",
    )
).resolve()
MODEL_DIR = PROJECT_DIR / "models" / "m3_milp_pso"


def _load_sibling(module_name: str, filename: str):
    spec = importlib.util.spec_from_file_location(module_name, MODEL_DIR / filename)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot import {filename}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


m3_model = _load_sibling("m3_repaired_model", "02_model.py")
m3_data = _load_sibling("m3_repaired_data", "01_data.py")
m3_full = _load_sibling("m3_repaired_full", "05_step5_full_solve.py")


UAV_IDS = ("FY1", "FY2", "FY3", "FY4", "FY5")
MISSILE_IDS = ("M1", "M2", "M3")


def missile_hit_time(instance: dict[str, Any], missile_id: str) -> float:
    initial = np.asarray(instance["missiles"][missile_id], dtype=float)
    speed = float(instance["parameters"]["missile_speed_mps"])
    return float(np.linalg.norm(initial) / speed)


def max_burst_delay(instance: dict[str, Any], uav_id: str) -> float:
    z0 = float(instance["uavs"][uav_id][2])
    gravity = float(instance["parameters"]["gravity_mps2"])
    return math.sqrt(2.0 * z0 / gravity)


def problem4_bounds(instance: dict[str, Any]) -> list[tuple[float, float]]:
    horizon = missile_hit_time(instance, "M1")
    bounds: list[tuple[float, float]] = []
    for uav_id in ("FY1", "FY2", "FY3"):
        bounds.extend(
            [
                (0.0, 2.0 * math.pi),
                (70.0, 140.0),
                (0.0, horizon),
                (0.0, max_burst_delay(instance, uav_id)),
            ]
        )
    return bounds


def decode_ordered_drops(raw: list[float] | np.ndarray, horizon: float) -> np.ndarray:
    if horizon < 2.0:
        raise ValueError("horizon must permit two one-second gaps")
    q = np.sort(np.clip(np.asarray(raw, dtype=float), 0.0, 1.0))
    if q.shape != (3,):
        raise ValueError("exactly three raw drop coordinates are required")
    slack = float(horizon) - 2.0
    return np.asarray(
        [slack * q[0], 1.0 + slack * q[1], 2.0 + slack * q[2]],
        dtype=float,
    )


def _plan_from_points(
    instance: dict[str, Any],
    uav_id: str,
    drop_point: tuple[float, float, float],
    burst_point: tuple[float, float, float],
    theta_deg: float,
    speed: float,
) -> Any:
    initial = np.asarray(instance["uavs"][uav_id], dtype=float)
    drop = np.asarray(drop_point, dtype=float)
    burst = np.asarray(burst_point, dtype=float)
    drop_time = float(np.linalg.norm(drop[:2] - initial[:2]) / speed)
    delay = float(np.linalg.norm(burst[:2] - drop[:2]) / speed)
    return m3_model.SmokePlan(
        theta=math.radians(theta_deg),
        speed=float(speed),
        drop_time=drop_time,
        delay=delay,
    )


def reference_p4_records(instance: dict[str, Any]) -> list[Any]:
    """Independent published strategy used only as an evaluator regression."""

    rows = [
        (
            "FY1",
            (17763.38330, 1.17214, 1800.0),
            (17552.65402, 7.91777, 1762.82904),
            178.16654,
            76.54968,
        ),
        (
            "FY2",
            (13251.30020, 260.33090, 1400.0),
            (13521.74556, 14.01253, 1361.06668),
            317.67312,
            129.77384,
        ),
        (
            "FY3",
            (6455.35305, -208.59043, 700.0),
            (6498.39015, 55.23604, 654.78273),
            80.73514,
            87.996834,
        ),
    ]
    return [
        m3_full.SmokeRecord(
            uav_id,
            "M1",
            1,
            _plan_from_points(instance, uav_id, drop, burst, theta, speed),
        )
        for uav_id, drop, burst, theta, speed in rows
    ]


def decode_p5_candidate(
    vector: list[float] | np.ndarray,
    assignments: dict[str, list[str | None]],
    instance: dict[str, Any],
) -> list[Any]:
    """Decode five 8-variable UAV blocks into a structurally feasible schedule."""

    values = np.clip(np.asarray(vector, dtype=float), 0.0, 1.0)
    if values.shape != (40,):
        raise ValueError("P5 vector must contain five 8-variable UAV blocks")
    records: list[Any] = []
    for uav_index, uav_id in enumerate(UAV_IDS):
        block = values[8 * uav_index : 8 * (uav_index + 1)]
        theta = float(2.0 * math.pi * block[0])
        speed = float(70.0 + 70.0 * block[1])
        labels = list(assignments.get(uav_id, []))[:3]
        labels.extend([None] * (3 - len(labels)))
        active_indices = [index for index, label in enumerate(labels) if label is not None]
        if not active_indices:
            continue
        delays = [
            max(1.0e-3, float(block[5 + index]) * max_burst_delay(instance, uav_id))
            for index in range(3)
        ]
        safe_horizon = min(
            missile_hit_time(instance, labels[index]) - delays[index]
            for index in active_indices
            if labels[index] is not None
        )
        safe_horizon = max(2.0, safe_horizon)
        drops = decode_ordered_drops(block[2:5], safe_horizon)
        slot = 0
        for index, missile_id in enumerate(labels):
            if missile_id is None:
                continue
            slot += 1
            plan = m3_model.SmokePlan(
                theta=theta,
                speed=speed,
                drop_time=float(drops[index]),
                delay=float(delays[index]),
            )
            records.append(m3_full.SmokeRecord(uav_id, missile_id, slot, plan))
    return records


def verify_shared_flight(records: list[Any], tolerance: float = 1.0e-9) -> None:
    for uav_id in UAV_IDS:
        selected = [record for record in records if record.uav_id == uav_id]
        if not selected:
            continue
        theta = selected[0].plan.theta
        speed = selected[0].plan.speed
        if any(abs(record.plan.theta - theta) > tolerance for record in selected):
            raise ValueError(f"{uav_id} uses inconsistent headings")
        if any(abs(record.plan.speed - speed) > tolerance for record in selected):
            raise ValueError(f"{uav_id} uses inconsistent speeds")
        drops = sorted(record.plan.drop_time for record in selected)
        if any(right - left < 1.0 - tolerance for left, right in zip(drops, drops[1:])):
            raise ValueError(f"{uav_id} violates the one-second drop gap")


def global_rescore_records(records: list[Any]) -> list[Any]:
    """Clone physical smokes across missile labels for assignment-free scoring."""

    return [
        m3_full.SmokeRecord(record.uav_id, missile_id, record.slot, record.plan)
        for missile_id in MISSILE_IDS
        for record in records
    ]


def p5_assignment_candidates(
    instance: dict[str, Any], canonical: dict[str, list[str | None]]
) -> list[dict[str, Any]]:
    canonical_assignment = {
        uav_id: (list(canonical.get(uav_id, [])) + [None, None, None])[:3]
        for uav_id in UAV_IDS
    }
    balanced = {
        "FY1": ["M1", "M1", "M1"],
        "FY2": ["M2", "M2", "M2"],
        "FY3": ["M3", "M3", "M3"],
        "FY4": ["M1", "M2", "M2"],
        "FY5": ["M1", "M3", "M3"],
    }
    proximity: dict[str, list[str]] = {}
    for uav_id in UAV_IDS:
        uav = np.asarray(instance["uavs"][uav_id], dtype=float)
        closest = min(
            MISSILE_IDS,
            key=lambda missile_id: float(
                np.linalg.norm(np.asarray(instance["missiles"][missile_id], dtype=float) - uav)
            ),
        )
        proximity[uav_id] = [closest, closest, closest]
    return [
        {"label": "canonical", "assignments": canonical_assignment},
        {"label": "balanced", "assignments": balanced},
        {"label": "proximity", "assignments": proximity},
    ]


def assignments_from_records(records: list[Any]) -> dict[str, list[str | None]]:
    assignments: dict[str, list[str | None]] = {}
    for uav_id in UAV_IDS:
        selected = sorted(
            (record for record in records if record.uav_id == uav_id),
            key=lambda record: record.slot,
        )
        labels: list[str | None] = [record.missile_id for record in selected[:3]]
        labels.extend([None] * (3 - len(labels)))
        assignments[uav_id] = labels
    return assignments


def encode_p5_records(
    records: list[Any],
    assignments: dict[str, list[str | None]],
    instance: dict[str, Any],
) -> np.ndarray:
    """Encode a feasible physical schedule into the normalized P5 genotype."""

    vector = np.full(40, 0.5, dtype=float)
    for uav_index, uav_id in enumerate(UAV_IDS):
        selected = sorted(
            (record for record in records if record.uav_id == uav_id),
            key=lambda record: record.slot,
        )
        if not selected:
            continue
        block = vector[8 * uav_index : 8 * (uav_index + 1)]
        block[0] = float(selected[0].plan.theta % (2.0 * math.pi)) / (2.0 * math.pi)
        block[1] = (float(selected[0].plan.speed) - 70.0) / 70.0
        labels = (list(assignments.get(uav_id, [])) + [None, None, None])[:3]
        delays = np.zeros(3, dtype=float)
        drops = np.zeros(3, dtype=float)
        for index, record in enumerate(selected[:3]):
            delays[index] = float(record.plan.delay)
            drops[index] = float(record.plan.drop_time)
            block[5 + index] = delays[index] / max_burst_delay(instance, uav_id)
        active_indices = [index for index, label in enumerate(labels) if label is not None]
        safe_horizon = min(
            missile_hit_time(instance, str(labels[index])) - delays[index]
            for index in active_indices
        )
        safe_horizon = max(2.0, safe_horizon)
        slack = safe_horizon - 2.0
        block[2:5] = np.asarray(
            [drops[0] / slack, (drops[1] - 1.0) / slack, (drops[2] - 2.0) / slack],
            dtype=float,
        )
    return np.clip(vector, 0.0, 1.0)


def p5_score(evaluation: dict[str, Any], mode: str) -> float:
    total = float(evaluation["objective"])
    if mode == "total":
        return total
    if mode == "fairness":
        return 1000.0 * min(float(value) for value in evaluation["T_i"].values()) + total
    raise ValueError(f"unknown P5 objective mode: {mode}")


def evaluate_p5_vector(
    instance: dict[str, Any],
    vector: np.ndarray,
    assignments: dict[str, list[str | None]],
    dt: float,
) -> tuple[list[Any], dict[str, Any]]:
    records = decode_p5_candidate(vector, assignments, instance)
    verify_shared_flight(records)
    evaluation = m3_full.evaluate_records(
        instance,
        global_rescore_records(records),
        list(MISSILE_IDS),
        dt,
    )
    return records, evaluation


def rank_p4_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(candidates, key=lambda item: float(item["fine_objective"]), reverse=True)


def _p4_records_from_vector(vector: np.ndarray) -> list[Any]:
    records: list[Any] = []
    for uav_index, uav_id in enumerate(("FY1", "FY2", "FY3")):
        offset = 4 * uav_index
        theta, speed, drop_time, delay = map(float, vector[offset : offset + 4])
        records.append(
            m3_full.SmokeRecord(
                uav_id,
                "M1",
                1,
                m3_model.SmokePlan(theta=theta, speed=speed, drop_time=drop_time, delay=delay),
            )
        )
    return records


def _records_to_p4_vector(records: list[Any]) -> np.ndarray:
    values: list[float] = []
    by_uav = {record.uav_id: record for record in records}
    for uav_id in ("FY1", "FY2", "FY3"):
        plan = by_uav[uav_id].plan
        values.extend([plan.theta, plan.speed, plan.drop_time, plan.delay])
    return np.asarray(values, dtype=float)


def _p4_objective(instance: dict[str, Any], vector: np.ndarray, dt: float) -> float:
    horizon = missile_hit_time(instance, "M1")
    records = _p4_records_from_vector(np.asarray(vector, dtype=float))
    for record in records:
        if record.plan.burst_time > horizon:
            return 0.0
        burst = m3_model.burst_point(
            np.asarray(instance["uavs"][record.uav_id], dtype=float),
            record.plan,
            instance["parameters"],
        )
        if float(burst[2]) < 0.0:
            return 0.0
    return float(m3_full.evaluate_records(instance, records, ["M1"], dt)["objective"])


def _load_canonical_records(project: Path, slug: str) -> list[Any]:
    return load_records_from_values(project / "results" / slug / "values.json")


def load_records_from_values(path: Path) -> list[Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return records_from_decision_items(payload["decision"]["smokes"])


def records_from_decision_items(items: list[dict[str, Any]]) -> list[Any]:
    records: list[Any] = []
    for item in items:
        records.append(
            m3_full.SmokeRecord(
                item["uav_id"],
                item["missile_id"],
                int(item["slot"]),
                m3_model.SmokePlan(
                    theta=float(item["theta_j"]),
                    speed=float(item["v_j"]),
                    drop_time=float(item["t_{d,j,k}"]),
                    delay=float(item["tau_{jk}"]),
                ),
            )
        )
    return records


def _single_smoke_search(
    instance: dict[str, Any],
    uav_id: str,
    seed: int,
    maxiter: int,
    dt: float,
) -> tuple[Any, float, int]:
    horizon = missile_hit_time(instance, "M1")
    bounds = [
        (0.0, 2.0 * math.pi),
        (70.0, 140.0),
        (0.0, horizon),
        (0.0, max_burst_delay(instance, uav_id)),
    ]

    def minimized(vector: np.ndarray) -> float:
        theta, speed, drop_time, delay = map(float, vector)
        plan = m3_model.SmokePlan(theta, speed, drop_time, delay)
        if plan.burst_time > horizon:
            return 0.0
        burst = m3_model.burst_point(
            np.asarray(instance["uavs"][uav_id], dtype=float), plan, instance["parameters"]
        )
        if float(burst[2]) < 0.0:
            return 0.0
        record = m3_full.SmokeRecord(uav_id, "M1", 1, plan)
        return -float(m3_full.evaluate_records(instance, [record], ["M1"], dt)["objective"])

    seeded = np.asarray(m3_model.seeded_particle(instance, uav_id, "M1", 0), dtype=float)
    seeded[2] = np.clip(seeded[2], 0.0, horizon)
    seeded[3] = np.clip(seeded[3], 0.0, max_burst_delay(instance, uav_id))
    result = differential_evolution(
        minimized,
        bounds,
        maxiter=maxiter,
        popsize=5,
        seed=seed,
        polish=False,
        tol=0.0,
        updating="immediate",
        workers=1,
        x0=seeded,
    )
    theta, speed, drop_time, delay = map(float, result.x)
    record = m3_full.SmokeRecord(
        uav_id,
        "M1",
        1,
        m3_model.SmokePlan(theta, speed, drop_time, delay),
    )
    fine = float(m3_full.evaluate_records(instance, [record], ["M1"], 0.02)["objective"])
    return record, fine, int(result.nfev)


def geometry_plan(
    instance: dict[str, Any],
    uav_id: str,
    missile_id: str,
    burst_time: float,
    line_fraction: float,
    lateral_offset: float = 0.0,
    vertical_offset: float = 0.0,
) -> Any | None:
    """Map a near-sightline burst point to a feasible UAV plan.

    The offsets deliberately place the cloud beside and above the instantaneous
    center sightline so that wind-free sinking can carry it through the full
    target silhouette after detonation.
    """

    horizon = missile_hit_time(instance, missile_id)
    if not (0.0 < burst_time <= horizon and 0.0 <= line_fraction <= 1.0):
        return None
    missile = np.asarray(
        m3_model.missile_position(
            np.asarray(instance["missiles"][missile_id], dtype=float),
            burst_time,
            float(instance["parameters"]["missile_speed_mps"]),
        ),
        dtype=float,
    )
    target = np.asarray([0.0, 200.0, 5.0], dtype=float)
    desired = missile + float(line_fraction) * (target - missile)
    desired += np.asarray([0.0, float(lateral_offset), float(vertical_offset)])
    initial = np.asarray(instance["uavs"][uav_id], dtype=float)
    velocity_xy = (desired[:2] - initial[:2]) / float(burst_time)
    speed = float(np.linalg.norm(velocity_xy))
    if not (70.0 <= speed <= 140.0):
        return None
    if desired[2] < 0.0 or desired[2] > initial[2]:
        return None
    delay = math.sqrt(max(0.0, 2.0 * float(initial[2] - desired[2]) / 9.8))
    drop_time = float(burst_time) - delay
    if drop_time < 0.0:
        return None
    theta = float(math.atan2(velocity_xy[1], velocity_xy[0]) % (2.0 * math.pi))
    return m3_model.SmokePlan(theta, speed, drop_time, delay)


def _geometry_single_search(
    instance: dict[str, Any],
    uav_id: str,
    seed: int,
    local_iters: int,
) -> tuple[Any, float, int]:
    horizon = missile_hit_time(instance, "M1")
    band_candidates: list[tuple[float, Any, float, float]] = []
    n_eval = 0
    burst_grid = np.linspace(1.0, horizon - 1.0, 55)
    band_edges = np.linspace(0.0, horizon, 4)
    best_by_band: list[tuple[float, Any, float, float] | None] = [None, None, None]
    for burst_time in burst_grid:
        for line_fraction in np.linspace(0.01, 0.75, 35):
            plan = geometry_plan(
                instance,
                uav_id,
                "M1",
                float(burst_time),
                float(line_fraction),
                lateral_offset=-8.0,
                vertical_offset=6.0,
            )
            if plan is None:
                continue
            record = m3_full.SmokeRecord(uav_id, "M1", 1, plan)
            value = float(m3_full.evaluate_records(instance, [record], ["M1"], 0.15)["objective"])
            n_eval += 1
            band = min(2, int(np.searchsorted(band_edges[1:], burst_time, side="right")))
            previous = best_by_band[band]
            if previous is None or value > previous[0]:
                best_by_band[band] = (value, plan, float(burst_time), float(line_fraction))
    band_candidates = [candidate for candidate in best_by_band if candidate is not None]
    if not band_candidates:
        raise RuntimeError(f"geometry search found no feasible plan for {uav_id}")

    refined_plans: list[Any] = [candidate[1] for candidate in band_candidates]
    for band_index, (_value, coarse_plan, best_burst, best_fraction) in enumerate(band_candidates):
        bounds = [
            (max(0.1, best_burst - 5.0), min(horizon, best_burst + 5.0)),
            (max(0.0, best_fraction - 0.15), min(0.95, best_fraction + 0.15)),
            (-15.0, 2.0),
            (0.0, 15.0),
        ]

        def minimized(vector: np.ndarray) -> float:
            nonlocal n_eval
            plan = geometry_plan(
                instance,
                uav_id,
                "M1",
                float(vector[0]),
                float(vector[1]),
                lateral_offset=float(vector[2]),
                vertical_offset=float(vector[3]),
            )
            n_eval += 1
            if plan is None:
                return 0.0
            record = m3_full.SmokeRecord(uav_id, "M1", 1, plan)
            return -float(m3_full.evaluate_records(instance, [record], ["M1"], 0.08)["objective"])

        result = differential_evolution(
            minimized,
            bounds,
            maxiter=local_iters,
            popsize=5,
            seed=seed + band_index,
            polish=False,
            tol=0.0,
            updating="immediate",
            workers=1,
            x0=np.asarray([best_burst, best_fraction, -8.0, 6.0], dtype=float),
        )
        refined = geometry_plan(
            instance,
            uav_id,
            "M1",
            float(result.x[0]),
            float(result.x[1]),
            lateral_offset=float(result.x[2]),
            vertical_offset=float(result.x[3]),
        )
        if refined is not None:
            refined_plans.append(refined)

    scored: list[tuple[float, Any]] = []
    for plan in refined_plans:
        record = m3_full.SmokeRecord(uav_id, "M1", 1, plan)
        fine = float(m3_full.evaluate_records(instance, [record], ["M1"], 0.02)["objective"])
        scored.append((fine, plan))
    fine, best_plan = max(scored, key=lambda item: item[0])
    return m3_full.SmokeRecord(uav_id, "M1", 1, best_plan), fine, n_eval


def solve_repaired_p4(
    project: Path,
    instance: dict[str, Any],
    single_iters: int = 45,
    joint_iters: int = 60,
    seeds: tuple[int, ...] = (20260721, 20260722, 20260723),
) -> dict[str, Any]:
    started = time.perf_counter()
    single_records: list[Any] = []
    single_runs: list[dict[str, Any]] = []
    total_evals = 0
    for index, uav_id in enumerate(("FY1", "FY2", "FY3")):
        record, fine, nfev = _geometry_single_search(
            instance, uav_id, seeds[index % len(seeds)], single_iters
        )
        single_records.append(record)
        single_runs.append(
            {
                "uav_id": uav_id,
                "fine_objective": fine,
                "decision": m3_full.smoke_record_to_decision(instance, record),
                "n_eval": nfev,
            }
        )
        total_evals += nfev

    candidates: list[dict[str, Any]] = []
    candidate_vectors: list[tuple[str, np.ndarray]] = [
        ("independent_full_horizon", _records_to_p4_vector(single_records)),
        ("delivered_canonical", _records_to_p4_vector(_load_canonical_records(project, "problem4"))),
    ]
    bounds = problem4_bounds(instance)

    for seed in seeds:
        x0 = candidate_vectors[0][1]

        def minimized(vector: np.ndarray) -> float:
            return -_p4_objective(instance, vector, 0.12)

        result = differential_evolution(
            minimized,
            bounds,
            maxiter=joint_iters,
            popsize=4,
            seed=seed,
            polish=False,
            tol=0.0,
            updating="immediate",
            workers=1,
            x0=x0,
        )
        candidate_vectors.append((f"joint_seed_{seed}", np.asarray(result.x, dtype=float)))
        total_evals += int(result.nfev)

    for label, vector in candidate_vectors:
        records = _p4_records_from_vector(vector)
        search_value = _p4_objective(instance, vector, 0.12)
        fine_evaluation = m3_full.evaluate_records(instance, records, ["M1"], 0.02)
        candidates.append(
            {
                "label": label,
                "search_objective": search_value,
                "fine_objective": float(fine_evaluation["objective"]),
                "intervals": fine_evaluation["by_missile"]["M1"]["intervals"],
                "records": records,
            }
        )

    ranked = rank_p4_candidates(candidates)
    best = ranked[0]
    best_records = best.pop("records")
    individual = [
        float(m3_full.evaluate_records(instance, [record], ["M1"], 0.02)["objective"])
        for record in best_records
    ]
    for candidate in ranked:
        candidate.pop("records", None)
    result_payload = {
        "status": "EXPERIMENTAL_FEASIBLE",
        "objective": best["fine_objective"],
        "intervals": best["intervals"],
        "individual_durations": individual,
        "decision": [m3_full.smoke_record_to_decision(instance, record) for record in best_records],
        "single_smoke_starts": single_runs,
        "candidates": ranked,
        "n_eval": total_evals,
        "runtime_sec": round(time.perf_counter() - started, 3),
        "search_contract": {
            "hard_drop_horizon": missile_hit_time(instance, "M1"),
            "search_dt": 0.12,
            "fine_dt": 0.02,
            "single_iters": single_iters,
            "joint_iters": joint_iters,
            "seeds": list(seeds),
            "reference_strategy_used_for_optimization": False,
        },
    }
    output_dir = project / "results" / "repaired"
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "p4_result.json").write_text(
        json.dumps(result_payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return result_payload


def _optimize_p5_assignment(
    instance: dict[str, Any],
    assignments: dict[str, list[str | None]],
    seed_vector: np.ndarray,
    mode: str,
    rng_seed: int,
    block_iters: int,
    cycles: int,
    search_dt: float,
) -> dict[str, Any]:
    current = np.clip(np.asarray(seed_vector, dtype=float), 0.0, 1.0).copy()
    _records, current_evaluation = evaluate_p5_vector(
        instance, current, assignments, search_dt
    )
    current_score = p5_score(current_evaluation, mode)
    eval_count = 1
    history = [
        {
            "cycle": 0,
            "uav_id": "seed",
            "objective": current_evaluation["objective"],
            "min_duration": min(current_evaluation["T_i"].values()),
        }
    ]
    for cycle in range(cycles):
        for uav_index, uav_id in enumerate(UAV_IDS):
            left = 8 * uav_index
            right = left + 8

            def minimized(block: np.ndarray) -> float:
                nonlocal eval_count
                candidate = current.copy()
                candidate[left:right] = block
                _candidate_records, evaluation = evaluate_p5_vector(
                    instance, candidate, assignments, search_dt
                )
                eval_count += 1
                return -p5_score(evaluation, mode)

            result = differential_evolution(
                minimized,
                [(0.0, 1.0)] * 8,
                maxiter=block_iters,
                popsize=3,
                seed=int(rng_seed + 101 * cycle + uav_index),
                polish=False,
                tol=0.0,
                updating="immediate",
                workers=1,
                x0=current[left:right],
            )
            candidate = current.copy()
            candidate[left:right] = np.asarray(result.x, dtype=float)
            _candidate_records, candidate_evaluation = evaluate_p5_vector(
                instance, candidate, assignments, search_dt
            )
            eval_count += 1
            candidate_score = p5_score(candidate_evaluation, mode)
            if candidate_score >= current_score:
                current = candidate
                current_evaluation = candidate_evaluation
                current_score = candidate_score
            history.append(
                {
                    "cycle": cycle + 1,
                    "uav_id": uav_id,
                    "objective": current_evaluation["objective"],
                    "min_duration": min(current_evaluation["T_i"].values()),
                }
            )
    records, fine_evaluation = evaluate_p5_vector(instance, current, assignments, 0.05)
    return {
        "mode": mode,
        "seed": int(rng_seed),
        "vector": current,
        "records": records,
        "search_evaluation": current_evaluation,
        "fine_evaluation": fine_evaluation,
        "n_eval": eval_count,
        "history": history,
    }


def solve_repaired_p5(
    instance: dict[str, Any],
    canonical_values: Path,
    output_dir: Path,
    block_iters: int = 12,
    cycles: int = 1,
    seeds: tuple[int, ...] = (20260731,),
    assignment_labels: tuple[str, ...] = ("canonical", "balanced", "proximity"),
    search_dt: float = 0.16,
) -> dict[str, Any]:
    started = time.perf_counter()
    canonical_records = load_records_from_values(canonical_values)
    canonical_assignments = assignments_from_records(canonical_records)
    candidates = [
        candidate
        for candidate in p5_assignment_candidates(instance, canonical_assignments)
        if candidate["label"] in assignment_labels
    ]
    runs: list[dict[str, Any]] = []
    for assignment_candidate in candidates:
        assignments = assignment_candidate["assignments"]
        seed_vector = encode_p5_records(canonical_records, assignments, instance)
        for mode in ("total", "fairness"):
            for rng_seed in seeds:
                run = _optimize_p5_assignment(
                    instance,
                    assignments,
                    seed_vector,
                    mode,
                    int(rng_seed),
                    block_iters,
                    cycles,
                    search_dt,
                )
                run["assignment_label"] = assignment_candidate["label"]
                run["assignments"] = assignments
                runs.append(run)

    if not runs:
        raise RuntimeError("no P5 assignment candidates selected")
    best_total = max(runs, key=lambda run: float(run["fine_evaluation"]["objective"]))
    best_fairness = max(
        runs,
        key=lambda run: (
            min(float(value) for value in run["fine_evaluation"]["T_i"].values()),
            float(run["fine_evaluation"]["objective"]),
        ),
    )

    def summarize(run: dict[str, Any]) -> dict[str, Any]:
        evaluation = run["fine_evaluation"]
        return {
            "mode": run["mode"],
            "assignment_label": run["assignment_label"],
            "seed": run["seed"],
            "objective": evaluation["objective"],
            "T_i": evaluation["T_i"],
            "by_missile": evaluation["by_missile"],
            "n_eval": run["n_eval"],
            "history": run["history"],
            "assignments": run["assignments"],
            "decision": [
                m3_full.smoke_record_to_decision(instance, record)
                for record in run["records"]
            ],
        }

    payload = {
        "status": "EXPERIMENTAL_FEASIBLE",
        "best_total": summarize(best_total),
        "best_fairness": summarize(best_fairness),
        "runs": [
            {
                "mode": run["mode"],
                "assignment_label": run["assignment_label"],
                "seed": run["seed"],
                "objective": run["fine_evaluation"]["objective"],
                "T_i": run["fine_evaluation"]["T_i"],
                "n_eval": run["n_eval"],
            }
            for run in runs
        ],
        "runtime_sec": round(time.perf_counter() - started, 3),
        "search_contract": {
            "global_rescore": True,
            "block_iters": block_iters,
            "cycles": cycles,
            "seeds": list(seeds),
            "assignment_labels": list(assignment_labels),
            "search_dt": search_dt,
            "fine_dt": 0.05,
            "fairness_score": "1000*min(T_i)+sum(T_i); reporting ranked lexicographically",
        },
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "p5_result.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--only", choices=("p4", "p5", "all"), default="all")
    parser.add_argument("--p5-block-iters", type=int, default=12)
    parser.add_argument("--p5-cycles", type=int, default=1)
    parser.add_argument("--p5-seeds", default="20260731")
    parser.add_argument("--p5-assignments", default="canonical,balanced,proximity")
    parser.add_argument("--p5-search-dt", type=float, default=0.16)
    args = parser.parse_args()
    instance = m3_data.build_instance("full_template", 36)
    payload: dict[str, Any] = {
        "project": str(args.project),
        "mode": args.only,
        "hit_times": {missile: missile_hit_time(instance, missile) for missile in MISSILE_IDS},
    }
    if args.only in ("p4", "all"):
        payload["p4"] = solve_repaired_p4(args.project, instance)
    if args.only in ("p5", "all"):
        seeds = tuple(int(value) for value in args.p5_seeds.split(",") if value)
        assignment_labels = tuple(
            value for value in args.p5_assignments.split(",") if value
        )
        payload["p5"] = solve_repaired_p5(
            instance,
            args.project / "results" / "problem5" / "values.json",
            args.project / "results" / "repaired",
            block_iters=args.p5_block_iters,
            cycles=args.p5_cycles,
            seeds=seeds,
            assignment_labels=assignment_labels,
            search_dt=args.p5_search_dt,
        )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
