# Orchestration Engine

This document owns the current runtime-state, migration, and recovery contract.
`STEPS.md` continues to own Step artifacts and quality gates.

## Authority

New and explicitly migrated modeling projects store authoritative workflow
state in `.factory/state.db`. Each transition uses one SQLite transaction to:

1. compare the caller's expected `revision`;
2. append an immutable event;
3. update the project snapshot and increment `revision`.

The snapshot records project identity, control mode, scheduler/catalog
generation, completed and active Stage/subtask/source Step, the compatibility
Step cursor, attempt, pending action, runner lease/PID, storage scope, and
timestamps. Database triggers reject event updates and deletes. Event payloads
redact secret-, token-, password-, credential-, and API-key-shaped fields. New
events retain their legacy type/payload fields and add a versioned `_workflow`
envelope containing a canonical event type, structured Gate reason, replay-state
patch, SHA-256 of the stable pre/post-transition state, distinct subject/result
Stage/subtask/Step coordinates, and an aggregate side-table root. A first event
or an older-schema cutover event is a full replay snapshot; later events are
merge patches. Event v1 remains replay-compatible; new writes use event v2.

Schema v9 also stores current and append-only historical Stage checkpoints,
machine-owned semantic dirty causes/flags and classifier-bound clear receipts,
an optional `contest_policy`, immutable Human Decision requests by generation,
and append-only decision instances. Rejection opens a new request generation;
only an explicit `approved=true` instance satisfies an Approval gate. It adds
recoverable projection-failure records, source-bound projector snapshots and
Solver job idempotency/request/Stage ownership columns. New projects receive `contest_core_v1`: a
74-hour final deadline, T−6h content freeze, T−2h delivery freeze, and six-hour
delivery reserve. Existing projects upgraded without a policy remain
unbounded. Step 3, content freeze, and post-freeze reopen decisions are
authoritative in SQLite; their JSON/Markdown forms are projections.

The project database does not grant Web access. `web/auth.db` is a separate
control-plane database: `project_acl` grants project access, `showcase_acl`
grants read-only display visibility, and `delivery_overrides` grants one scoped
operational exception. Conversely, those grants do not choose a method,
resolve a Human Gate, advance a scheduler cursor, or replace the immutable
decision request/instance in the project database. See
[`decisions/ADR-0001-phase0-source-truth.md`](decisions/ADR-0001-phase0-source-truth.md).

Step outputs remain validation evidence. `checkpoint.md`, `.heartbeat`,
`.paused`, `.killed`, `.runner.pid`, and `diagnostics/status.json` are generated
compatibility projections for migrated projects. Web and CLI readers must not
derive migrated state from those files.

## Execution And Recovery

`run_paper.sh` is a compatibility launcher. Engine-controlled `native_v2`
projects route to `FactoryService`/`FactoryEngine`; unmigrated or explicitly
rolled-back projects use the frozen Legacy Runner/adapter. Rollback changes both
`control_mode` and `runtime_generation`, and `FactoryService` treats legacy
control mode as authoritative, so CLI and Web cannot select different runtimes.
The native Stage/Step registry never calls `legacy_runner.sh`.

Every native Step implements one lifecycle:

```python
class Step(Protocol):
    def prepare(self, context): ...
    def execute(self, context): ...
    def validate(self, context): ...
    def recover(self, context, error): ...
```

`FactoryEngine` owns generic dispatch, retry budgets, reopen budgets, recovery,
pending-action transitions, and archiving. Steps return structured outcomes and
cannot mutate scheduler state. `StageExecutionPipeline` runs prepare, execution,
deadline checking and validation from an immutable request and returns a
`StageOutcome`; even Step 16 returns audit events as outcome effects rather than
writing SQLite. `TransitionCoordinator` is the target orchestration/application
writer of workflow state, but writer exclusivity is not implemented at this
baseline. Current bypasses include direct `record_decision`, pending-request
supersede, prompt-attempt input binding, and projection-failure bookkeeping;
bootstrap/migration initialization and archive relocation are separate write
surfaces. The characterized inventory and future static-gate specification are
[`application_writer_allowlist_v1.json`](application_writer_allowlist_v1.json).
No caller may infer from this target that the current code has a unique writer.
The Stage catalog maps every Step 0-16 contract
exactly once and adds non-integer reviewer-entry and content-freeze subtasks;
specialized implementations own parallel proposals, the Step 6 precheck, the
Step 8.5 gate, conditional Step 13, isolated judging, and final
compile/judge/package delivery.

Before each attempt, the engine caps the Step timeout against the remaining
contest boundary. Steps 0–15 cannot cross content freeze; Step 16 can use the
terminal reserve but cannot cross the final deadline. Retry delays are budgeted
the same way and fail closed when they do not fit. The catalog exposes eight
contest-facing phases while retaining all 17 internal Step contracts.

