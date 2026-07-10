# First-Principles Paper Quality Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace artifact-driven false confidence with a verified chain from problem semantics through mathematical claims, executable evidence, independent judging, and a real `CURRENT_PASS` baseline.

**Architecture:** Keep the existing 16-step runner, but add a small declarative quality-contract layer owned by the project rather than hard-coded incident heuristics. Domain oracles and independent executable audits veto delivery; writing and LLM scores remain advisory until core correctness passes. Existing provenance, freshness, and spec/implementation checks are retained where they establish facts, while universalized heuristics are downgraded to diagnostics unless the project explicitly justifies them.

**Tech Stack:** Bash runner, Python 3, pytest, JSON schemas, existing solver wrappers, existing DeepSeek/Claude/Codex judge adapters.

---

## File Boundaries

- `scripts/quality_contract.py`: parse and validate project-authored claims, hard invariants, anomaly checks, and independent oracle commands.
- `scripts/verify_quality_contract.py`: CLI that executes the contract and writes machine-readable/Markdown reports.
- `scripts/domain_oracles/cumcm2025a_occlusion.py`: independent exact line-segment/sphere oracle and fixed-strategy reference computation.
- `scripts/judge_packet.py`: build separated paper-only, math-audit, and execution-audit packets without self-authored evaluation prose.
- `scripts/aggregate_judges.py`: aggregate separate judge runs with correctness vetoes.
- `evaluation/calibration_manifest.json`: labeled real-paper calibration set and pairwise expectations.
- `scripts/evaluate_calibration.py`: measure pairwise accuracy and ranking correlation.
- Existing `run_paper.sh`, `scripts/evaluate_modeling_project.py`, `scripts/delivery_contract.py`: wire the new contract without duplicating state logic.
- Existing method recommendation files: remove uncalibrated “correctness” semantics and expose evidence/confidence honestly.

### Task 1: Correct the 2025A geometric oracle

**Files:**
- Create: `scripts/domain_oracles/__init__.py`
- Create: `scripts/domain_oracles/cumcm2025a_occlusion.py`
- Modify: `tests/test_cumcm2025a_occlusion_model.py`
- Modify: `tests/test_cumcm2025a_cloudrun.py`
- Create: `tests/test_cumcm2025a_exact_oracle.py`

- [ ] **Step 1: Write failing exact-geometry tests**

```python
def test_segment_sphere_detects_endpoint_intersection():
    assert segment_intersects_sphere(
        np.array([0.0, 0.0, 0.0]),
        np.array([1.0, 0.0, 0.0]),
        np.array([1.1, 0.0, 0.0]),
        0.2,
    )

def test_problem1_exact_duration():
    result = solve_problem1_reference(dt_bracket=0.01, endpoint_tol=1e-6)
    assert result.duration == pytest.approx(1.391643, abs=1e-5)
```

- [ ] **Step 2: Run tests and confirm the current `1.36` oracle fails**

Run: `pytest -q tests/test_cumcm2025a_exact_oracle.py tests/test_cumcm2025a_occlusion_model.py`

Expected: exact reference test fails and the legacy `1.36` assertions conflict.

- [ ] **Step 3: Implement an independent quadratic-root line-segment/sphere predicate**

The implementation must solve `||a + s(b-a) - c||^2 = r^2`, accept any root in `[0,1]`, and handle a segment fully inside the sphere.

- [ ] **Step 4: Replace legacy reference assertions**

Use `1.391643` only in offline regression tests. Runtime prompts must not receive this value.

- [ ] **Step 5: Run focused tests**

Run: `pytest -q tests/test_cumcm2025a_exact_oracle.py tests/test_cumcm2025a_occlusion_model.py tests/test_cumcm2025a_cloudrun.py`

Expected: all pass.

### Task 2: Add a declarative quality contract

**Files:**
- Create: `scripts/quality_contract.py`
- Create: `scripts/verify_quality_contract.py`
- Create: `tests/test_quality_contract.py`
- Modify: `prompts/step4_model_construction.txt`
- Modify: `prompts/step5_full_solve.txt`
- Modify: `STEPS.md`

- [ ] **Step 1: Write failing parser and policy tests**

```python
def test_hard_claim_requires_independent_evidence(tmp_path):
    contract = load_contract(write_contract(tmp_path, hard_claim_without_evidence()))
    result = evaluate_contract(contract, tmp_path)
    assert result.passed is False
    assert result.failures[0].code == "MISSING_INDEPENDENT_EVIDENCE"

def test_anomaly_rule_does_not_veto_without_problem_justification(tmp_path):
    contract = load_contract(write_contract(tmp_path, anomaly_only_contract()))
    assert evaluate_contract(contract, tmp_path).passed is True
```

