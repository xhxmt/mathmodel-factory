# Bounded rerun repair

This is the current operator contract for the 2026-09-06 rerun findings. It does
not authorize Phase9-A, Run4, migration, deployment, delivery or Phase10-B.
All examples require an explicitly selected disposable/test project. Never
run them against the frozen historical experiment.

## Normal retry budget

Each step retains its configured attempt limit. Exhausting that budget stops
the step; changing inputs or implementation does not grant an extra attempt.
The former `repair-retry` command has been removed. Existing attempt/event
history remains intact, and historical repair grants do not extend the budget.
Normal retries within the configured limit and ordinary recovery remain in
place. The manual Step13 technical continuation (`repair_operations
authorize-gate2/continue-gate2`) and its `persistent_launcher` were removed on
2026-11-14. A failed Step is retried with `factory action retry-step`, the
explicit operator reset: it applies only to a FAILED Step at the cursor and
starts that Step again at attempt 1.

## Judge evidence

Native calls freeze all three role packets, objective evidence, effective
prompt, execution step, evaluator implementation/configuration, unique call ID,
response and metadata under `judge_outputs/batches/`. Only the final exclusive
commit marker makes a call reusable. Exact reuse verifies frozen bytes,
current inputs/configuration and the current response/metadata. Changed inputs
start a fresh call; damaged matching records return an evidence-binding error.
No archive is overwritten. The three roles must share one input/configuration
and execution-step identity before aggregation. Step13 template reuse is
recorded separately from actual Step16 execution/model selection.

Quote/chunk validation remains strict. Grounding repair uses the existing
bounded retry and exact packet-derived feedback. Failure to ground an answer
stays INDETERMINATE and produces no official score.

Execution packets deduplicate identical source bytes and retain alias paths
and the canonical citation chunk. Cite the canonical path/chunk for an alias.
Required completeness and overall selected-content coverage are distinct
manifest fields. `COMPLETE` alone never means every selected log was retained.

## Accepted numeric versions

Claim artifacts may declare an exact JSON `field`, such as `metrics.rmse[0]`.
Future-owner declarations are plans; existing/owner-stage artifacts must bind
real paths and fields before validation or judge dispatch.

Register competing numeric sources under one numeric claim ID. After explicitly
deciding which run to accept, use the publicly returned completion receipt:

```sh
python scripts/canonical_claims.py PROJECT --claim Q3B_THICKNESS \
  --source 'results/problem3B/values.json::thickness' \
  --candidate 'results/step12/values.json::thickness' \
  --completion '.factory/solver_receipts/JOB.completed.json' \
  --reason 'scientific justification for selecting this run'
python scripts/canonical_claims.py PROJECT --check
```

The tool verifies current submitted/completed solver evidence and binds the
accepted source hash/field/value to that run. It archives the immutable version
and generates `tables/canonical_claim_values.tex`. Load that file from the main
paper and use `\csname FactoryClaimQ3B_THICKNESS\endcsname` consistently in
summary, body and tables. Derived `key_results` include `claim_id`,
`canonical_version` and the accepted `value` (or its exact canonical_source).
Changing accepted/candidate inputs, receipts or generated values invalidates
the chain; a final snapshot rejects unversioned key results and mixed literal
values from registered alternatives. This check is conservative: discussing an
alternative numeric value in prose requires an explicit modeling/claim contract
revision, not an automatic waiver. It does not infer scientific equivalence
between unrelated, undeclared fields or select the physical truth by objective.

Native local Python jobs use one SolverRequest for argv, declared dependencies,
receipt and audited file-read enforcement. Undeclared project file reads fail
the job and its input-closure report. The claim is explicitly limited to Python
audited project-file opens, not arbitrary native-library I/O; other runtimes
remain declaration-only. NumPy producers use `factory_core.json_values.dumps`
for strict bool/int/float/array normalization and precise field errors.

## Current ownership versus frozen compatibility contracts

Native execution uses `current_artifact_ownership.py` (native v2) and
`current_dirty.py` (native v10). The two exact root evidence rules are additive;
unknown paths still fail closed. Final/submission manifests record the new
ownership schema and new native dirty receipts carry the native classifier
identity, an explicit version (`current_dirty.CLASSIFIER_CONTRACT_VERSION`) that
moves only when a classification changes, not with the source bytes. Normal
rebase preserves historical causes/clear receipts. `artifact_ownership.py` keeps
the v1 registry that the native extension appends to.
Existing native tests use the current classifier for new receipts; historical
identity tests still verify the frozen sources. Additive engine recovery hooks
are below the frozen owner-policy symbol spans, retaining their bytes/anchors.

## Status projection

Status refreshes from the authoritative workflow revision and reports execution
state, workflow error, evidence validity, scientific verdict, diagnostic score
and delivery permission separately. Missing/broken judgment binding suppresses
scientific verdict and score availability; a raw paper score is never an
official score. Old compatibility projection revisions do not override SQLite.
