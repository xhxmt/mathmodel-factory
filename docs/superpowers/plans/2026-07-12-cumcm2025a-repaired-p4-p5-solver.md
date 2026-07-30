# CUMCM 2025A Repaired P4/P5 Solver Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement and run a corrected P4/P5 solver with full physical time horizons, structurally feasible drop schedules, exact joint smoke coverage, and auditable multi-start results.

**Architecture:** Keep the delivered project immutable and add a repaired experimental solver inside the isolated copy. Reuse the existing motion and finite-segment geometry primitives, but replace P4/P5 search-domain construction and schedule decoding. Store experimental results separately from canonical delivery artifacts.

**Tech Stack:** Python 3, NumPy, SciPy differential evolution, pytest, JSON.

---

### Task 1: Add regression tests for the corrected model contract

**Files:**
- Create: `complete/cumcm_2025_a_current_pass/tests/test_repaired_p4_p5_solver.py`
- Create: `complete/cumcm_2025_a_current_pass/models/m3_milp_pso/10_repaired_p4_p5.py`

- [ ] **Step 1: Write failing tests for full P4 time bounds and ordered drops**

```python
def test_p4_bounds_cover_late_fy3_drop():
    bounds = repaired.problem4_bounds(instance)
    assert bounds[10][1] >= 32.141

def test_ordered_drop_decoder_preserves_horizon_and_one_second_gaps():
    drops = repaired.decode_ordered_drops([0.95, 0.10, 0.50], horizon=60.0)
    assert drops[1] - drops[0] >= 1.0
    assert drops[2] - drops[1] >= 1.0
    assert drops[-1] <= 60.0
```

- [ ] **Step 2: Write a failing regression test for the published P4 strategy**

```python
def test_reference_p4_strategy_scores_above_ten_seconds_under_current_geometry():
    records = repaired.reference_p4_records(instance)
    evaluation = full.evaluate_records(instance, records, ["M1"], 0.05)
    assert evaluation["objective"] >= 10.9
```

- [ ] **Step 3: Run tests and verify RED**

Run:

```bash
python3 -m pytest -q complete/cumcm_2025_a_current_pass/tests/test_repaired_p4_p5_solver.py
```

Expected: import or attribute failures because the repaired solver does not exist.

### Task 2: Implement corrected bounds and feasible decoders

**Files:**
- Create: `complete/cumcm_2025_a_current_pass/models/m3_milp_pso/10_repaired_p4_p5.py`
- Test: `complete/cumcm_2025_a_current_pass/tests/test_repaired_p4_p5_solver.py`

- [ ] **Step 1: Implement missile horizons and per-UAV delay bounds**

```python
def missile_hit_time(instance, missile_id):
    initial = np.asarray(instance["missiles"][missile_id], dtype=float)
    return float(np.linalg.norm(initial) / instance["parameters"]["missile_speed_mps"])

def max_burst_delay(instance, uav_id):
    z0 = float(instance["uavs"][uav_id][2])
    g = float(instance["parameters"]["gravity_mps2"])
    return math.sqrt(2.0 * z0 / g)
```

- [ ] **Step 2: Implement P4 bounds using the full M1 horizon**

```python
def problem4_bounds(instance):
    horizon = missile_hit_time(instance, "M1")
    bounds = []
    for uav_id in ("FY1", "FY2", "FY3"):
        bounds.extend([
            (0.0, 2.0 * math.pi),
            (70.0, 140.0),
            (0.0, horizon),
            (0.0, max_burst_delay(instance, uav_id)),
        ])
    return bounds
```

- [ ] **Step 3: Implement ordered-drop decoding with a finite horizon**

```python
def decode_ordered_drops(raw, horizon):
    q = np.sort(np.clip(np.asarray(raw, dtype=float), 0.0, 1.0))
    slack = horizon - 2.0
    return np.asarray([slack * q[0], 1.0 + slack * q[1], 2.0 + slack * q[2]])
```

- [ ] **Step 4: Run tests and verify GREEN**

