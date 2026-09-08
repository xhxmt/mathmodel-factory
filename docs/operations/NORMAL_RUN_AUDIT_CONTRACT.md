# Normal-run audit contracts

Current contract for new native runs after the 2026-09-08 follow-up repairs.
`STEPS.md` remains the workflow authority; `modeling_guide.md` owns modeling and
solver rules, and `web/README.md` owns the Web control plane. Historical audit
reports describe their dated candidates, not the current implementation.

## Scope and gates

Start a new project without creating or inheriting Step13 technical continuation
grants. Use the normal scheduler, ordinary recovery and configured attempt limits.
Step13 math precheck does not authorize final delivery. Step16 still requires
its native prerequisites, content freeze, final input evidence and delivery fence.
The removed extra repair retry does not grant another attempt when inputs or code
change. The separate manual technical-continuation implementation has not been
repaired or accepted for this normal-run scope.

## Evidence contracts

- State, checkpoint, dirty-clear records and events commit together. Schema
  migration finishes before business or status-read transactions.
- Judge call archives retain template and final sent prompt separately. API
  preparation and the runner use the same formatter, recheck its hash before
  dispatch, and seal final input, response and metadata. Reuse verifies all
  frozen bytes and current configuration. Unsealed attempts cannot be reused.
- Packet version 5 uses one alias resolver across coverage, grounding and
  aggregation. An alias must have the same complete source bytes and chunk
  identities as its directly included target. Missing, truncated or differing
  evidence remains incomplete. Exact quote validation still applies.
- Execution packets derive the adopted solver chain from canonical claims,
  canonical result provenance and execution declarations. Inputs, script,
  submission/completion receipts, input closure and adopted outputs consume the
  packet budget. Missing or stale links produce explicit incomplete evidence;
  a small selected set cannot prove that all required evidence was selected.
- Primitive numeric `.npy`/`.npz` evidence uses `numpy-review-capsule-v1`.
  The original required path retains raw SHA-256/size; `binary_review` binds it
  to a full deterministic decoding and its separate included hash/byte count.
  The capsule contains the raw bytes and every member/index/value, so grounding
  can regenerate and compare the entire decoding without host filesystem access.
  Identical sources can share one verified capsule through the normal alias
  resolver. Raw encoding and decoded values both consume the existing budget.
  `modeling_guide.md` lists exact dtype/resource limits. Unsupported types,
  Parquet and over-budget sources remain required and incomplete; the decoder
  never calls pickle or executes array objects. Decoding is not scientific validation.
- New local Python receipts use `python-audit-open-v2`. Reading or appending to
  a preexisting output requires explicitly declaring that path as an input.
  Submission preserves the initial bytes under `.factory/solver_inputs/`; the
  completion records initial input identity separately from final output identity.
  Receipt-verified initial snapshots enter final reproducibility inputs and the
  submission bundle individually (native ownership v3). Generated files can be
  read back after creation or truncating replacement. Untracked native-library
  I/O is outside this Python open-audit claim; scientific validity is separate.
- Every required numeric claim needs a field locator and an accepted candidate.
  Every `key_results[i].value` has its own source/version binding. A bound field
  never exempts the rest of its file. Adopting another source invalidates old
  derived bindings.

## Status and lifecycle

`FactoryService.status`, Web project status and native Web diagnostics use a
shared projection from one SQLite snapshot of state, events and policy. Actual
user CLI `diagnostics` and compatibility `--status` still use older status paths;
N03 is explicitly deferred and these commands do not satisfy the shared audit
field contract. The worker CLI entry is covered separately below. Current workflow
errors are separate from evidence errors. A recovered workflow does not retain a
historical error as its current failure. `scientific_verdict` requires current
verified evidence; `raw_scientific_verdict` is untrusted diagnostic context.
Math precheck uses `judge-precheck-v2` with `audit_binding` and `input_fingerprint`
and is shown as `review_mode=math_only`, without a score or delivery.
`diagnostic_score` is not an official score; `official_score` remains null.
`delivery_allowed` is still controlled by the existing verified delivery fence.

Projection writers serialize, reload the current revision, update compatibility
files and publish `diagnostics/status.json` last with hashes for the checkpoint,
heartbeat and markers. Use `read_compatibility_projection` (also used by
`project_diagnostics.load_status`) to reject a partial file set. Individual files
are compatibility views, not an atomic authoritative database replacement.
The database remains the status source for native Web/API reads.

The normal background entry is `FactoryService.start` → `WorkerLauncher` →
`factory_core.cli worker`. It uses an initialization acknowledgment and a bounded
30-second wait; pre-ready exit, timeout or identity mismatch terminates observed
processes and records `WORKER_START_FAILED`. The actual worker CLI forwards its
ready file after consuming the parent permit; ordinary compat execution has no
ready-file argument. Pause/kill takes state and events from the same snapshot,
matches the latest launch/run event's PID, lease and persisted process identity,
then verifies termination of that owner and observed descendants. Missing or
differing ownership is never replaced with a newly sampled identity: no process
signals are sent and `RUNNER_EXIT_UNVERIFIED` with interrupted state is persisted.
Old records lacking the identity/lease binding require explicit reconciliation;
they do not grant cancellation authority.

The separate `persistent_launcher` helper persists STARTING before launch,
records initialization exceptions and recognizes a missing monitor as INTERRUPTED
with process-tree exit unverified. A missing or reused normal worker PID also
projects interrupted execution; `recorded_workflow_state` retains the database
state for diagnosis, without mutating workflow history on read. SIGKILL cannot guarantee that a dead monitor
continues discovering or cleaning descendants. These bounded lifecycle checks do
not claim that arbitrary unobserved detached processes were found.

## Verification entry points

`tests/test_normal_run_transactions.py`, `test_normal_run_api_batch.py`,
`test_normal_run_packet_chain.py`, `test_normal_run_solver_lifecycle.py`,
`test_normal_run_numeric_bindings.py`, `test_normal_run_status.py` and
`test_normal_run_worker_lifecycle.py`, `test_normal_run_cli_entry.py`,
`test_normal_run_worker_ownership.py` and `test_normal_run_numpy_evidence.py`
exercise the repaired module combinations.
External provider transport is controlled; solver fixtures are small local
producers including NumPy JSON conversion. Existing native orchestration,
scheduler, package, Web and full-repository checks remain applicable.

Engineering checks do not establish model correctness, units, parameter validity
or paper conclusions. A real-provider interface acceptance and a fresh full
problem experiment require their own execution scope and budget.
