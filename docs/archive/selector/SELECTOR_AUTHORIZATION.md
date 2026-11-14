# Selector Cutover Authorization Contract

`scripts/selector_cutover_authorization.py` validates a human-issued
`selector-cutover-authorization-v1` receipt. It never creates approval and
never edits workflow routing.

The receipt hash-pins ready R0a, R0b, and R3 reports. The R3 report must bind
the same R0a/R0b file hashes, remain advisory-only, and report
`production_selection_authorized=false`. Hard-gate and selector evaluator
identities are checked independently against all three reports.
The R3 Gate 2 isolation receipt, distinct evaluator fingerprint, and hidden
selector fields are checked again before an authorization can validate.

An authorization is valid only while its timezone-aware approval window is
active and `revoked=false`. The first rollout must be `canary_only=true`. Scope
must explicitly list workflow steps, projects, problem types, maximum K,
budget-policy and packet-builder hashes, and the exact calibrated TIE band.
Wildcard or implicit project scope is not accepted.

```bash
python3 scripts/selector_cutover_authorization.py \
  evaluation/selector_runs/<run-id>/selector_cutover_authorization.json \
  --json-output evaluation/selector_runs/<run-id>/authorization_assessment.json
```

The assessment can report `authorization_valid=true`, but still contains
`automatic_switch_performed=false` and `route_change_event_required=true`.
Actual routing remains a separate, auditable operation. Expiry, revocation,
identity drift, report changes, scope mismatch, or readiness regression
invalidates the receipt and returns exit status 2.