Run the focused test file and expect all tests to pass.

### Task 3: Implement and run the repaired P4 solver

**Files:**
- Modify: `complete/cumcm_2025_a_current_pass/models/m3_milp_pso/10_repaired_p4_p5.py`
- Test: `complete/cumcm_2025_a_current_pass/tests/test_repaired_p4_p5_solver.py`
- Create at runtime: `complete/cumcm_2025_a_current_pass/results/repaired/p4_result.json`

- [ ] **Step 1: Write a failing test that P4 candidates are selected by fine-grid score**

```python
def test_repaired_p4_uses_fine_grid_to_rank_candidates(monkeypatch):
    candidates = repaired.rank_p4_candidates([...])
    assert candidates[0]["fine_objective"] >= candidates[1]["fine_objective"]
```

- [ ] **Step 2: Implement geometry-guided and global P4 starts**

Use the current P2 solution for FY1, single-smoke searches across the full horizon for FY2/FY3, and the independently reconstructed late-window candidate as a validation seed. Do not narrow the hard bounds around these starts.

- [ ] **Step 3: Implement multi-start DE and Top-K fine rescoring**

Search at `dt=0.10`, retain the best candidates from every seed, rescore at `dt=0.02`, and report individual smoke contributions and union intervals.

- [ ] **Step 4: Run P4 solver**

```bash
python3 complete/cumcm_2025_a_current_pass/models/m3_milp_pso/10_repaired_p4_p5.py --project complete/cumcm_2025_a_current_pass --only p4
```

Expected: a feasible P4 result above 10 seconds, with a late FY3 window represented.

### Task 4: Implement a feasible P5 search and global rescore

**Files:**
- Modify: `complete/cumcm_2025_a_current_pass/models/m3_milp_pso/10_repaired_p4_p5.py`
- Test: `complete/cumcm_2025_a_current_pass/tests/test_repaired_p4_p5_solver.py`
- Create at runtime: `complete/cumcm_2025_a_current_pass/results/repaired/p5_result.json`

- [ ] **Step 1: Write failing tests for P5 shared-flight and optional-slot constraints**

```python
def test_p5_decoder_shares_heading_and_speed_and_allows_unused_slots():
    records = repaired.decode_p5_candidate(vector, assignment, instance)
    repaired.verify_shared_flight(records)
    assert len(records) <= 15
```

- [ ] **Step 2: Implement multiple assignment candidates**

Include proximity, current MILP assignment, balanced variants, and mutations. Treat assignment as initialization, then globally rescore every active smoke against every missile.

- [ ] **Step 3: Implement two-stage objective reporting**

Optimize and report both total-duration and fairness-oriented candidates. Do not claim weighted epsilon scoring is exact lexicographic optimization.

- [ ] **Step 4: Run bounded P5 experiments**

Start with reduced but real multi-start budgets, preserve solver provenance, and extend only if objective improvements remain material.

### Task 5: Verification and result comparison

**Files:**
- Modify: `complete/cumcm_2025_a_current_pass/tests/test_repaired_p4_p5_solver.py`
- Create: `complete/cumcm_2025_a_current_pass/results/repaired/comparison.json`

- [ ] **Step 1: Run all project-focused tests**

```bash
python3 -m pytest -q \
  complete/cumcm_2025_a_current_pass/tests/test_exact_occlusion.py \
  complete/cumcm_2025_a_current_pass/tests/test_result_contract.py \
  complete/cumcm_2025_a_current_pass/tests/test_problem4_multistart.py \
  complete/cumcm_2025_a_current_pass/tests/test_repaired_p4_p5_solver.py
```

- [ ] **Step 2: Compare repaired and delivered results**

Record P4/P5 objectives, per-missile durations, intervals, constraint checks, evaluation budgets, and random seeds. Explicitly label all new outputs experimental rather than CURRENT_PASS canonical artifacts.

- [ ] **Step 3: Verify reproducibility**

Rerun the selected best candidates through the fine evaluator and require identical values within the configured discretization tolerance.
