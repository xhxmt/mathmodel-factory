# Orchestration Engine

This document owns the current runtime-state, migration, and recovery contract.
`STEPS.md` continues to own Step artifacts and quality gates.

## Authority

New and explicitly migrated modeling projects store authoritative workflow
state in `.factory/state.db`. Each transition uses one SQLite transaction to:

1. compare the caller's expected `revision`;
2. append an immutable event;
3. update the project snapshot and increment `revision`.

The snapshot records project identity, control mode, status, completed and
active Step, attempt, pending action, runner lease/PID, storage scope, and
timestamps. Database triggers reject event updates and deletes. Event payloads
redact secret-, token-, password-, credential-, and API-key-shaped fields.

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
The native Step 0-16 registry never calls `legacy_runner.sh`.

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
cannot mutate scheduler state. The catalog registers Step 0-16 metadata;
specialized implementations own parallel proposals, the Step 6 precheck, the
Step 8.5 gate, isolated judging, and final compile/judge/package delivery.

Only one live runner lease is allowed per project. A second start or resume is
rejected whenever the recorded PID is live, regardless of snapshot status. A
Worker keeps the project `RUNNING` between successful Steps; `READY` means that
no Worker owns the project. Every Worker-owned transition atomically compares
the expected PID, lease, and revision in SQLite. Execution and validation both
recheck ownership, and a replaced Worker raises `RunnerLeaseLost` without
committing another event.

After an interrupted Step, recovery calls that Step's validator. Waiting and
failed recovery decisions return without writing `RUN_STARTED`. Valid artifacts
produce `RECOVERY_DECIDED`; a structured `completed_through_step` preserves
adapter fast-forward decisions. A durable reopen marker produces
`STEP_REOPENED` with `source=recovery`, so normal and recovered reopens consume
the same budget. Invalid artifacts retry the same Step. Recovery does not
compare file modification times.

Pending Step 3 selections and consultations are stored in `pending_action`.
Their JSON/Markdown files are evidence; resume is rejected until the evidence
resolves the pending action through an engine transaction. The CLI, Web API,
and compatibility launchers all call `FactoryService`; Web authentication and
ACL checks remain outside that service. A normal resume uses
`resume_and_start`: the caller's expected revision is checked before evidence
is written, the gate is resolved, `RESUMED` is appended, and the worker launcher
commits `WORKER_LAUNCHED` with the exact resulting revision before releasing the
worker. Stale requests do not launch a process. The lower-level `resume` method
remains available for explicit no-start maintenance and tests.

Step 16 validates compile, visual gate, final judge receipt, publication, and
submission package evidence. `FactoryService` writes `delivery_manifest.json`
only after the engine reaches `completed`. Archiving then writes an
archive-request event, checkpoints and closes SQLite, moves the project from
`ongoing/` to `complete/`, and writes the archive-complete event. Re-entry
completes either half-finished archive state.

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
independent `job_revision`, so backend confirmation can persist its external ID
without conflicting with pause, resume, or policy events. `.env.cloud` is only
a compatibility projection for engine projects and cannot override the global
cloud quarantine. `CLOUD_SOLVER_URL` is required before cloud execution is
enabled; IAM credentials are loaded by the transport and are never stored in
project state.

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

Rollback is explicit and requires a stopped engine project:

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

The database schema is version 4. Versions 1-3 upgrade in place; version 4 adds
the independent Solver job revision while retaining existing workflow events.
Legacy upgrades retain `legacy_adapter`; new and explicitly native-migrated
projects use `native_v2`. Events remain append-only across upgrades.

`FACTORY` is the runtime data root used by the CLI and workers. It defaults to
the repository root for compatibility, but source code is resolved independently
from that data root. Repository ownership and removal criteria are documented in
[`repository-boundaries.md`](repository-boundaries.md) and
[`compatibility-removal.md`](compatibility-removal.md).

Sensitive values are loaded only by execution adapters. They must never be
placed in state, event payloads, diagnostics, migration reports, or logs.
