# Repeated-judge reliability (no human labels)

[`scripts/judge_reliability.py`](../scripts/judge_reliability.py) measures the
repeatability of a fixed evaluator on an immutable packet.  It is deliberately
not a human-calibration or award-prediction tool: a unanimous model can still
be consistently wrong.

The input uses `judge-reliability-input-v1` and must bind every run to the same
lower-case SHA-256 packet identity (or the complete role-specific
`packet_fingerprints` map).  For production use, set
`required_roles: ["math", "execution", "paper"]`; omit it only for a clearly
labelled partial role diagnostic.  A repeat-centric form (`repeats` with
`sample_id`, packet identity, and per-role `decisions`) is useful for the
three-role runtime; role-centric `roles[*].runs` is supported for small
harnesses.  The two forms must not be mixed.

```bash
python3 scripts/judge_reliability.py evaluation/reliability_input.json \
  --output evaluation/reliability_report.json --min-runs 3
```

The report exposes modal and pairwise agreement, entropy, empirical score
spread (median/range/MAD/IQR), coverage, and a nominal-alpha field.  Alpha is
`UNKNOWN` for a single packet; a numeric value requires an explicitly marked
batch of distinct packet identities.  Hard roles use fail-closed logic: any
validated `FAIL` remains a veto, while only complete unanimous `PASS` can pass;
other cases are `INDETERMINATE`.  Paper scores and dimensions are
`UNCALIBRATED_DIAGNOSTIC_ONLY`, and `workflow_gate_eligible` is always false.
If runs provide `pair_id`, `orientation=AB|BA`, and displayed `winner=A|B`,
the report also emits an AB/BA position-consistency diagnostic; incomplete or
malformed pairs become `UNKNOWN` rather than being dropped.
