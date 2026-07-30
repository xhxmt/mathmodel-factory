import importlib.util
import json
import os
import sys
from pathlib import Path

import numpy as np
import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
PACKAGE_DIR = Path(__file__).resolve().parents[1]
PROJECT = Path(
    os.environ.get(
        "CUMCM2025A_PROJECT",
        REPO_ROOT / "complete" / "cumcm_2025_a_current_pass",
    )
).resolve()
MODEL_DIR = PROJECT / "models" / "m3_milp_pso"
REPAIRED_MODULE = PACKAGE_DIR / "src" / "10_repaired_p4_p5.py"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def load_stack():
    model = load_module("m3_model", MODEL_DIR / "02_model.py")
    data = load_module("m3_data", MODEL_DIR / "01_data.py")
    full = load_module("m3_full", MODEL_DIR / "05_step5_full_solve.py")
    repaired = load_module("m3_repaired", REPAIRED_MODULE)
    return model, data, full, repaired


def test_p4_bounds_cover_late_fy3_drop():
    _model, data, _full, repaired = load_stack()
    instance = data.build_instance("full_template", 36)
    bounds = repaired.problem4_bounds(instance)

    assert bounds[10][1] >= 32.141


def test_ordered_drop_decoder_preserves_horizon_and_one_second_gaps():
    _model, _data, _full, repaired = load_stack()
    drops = repaired.decode_ordered_drops([0.95, 0.10, 0.50], horizon=60.0)

    assert drops[1] - drops[0] >= 1.0
    assert drops[2] - drops[1] >= 1.0
    assert drops[-1] <= 60.0


def test_reference_p4_strategy_scores_above_ten_seconds_under_current_geometry():
    _model, data, full, repaired = load_stack()
    instance = data.build_instance("full_template", 36)
    records = repaired.reference_p4_records(instance)
    evaluation = full.evaluate_records(instance, records, ["M1"], 0.05)

    assert evaluation["objective"] >= 10.9


def test_p5_decoder_uses_shared_flight_and_optional_slots():
    _model, data, _full, repaired = load_stack()
    instance = data.build_instance("full_template", 36)
    raw = np.full(5 * 8, 0.5, dtype=float)
    assignments = {
        "FY1": ["M1", "M1", None],
        "FY2": ["M2", None, None],
        "FY3": ["M3", "M3", "M3"],
        "FY4": ["M2", "M2", None],
        "FY5": ["M3", None, None],
    }

    records = repaired.decode_p5_candidate(raw, assignments, instance)
    repaired.verify_shared_flight(records)

    assert len(records) == 9
    for uav_id in assignments:
        uav_records = [record for record in records if record.uav_id == uav_id]
        if not uav_records:
            continue
        assert len({round(record.plan.theta, 12) for record in uav_records}) == 1
        assert len({round(record.plan.speed, 12) for record in uav_records}) == 1
        drops = sorted(record.plan.drop_time for record in uav_records)
        assert all(right - left >= 1.0 for left, right in zip(drops, drops[1:]))


def test_rank_p4_candidates_uses_fine_objective():
    _model, _data, _full, repaired = load_stack()
    candidates = [
        {"label": "coarse_winner", "search_objective": 9.0, "fine_objective": 8.1},
        {"label": "fine_winner", "search_objective": 8.8, "fine_objective": 8.4},
    ]

    ranked = repaired.rank_p4_candidates(candidates)

    assert ranked[0]["label"] == "fine_winner"


def test_repaired_module_loads_without_preloaded_aliases():
    for name in ("m3_model", "m3_data", "m3_full", "m3_repaired_standalone"):
        sys.modules.pop(name, None)

    module = load_module("m3_repaired_standalone", REPAIRED_MODULE)

    assert callable(module.solve_repaired_p4)


def test_geometry_parameterization_reaches_late_fy3_window():
    _model, data, _full, repaired = load_stack()
    instance = data.build_instance("full_template", 36)

    plan = repaired.geometry_plan(instance, "FY3", "M1", burst_time=35.2, line_fraction=0.316)

    assert plan is not None
    assert 70.0 <= plan.speed <= 140.0
    assert plan.drop_time > 10.0
    assert plan.burst_time <= repaired.missile_hit_time(instance, "M1")