New projects use `stage_v1`: ten persistent Stages are the scheduling,
checkpoint, retry, and recovery boundary, while every Step remains a validator,
budget, evidence, and compatibility boundary. `active_stage`,
`active_subtask`, `source_step_id`, the Step compatibility cursor, the event,
the input baseline, and any completion checkpoint commit in one revision.
Step 13 runs for MODEL/MATH/RESULT dirty state and otherwise writes a
checker/classifier-bound skip receipt. Draft and audit share a Stage but retain
separate lifecycles and fingerprint domains, so a writing Agent cannot
self-authorize audit PASS. See
[`STAGE_SIMPLIFICATION_PLAN.md`](STAGE_SIMPLIFICATION_PLAN.md).

Existing schema-v5 native projects upgrade in place as `step_v2`; they receive
a read-only Stage projection until an operator explicitly activates `stage_v1`.
The persisted `scheduler_generation` prevents the Step and Stage schedulers
from taking authority over the same project.

Only one live runner lease is allowed per project. A second start or resume is
rejected whenever the recorded PID is live, regardless of snapshot status. A
Worker keeps the project `RUNNING` between successful subtasks; `READY` means
that no Worker owns the project. Every Worker-owned transition atomically compares
the expected PID, lease, and revision in SQLite. Execution and validation both
recheck ownership, and a replaced Worker raises `RunnerLeaseLost` without
committing another event.

After an interrupted Stage subtask, recovery calls the source Step or
specialized subtask validator. Waiting and failed recovery decisions return
without writing `RUN_STARTED`. Valid artifacts produce `RECOVERY_DECIDED` and a
Stage checkpoint; a structured `completed_through_step` preserves adapter
fast-forward decisions. A durable reopen marker produces the appropriate
Step/Stage reopen event, so normal and recovered reopens consume the same
inherited budget. Invalid artifacts retry the same subtask. Recovery does not
compare file modification times.

Pending human selections, approvals and consultations share a versioned
`HumanDecisionRequest`, while Selection and Approval retain distinct validation
contracts. Structured decisions are stored in SQLite; JSON/Markdown files are
rebuildable projections. Web writes evidence by atomic rename and fingerprints it
before the normal engine/service path resolves the decision, appends the
resolution event, and clears the pending action. The compatibility selection
writer still calls `SQLiteStateStore.record_decision` directly and is a
characterized application-writer bypass, not evidence of coordinator
exclusivity. Published evidence left by a failed database commit is
reported as an orphan for retry/reconciliation. Resume is rejected until the decision
resolves the pending action through an engine transaction. The CLI, Web API,
and compatibility launchers all call `FactoryService`; Web authentication and
ACL checks remain outside that service. A normal resume uses
`resume_and_start`: the caller's expected revision is checked before evidence
is written, the gate is resolved, `RESUMED` is appended, and the worker launcher
commits `WORKER_LAUNCHED` with the exact resulting revision before releasing the
worker. Stale requests do not launch a process. The lower-level `resume` method
remains available for explicit no-start maintenance and tests.

Stage 10 first persists the content-freeze guard, performs pre-snapshot cleanup,
and freezes the canonical authored final-input manifest. Step 16 consumes the
independent final-audit result, rechecks that manifest through release, then
validates publication and submission package evidence. Snapshot mutation records
`FINALIZATION_ABORTED_SNAPSHOT_CHANGED` and reopens the owning Stage rather than
reusing the old receipt. `FactoryService` writes `delivery_manifest.json`
only after the engine reaches `completed`. Archiving then writes an
archive-request event, checkpoints and closes SQLite, moves the project from
`ongoing/` to `complete/`, and writes the archive-complete event. Re-entry
completes either half-finished archive state.

## Audit Boundary

The native validators run deterministic audit profiles at the earliest useful
boundaries: `model` after Step 4, `results` after Step 5 and again after Step 6,
and `paper` after Step 10. Records are stored under
`.factory/audits/profiles/<profile>/<snapshot>/`; failures synchronize to
`audit_issue_ledger.md`. Their snapshots include checker implementation hashes,
so unchanged inputs are reused only under the same checker contract. Stage
profiles never authorize delivery.

Step 15 is the `CONTENT_READY` boundary. The `final` profile owns release
acceptance checks, final compilation, visual gate execution, isolated judge
roles, decision routing, content fingerprints, and judgment receipts. It writes
immutable snapshot identity plus append-only attempts under
`.factory/audits/<snapshot>/`; `judge_outputs/` remains a compatibility
projection for existing tools and delivery contracts.

The audit subsystem does not publish into `papers/`, create a submission zip,
clean project artifacts, archive a project, or write workflow state. It can be
run independently:

```bash
python3 -m factory_core.cli audit ongoing/<base>
```

Step 16 invokes the same service as a compatibility adapter. A verified PASS
for the unchanged evaluator+packet+asset+PDF snapshot can be reused. FAIL and
INDETERMINATE results carry structured repair or retry metadata; the engine,
not the audit subsystem, decides whether to reopen a Step. An explicit scoped
delivery override remains `OVERRIDDEN`, never a fabricated PASS receipt.

## Application And Solver Service

