# Shadow Portfolio Contract

`scripts/shadow_portfolio.py` implements the R3 offline shadow orchestrator.
It answers only which candidate a calibrated selector would recommend after
hard-pass admission. It does not call a model, change the mainline candidate,
or authorize delivery.

## Required upstream reports

`shadow-portfolio-manifest-v1` hash-pins both upstream reports by relative path
and file SHA-256:

- `judge-hard-gate-calibration-report-v1` must be `hard_gate_ready=true` for a
  ready R3 report, retain manual-authorization policy, and bind the hard-gate
  evaluator identity.
- `selector-reliability-v1` must be `comparison_ready_human=true` for a ready
  R3 report, retain advisory-only policy, and bind the R0a fingerprint it used.

R0a and R0b are separate evaluators. Every candidate must match the R0a
fingerprint, while every selector decision must match the R0b fingerprint.
The R3 cohort's `family_id` values must be disjoint from all R0b dev/holdout
families.

## Candidate admission

Each candidate binds immutable method-stream, code, solver receipt, canonical
result, packet, and PDF SHA-256 values, plus explicit budget and seed. Both
math and execution hard-gate decisions must be `PASS`, and
`r1_r2_hard_pass` must be true. Other candidates remain in admission-rate
statistics but cannot enter selector metrics.

Pair decisions bind two candidates with the same `problem_identity`, the exact
selector fingerprint, a canonical `score(A)-score(B)` margin, the actual
mainline candidate, and any R1 conflicts. The decision must follow the R0b
`tie_band` exactly. R1 conflicts produce `R1_VETO`; hard-gate failures produce
`HARD_GATE_BLOCKED`; neither is counted as a selector win.

Budget ratios above the pre-registered limit are reported and block readiness.
The report includes hard-pass admission, directional coverage, TIE rate,
mainline disagreement, R1 conflicts, budget imbalance, and metrics grouped by
candidate count K.

## Independent adjudication

Adjudications are separate from selector decisions and must be blind to the
selector output. Each entry records source, timestamp, method, and the winning
candidate or `TIE`. Gate 2 output is rejected as a source. Reports compute
Wilson-bounded selector win and regret rates; every mainline disagreement must
be adjudicated before `portfolio_ready` can be true.

The manifest also requires a Gate 2 isolation receipt SHA-256, a Gate 2
evaluator fingerprint distinct from the selector fingerprint, and explicit
confirmation that selector recommendation, candidate scores, and rejected
candidate identity are hidden from Gate 2. The report preserves these bindings
instead of asserting isolation as an unproved constant.

The report always contains:

```json
{
  "advisory_only": true,
  "automatic_switch_performed": false,
  "operator_authorization_required": true,
  "production_selection_authorized": false,
  "gate2_isolated": true,
  "selector_labels_from_gate2": false
}
```

## CLI

```bash
python3 scripts/shadow_portfolio.py \
  evaluation/selector_runs/<run-id>/shadow_portfolio_manifest.json \
  --json-output evaluation/selector_runs/<run-id>/shadow_portfolio_report.json
```

With `--require-ready`, exit status `0` means all pre-registered shadow checks
passed, `3` means the manifest/report is valid but not ready, and `2` means the
input contract is invalid. Even status `0` remains advisory and requires a
separate human cutover authorization receipt.
