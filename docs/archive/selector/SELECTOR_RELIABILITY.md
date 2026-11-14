# Pairwise Selector Reliability Contract

`scripts/selector_calibration.py` implements the offline R0b calibration
contract. It consumes a frozen `selector-calibration-manifest-v1` and emits a
hash-bound `selector-reliability-v1` report. The report is advisory only and
does not alter candidate routing or delivery state.

## Scope and identity

Every manifest must declare `run_id`, `dataset_version`, `frozen: true`, and
`holdout_unsealed: false`. The evaluator identity is the exact
`model`/`backend`/`prompt_sha256`/`schema_sha256` tuple used to produce all
observations. Both candidates in every pair must provide immutable
`packet_sha256`, `pdf_sha256`, a matching R0a
`hard_gate_identity_fingerprint`, and a `quality_receipt_sha256` for the
R1+R2-min hard-pass evidence. A candidate with `hard_pass: false`, a missing
receipt, or a different R0a identity is rejected.

The same `family_id` may not occur in both `dev` and `holdout`; candidate IDs
also may not cross the split boundary. This prevents a rewrite, derivative, or
near-duplicate paper from leaking its label into the holdout.

## Blind observation format

`pairs[*].observations` contains only selector-visible data:

```json
{
  "run_id": "pair-001-AB-1",
  "orientation": "AB",
  "status": "OK",
  "winner": "A",
  "margin": 0.42,
  "evaluator_identity_fingerprint": "<sha256>"
}
```

`margin` is canonical `score(A) - score(B)`, independent of display order.
`status` is `OK`, `FORMAT_ERROR`, or `INDETERMINATE`; failed observations stay
in the denominator. Each pair requires a balanced AB/BA design and at least
`minimum_repeats_per_orientation` observations per orientation (minimum 2).
Every observation must bind both candidate packet hashes exactly.
Labels, school/award fields, and filesystem paths are forbidden inside an
observation. Calibration labels are a separate top-level `labels` array and
are not passed to the selector aggregation function.

Labels include `kind: proxy|human`, winner, source, timestamp, and adjudication
method. Human labels must set both `blind: true` and `selector_blinded: true`.
Proxy labels must declare their limited `proxy_scope`; they cannot establish
natural-paper quality validity.

## Metrics and readiness

The dev split selects `tie_band` from the pre-frozen
`thresholds.tie_band_candidates` list. Selection uses dev known-order accuracy,
coverage, AB/BA flip rate, and then the smallest band as deterministic
tie-breakers. Holdout is never used to tune this value. For
`abs(median_margin) <= tie_band`, the aggregate decision is `TIE`; no candidate
is selected. Directional decisions are only emitted outside the band.

Reports include Wilson intervals for known-order accuracy, coverage, AB/BA
flips, repeated-run flips, format failures, and indeterminate observations;
margin error distribution; quality-axis pair results; and all
`must_not_miss` reversals. Readiness is computed independently for proxy and
human holdout labels:

- `comparison_ready_proxy` is limited to the declared deterministic mutation
  scope.
- `comparison_ready_human` is the only readiness that can support natural
  candidate selection, and requires an independent blind holdout.
- `comparison_ready` mirrors human readiness and remains false when human
  evidence is absent.

Any failed contract, identity drift, split leakage, missing orientation,
format/indeterminate threshold violation, or must-not-miss reversal produces
`comparison_ready=false`. Reports always contain
`advisory_only=true`, `automatic_switch_performed=false`,
`operator_authorization_required=true`, and
`production_selection_authorized=false`.

## CLI

```bash
python3 scripts/selector_calibration.py \
  evaluation/selector_runs/<run-id>/selector_manifest.json \
  --json-output evaluation/selector_runs/<run-id>/selector_reliability.json
```

Use `--require-ready` only for an operator check. Exit status `0` means the
report was generated (and, when requested, human readiness passed), `3` means
the report is valid but not ready, and `2` means the manifest contract was
invalid. No exit status authorizes production routing.