def test_geometry_parameterization_represents_los_offset_needed_by_late_fy3_smoke():
    _model, data, full, repaired = load_stack()
    instance = data.build_instance("full_template", 36)

    plan = repaired.geometry_plan(
        instance,
        "FY3",
        "M1",
        burst_time=35.17875979424529,
        line_fraction=0.3158164539132442,
        lateral_offset=-7.92726361,
        vertical_offset=3.31496015,
    )

    assert plan is not None
    record = full.SmokeRecord("FY3", "M1", 1, plan)
    evaluation = full.evaluate_records(instance, [record], ["M1"], 0.02)
    assert evaluation["objective"] >= 2.65


def test_geometry_single_search_finds_effective_fy3_smoke():
    _model, data, _full, repaired = load_stack()
    instance = data.build_instance("full_template", 36)

    record, fine_objective, _n_eval = repaired._geometry_single_search(
        instance, "FY3", seed=20260723, local_iters=10
    )

    assert record.plan.drop_time > 20.0
    assert fine_objective >= 2.4


def test_global_rescore_exposes_every_smoke_to_every_missile():
    _model, data, _full, repaired = load_stack()
    instance = data.build_instance("full_template", 36)
    records = repaired.reference_p4_records(instance)

    global_records = repaired.global_rescore_records(records)

    assert len(global_records) == len(records) * len(repaired.MISSILE_IDS)
    for missile_id in repaired.MISSILE_IDS:
        assert sum(record.missile_id == missile_id for record in global_records) == len(records)


def test_p5_assignment_candidates_include_canonical_balanced_and_proximity():
    _model, data, _full, repaired = load_stack()
    instance = data.build_instance("full_template", 36)
    canonical = {
        "FY1": ["M1", "M1", "M1"],
        "FY2": ["M2", "M3", "M1"],
        "FY3": ["M3", "M1", "M3"],
        "FY4": ["M1", "M3", "M1"],
        "FY5": ["M2", "M3", "M1"],
    }

    candidates = repaired.p5_assignment_candidates(instance, canonical)

    assert {candidate["label"] for candidate in candidates} >= {
        "canonical",
        "balanced",
        "proximity",
    }
    for candidate in candidates:
        assert set(candidate["assignments"]) == set(repaired.UAV_IDS)
        assert all(len(labels) == 3 for labels in candidate["assignments"].values())


def test_p5_encoder_round_trips_canonical_physical_schedule():
    _model, data, _full, repaired = load_stack()
    instance = data.build_instance("full_template", 36)
    records = repaired._load_canonical_records(PROJECT, "problem5")
    assignments = repaired.assignments_from_records(records)

    vector = repaired.encode_p5_records(records, assignments, instance)
    decoded = repaired.decode_p5_candidate(vector, assignments, instance)

    original = sorted(records, key=lambda record: (record.uav_id, record.slot))
    reconstructed = sorted(decoded, key=lambda record: (record.uav_id, record.slot))
    assert len(original) == len(reconstructed)
    for expected, actual in zip(original, reconstructed, strict=True):
        assert actual.plan.theta == pytest.approx(expected.plan.theta, abs=1e-7)
        assert actual.plan.speed == pytest.approx(expected.plan.speed, abs=1e-5)
        assert actual.plan.drop_time == pytest.approx(expected.plan.drop_time, abs=1e-5)
        assert actual.plan.delay == pytest.approx(expected.plan.delay, abs=1e-5)


def test_p5_fairness_score_prioritizes_the_minimum_duration():
    _model, _data, _full, repaired = load_stack()
    balanced = {"objective": 12.0, "T_i": {"M1": 4.0, "M2": 4.0, "M3": 4.0}}
    lopsided = {"objective": 20.0, "T_i": {"M1": 10.0, "M2": 9.0, "M3": 1.0}}

    assert repaired.p5_score(balanced, "fairness") > repaired.p5_score(lopsided, "fairness")
    assert repaired.p5_score(lopsided, "total") == 20.0


def test_decision_items_reconstruct_a_constraint_valid_schedule():
    _model, data, _full, repaired = load_stack()
    instance = data.build_instance("full_template", 36)
    payload = json.loads((PROJECT / "results" / "problem5" / "values.json").read_text())

    records = repaired.records_from_decision_items(payload["decision"]["smokes"])

    repaired.verify_shared_flight(records)
    evaluation = repaired.m3_full.evaluate_records(
        instance, repaired.global_rescore_records(records), list(repaired.MISSILE_IDS), 0.05
    )
    assert evaluation["objective"] >= 15.1