- [ ] **Step 2: Define the project contract schema**

```json
{
  "version": 1,
  "claims": [{
    "id": "P1_OCCLUSION_DURATION",
    "severity": "hard",
    "statement": "Problem 1 duration uses exact segment-sphere intersection",
    "source": "problem/source.md#problem-1",
    "implementation": ["models/.../02_model.py::segment_intersects_sphere"],
    "evidence": [
      {"type": "pytest", "command": "pytest -q tests/domain/test_occlusion.py"},
      {"type": "oracle", "command": "python3 .../cumcm2025a_occlusion.py --verify-project ."}
    ]
  }],
  "anomaly_checks": [{"type": "nonzero_each", "justification": "problem-specific text"}]
}
```

- [ ] **Step 3: Implement parser, command execution, freshness, and reports**

The verifier returns nonzero only for hard-claim failures. Anomaly checks are warnings unless `hard: true` and `justification` is nonempty.

- [ ] **Step 4: Replace universal prompt assertions**

Remove statements that universally require strict resource gains, nonzero contribution for every resource, or a fixed `250*n_vars^2` budget as proof. Require projects to justify these in `quality_contract.json`.

- [ ] **Step 5: Run tests**

Run: `pytest -q tests/test_quality_contract.py tests/test_quality_gates_regression.py`

Expected: all pass.

### Task 3: Wire correctness vetoes into delivery

**Files:**
- Modify: `run_paper.sh`
- Modify: `scripts/evaluate_modeling_project.py`
- Modify: `scripts/delivery_contract.py`
- Modify: `tests/test_quality_gates_regression.py`
- Modify: `tests/test_delivery_contract.py`

- [ ] **Step 1: Add failing runner/evaluator tests**

Assert that Step 10 and Step 16 reject a failed `quality_contract` even when Gate 2 says PASS, and that a warning-only contract remains deliverable.

- [ ] **Step 2: Add `quality_contract_gate_passed()`**

Run `verify_quality_contract.py`, persist `quality_contract_verification.latest.json` and `.txt`, and require freshness against contract inputs.

- [ ] **Step 3: Add evaluator and manifest evidence**

Expose a `quality_contract_gate` check and include its result and hashes in `delivery_manifest.json`.

- [ ] **Step 4: Run focused tests and shell syntax checks**

Run: `pytest -q tests/test_quality_gates_regression.py tests/test_delivery_contract.py`

Run: `bash -n run_paper.sh`

Expected: all pass.

### Task 4: Repair method recommendation semantics

**Files:**
- Modify: `web/backend/modeling_direction_service.py`
- Modify: `web/frontend/src/components/ModelingDirectionPanel.vue`
- Modify: `scripts/method_fit_score.py`
- Modify: `tests/test_modeling_direction_service.py`
- Create: `tests/test_method_fit_score.py`

- [ ] **Step 1: Write failing tests for misleading scores**

```python
def test_direction_output_does_not_claim_correctness_probability():
    result = build_modeling_directions(project, root)
    assert "correctness_score" not in result["directions"][0]
    assert result["directions"][0]["evidence_level"] in {"none", "weak", "moderate", "strong"}
```

- [ ] **Step 2: Replace fabricated correctness/feasibility percentages**

Return retrieval score, matched terms, required-data coverage, known failure modes, historical sample count, and `evidence_level`. Do not present 0–100 correctness claims.

- [ ] **Step 3: Reject unusable historical training rows**

Exclude projects without `CURRENT_PASS`, projects whose feature extraction yields zero variables/constraints for a nontrivial problem, and method paths absent from the current registry.

- [ ] **Step 4: Update UI labels and selection records**

Write `Evidence level`, `Historical samples`, and `Known risks` into `human_review.md`.

- [ ] **Step 5: Run backend/frontend focused tests**

Run: `pytest -q tests/test_modeling_direction_service.py tests/test_method_fit_score.py`

Expected: all pass.

### Task 5: Build genuinely separated judge packets

**Files:**
- Create: `scripts/judge_packet.py`
- Create: `scripts/aggregate_judges.py`
- Create: `prompts/judges/math_auditor.txt`
- Create: `prompts/judges/execution_auditor.txt`
- Create: `prompts/judges/paper_reviewer.txt`
- Modify: `evaluation/run_evaluation.sh`
- Modify: `prompts/step13_gate2_judge.txt`
- Create: `tests/test_judge_packet.py`
- Create: `tests/test_aggregate_judges.py`

