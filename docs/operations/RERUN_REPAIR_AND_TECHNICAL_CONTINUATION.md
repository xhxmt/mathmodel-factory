# Bounded rerun repair and technical continuation

This is the current operator contract for the 2026-09-06 rerun findings. It does
not authorize Phase9-A, Run4, migration, deployment, delivery or Phase10-B.
All examples require an explicitly selected disposable/test project. Never
run them against the frozen historical experiment.

## Repairing an exhausted attempt

After fixing an input or implementation, use the exact current revision:

```sh
python -m factory_core.repair_operations repair-retry PROJECT \
  --expected-revision REVISION --reason 'describe the actual repair'
```

The workflow records one additional attempt bound to the failed attempt,
authored input fingerprint and implementation bytes. Historical attempts and
the normal maximum remain unchanged. Unchanged inputs/code, a repeated repair
version, live runners, missing baselines and missing upstream claim artifacts
are rejected. The next normal runner consumes this opportunity through its
monotonically increasing attempt number. A changed upstream owner can still
trigger normal semantic invalidation; repair authorization does not disable it.
The failed input baseline must match the exact current stage/subtask/source
step, and only that failed attempt's start event attests its implementation.
Claim checks cover the current owner's stage and earlier dependencies; future
owner declarations are not mistaken for missing inputs of the repair target.

## Step13 unsuccessful, then evaluate Steps14–16

Before the selected Step13 attempt, record the explicit local operator intent:

```sh
python -m factory_core.repair_operations authorize-gate2 PROJECT \
  --expected-revision REVISION --reason 'evaluate downstream despite Step13 outcome'
```

On FAIL, REVISE, INDETERMINATE, infrastructure failure or a reopen outcome, the
engine preserves the actual failure/reopen event and pauses before running an
upstream step. Continue from the returned revision:

```sh
python -m factory_core.repair_operations continue-gate2 PROJECT \
  --expected-revision PAUSED_REVISION
```

This consumes the event-bound grant exactly once. Step14/15 execute their
prepare/execute/validate contracts with Stage9 identity. Step16 uses Stage10
identity and the analysis-only audit service. Each technical outcome has its
own event; failed checks stop the route. Step13 receives no success event and
the production completion cursor is not advanced. The workflow ends paused,
even if the final scientific verdict is PASS. Ordinary production resumption
still requires the normal workflow and human gates.

This local technical authorization is separate from administrator delivery
overrides, Web access permissions and per-project human decisions. It never
resolves a pending human gate or permits acceptance/release side effects.

## Persistent local launcher

Use a unique external control directory instead of a short-lived shell parent:

```sh
python -m factory_core.persistent_launcher start CONTROL_ROOT --cwd SOURCE_ROOT \
  --key UNIQUE_REQUEST --timeout 3600 -- python -m factory_core.repair_operations \
  continue-gate2 PROJECT --expected-revision PAUSED_REVISION
python -m factory_core.persistent_launcher status CONTROL_ROOT
python -m factory_core.persistent_launcher cancel CONTROL_ROOT
```

The detached Linux monitor owns a lifetime file lock, an immutable command
request, readiness status and separate stdout/stderr logs. An exact repeated
start returns the same run; a different request requires a new directory.
Cancellation records verified process-tree exit, including adopted orphaned
descendants. A timeout or interrupted status is not a scientific verdict.
Do not reuse or delete an interrupted control directory to conceal its history.

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
ownership schema and new native dirty receipts carry a distinct source-bound
classifier identity. Normal rebase preserves historical causes/clear receipts.
`artifact_ownership.py` and `dirty.py` remain byte-frozen v1/v9 compatibility
sources for the M0.2/M0.3 prototype; their registry indices and identity goldens
are not updated or represented as approval for the new native contracts.
Existing native tests use the current classifier for new receipts; historical
identity tests still verify the frozen sources. Additive engine recovery hooks
are below the frozen owner-policy symbol spans, retaining their bytes/anchors.

## Status projection

Status refreshes from the authoritative workflow revision and reports execution
state, workflow error, evidence validity, scientific verdict, diagnostic score
and delivery permission separately. Missing/broken judgment binding suppresses
scientific verdict and score availability; a raw paper score is never an
official score. Old compatibility projection revisions do not override SQLite.