`FactoryService` is the application boundary for create, inspect, start, run,
pause, resume, kill, resolve, archive, migration, solver policy, and solver job
commands. One Python worker launcher starts long-running engine work in an
isolated process group and records its lease in SQLite. Killed, completed, and
archiving projects cannot be started. `READY` projects can start directly;
paused, failed, interrupted, and satisfied human-gate states resume first.

Local and Cloud Run solvers implement one `SolverBackend` contract and are
assembled by `build_solver_backends()` for both CLI Workers and Web. Solver
policy and submission requests use the project revision. Each job has an
independent `job_revision`, stable `idempotency_key`, receipt `request_sha256`,
Stage/subtask/revision ownership and attempt identity, so duplicate requests return
the existing job and backend confirmation can persist its external ID
without conflicting with pause, resume, or policy events. `.env.cloud` is only
a compatibility projection for engine projects and cannot override the global
cloud quarantine. `CLOUD_SOLVER_URL` is required before cloud execution is
enabled; IAM credentials are loaded by the transport and are never stored in
project state. Cloud submission passes the idempotency key to the provider; a
locally `submitting` job reconciles by its provider job ID, while an unprovable
local-process identity remains fail-closed instead of being submitted again.

Native diagnostics, Action Center, Recovery Status and Audit Timeline are pure
event projectors. They display the engine-recorded recovery target but never
calculate or execute recovery. Projector snapshots are optional caches and are
discarded on version, revision or state-hash mismatch. Legacy projects retain the
runner status/heartbeat/log fallback.

Operators can inspect the same Native projections and replay-parity result from
the CLI without executing recovery:

```bash
python3 -m factory_core.cli diagnostics ongoing/<base>
```

## Explicit Migration

Never create or copy `state.db` manually. Inspect a stopped project first:

```bash
python3 -m factory_core.cli migrate inspect ongoing/<base> \
  --report /tmp/<base>-migration.json
```

Inspection fingerprints legacy state-bearing files and compares artifact
inference with checkpoint state. It refuses active locks/PIDs, conflicting
ongoing Steps, existing state databases, unknown projects, and retired
social-science projects. A historical `complete/` mismatch is retained as a
warning and imports as read-only `completed` while preserving the lower inferred
Step; it is not promoted to current-contract Step 16. Review the report, then
apply its exact digest:

```bash
python3 -m factory_core.cli migrate apply ongoing/<base> \
  --report /tmp/<base>-migration.json --digest <report-digest>
```

If any fingerprint changes between inspection and apply, the import fails and
must be inspected again. Apply then briefly owns the Legacy Runner project lock
while creating the imported snapshot and event, preventing a runner from
starting inside the migration window. Pending selection/consultation, paused,
killed, ready, and completed states are preserved.

Legacy-to-native apply defaults directly to `stage_v1`. An already-native
`step_v2` project is switched separately, only while stopped and after its
interrupted Step has been recovered:

```bash
python3 -m factory_core.cli migrate scheduler-activate ongoing/<base> \
  --expected-revision <revision>
```

The activation transaction seeds explicit Stage checkpoints from the approved
Step cursor. It does not rewrite historical events. A stopped Stage project can
return to the native Step scheduler only before a subtask attempt and while no
semantic dirty flag remains unresolved:

```bash
python3 -m factory_core.cli migrate scheduler-rollback ongoing/<base> \
  --expected-revision <revision>
```

Rollback is explicit and requires a stopped engine project. A Stage project
must also have no unresolved semantic dirty flags; rollback cannot bypass an
upstream revalidation obligation:

```bash
python3 -m factory_core.cli migrate rollback ongoing/<base>
```

Rollback appends `ENGINE_DEACTIVATED`, changes the runtime generation to
`legacy_adapter`, regenerates compatibility projections, and leaves the database
intact as audit evidence. It never deletes project artifacts, logs, markers,
backups, or credentials.

## Extension Contract

New Steps implement the lifecycle and add one catalog/registry entry. New model
providers implement `ModelBackend`; new solver transports implement
`SolverBackend`. None of these changes may add a branch to the engine scheduler,
CLI/Web routing, or the public `run_paper.sh` launcher.

The workflow database schema is version 9 (`factory_core.domain.SCHEMA_VERSION`).
Versions 1-8 upgrade in place; the current
schema includes runtime and scheduler generation, Stage cursors/checkpoints,
semantic dirty evidence, independent Solver job revision and idempotent identity,
contest policy, append-only workflow decisions, replay envelopes and projector
snapshots while retaining existing workflow event names.
Legacy upgrades retain `legacy_adapter`; new and explicitly native-migrated
projects use `native_v2` plus `stage_v1`; upgraded native projects retain
`step_v2` until explicit activation. Events remain append-only across upgrades.

`FACTORY` is the runtime data root used by the CLI and workers. It defaults to
the repository root for compatibility, but source code is resolved independently
from that data root. Repository ownership and removal criteria are documented in
[`repository-boundaries.md`](repository-boundaries.md) and
[`compatibility-removal.md`](compatibility-removal.md).

Sensitive values are loaded only by execution adapters. They must never be
placed in state, event payloads, diagnostics, migration reports, or logs.