- [ ] **Step 1: Write packet-separation tests**

The paper reviewer packet must exclude `evaluation.md`, `judge_evaluation.md`, `review_comments.md`, and `revision_summary.md`. The math packet includes the problem statement and model/code references. The execution packet includes commands and machine reports.

- [ ] **Step 2: Implement three packet builders**

Each packet has a stable JSON manifest containing included files and hashes.

- [ ] **Step 3: Implement verdict aggregation**

`math=FAIL` or `execution=FAIL` vetoes delivery. Paper score cannot average away correctness failures. Missing/malformed judge output is `INDETERMINATE`, not PASS.

- [ ] **Step 4: Replace same-context role play**

Step 13 invokes independent judge calls with separate prompts/contexts and stores separate outputs before aggregation.

- [ ] **Step 5: Run tests**

Run: `pytest -q tests/test_judge_packet.py tests/test_aggregate_judges.py`

Expected: all pass.

### Task 6: Calibrate evaluation against real awarded papers

**Files:**
- Create: `evaluation/calibration_manifest.json`
- Create: `scripts/evaluate_calibration.py`
- Create: `tests/test_evaluate_calibration.py`
- Modify: `evaluation/README.md`
- Modify: `evaluation/baseline_scores.md`

- [ ] **Step 1: Register available labeled papers**

Include national first, provincial first, provincial third, and generated baselines with paths and problem identifiers. Do not expose answer material to runtime agents.

- [ ] **Step 2: Write failing pairwise metric tests**

Test pairwise accuracy, tie handling, coverage, and Kendall-style ordering output.

- [ ] **Step 3: Implement calibration reporting**

Report pairwise award-order accuracy, malformed-output rate, fatal-flaw detection rate, and per-problem coverage. Absolute scores remain secondary.

- [ ] **Step 4: Run tests and generate the offline report**

Run: `pytest -q tests/test_evaluate_calibration.py`

Run: `python3 scripts/evaluate_calibration.py evaluation/calibration_manifest.json --existing-results`

Expected: deterministic report; missing evaluations are explicitly `MISSING`, never silently omitted.

### Task 7: Produce a real CURRENT_PASS baseline

**Files:**
- Modify/create inside a dedicated repair copy of `complete/cumcm_2025_a_blind` or the smallest-gap project under `ongoing/`
- Modify: `complete/_validation_index.json` only through `scripts/audit_complete_projects.py`
- Test: project-specific oracle and quality-contract tests

- [ ] **Step 1: Create a repair copy rather than mutating historical evidence**

Use a new project name such as `cumcm_2025_a_current_pass`.

- [ ] **Step 2: Replace the geometric predicate and regenerate canonical results**

Do not patch paper numbers manually. Re-run solver and post-processing from the corrected implementation.

- [ ] **Step 3: Add project-specific quality contract and independent evidence**

Include exact P1 oracle, per-resource diagnostics, convergence evidence, and cross-algorithm checks.

- [ ] **Step 4: Regenerate paper, attachments, judges, and delivery manifest**

Use the normal runner paths.

- [ ] **Step 5: Audit all completed projects**

Run: `python3 scripts/audit_complete_projects.py --write-manifests`

Expected: at least one `CURRENT_PASS`, with no failed hard correctness checks.

### Task 8: Final verification and documentation cleanup

**Files:**
- Modify: `README.md`
- Modify: `STEPS.md`
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Run the focused verification bundle**

Run the oracle, quality contract, delivery, method recommendation, judge packet, calibration, and project audit tests.

- [ ] **Step 2: Run syntax and compile checks**

Run: `bash -n run_paper.sh evaluation/run_evaluation.sh`

Run: `python3 -m py_compile scripts/quality_contract.py scripts/verify_quality_contract.py scripts/judge_packet.py scripts/aggregate_judges.py scripts/evaluate_calibration.py`

- [ ] **Step 3: Confirm no stale claims remain**

Search for `1.36`, `correctness_score`, “three judges” role-play language, and claims that `complete/` implies success. Every remaining occurrence must be historical/answer-key material or explicitly qualified.

- [ ] **Step 4: Refresh the project audit**

Run: `python3 scripts/audit_complete_projects.py --no-write`

Expected: the new baseline is `CURRENT_PASS`; legacy projects remain labeled honestly.

## Self-Review

- Spec coverage: correct oracle, declarative contracts, delivery veto, recommendation semantics, separated judges, real-paper calibration, positive baseline, and final audit all have explicit tasks.
- Placeholder scan: no deferred implementation placeholders remain.
- Scope control: existing runner architecture is retained; the plan avoids a full rewrite.
