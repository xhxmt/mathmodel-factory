# Authority Schema V2 Phase 2

Status: additive, local persistence slice; installed only by an explicit
migration call and not connected to the active Scheduler, Web API/UI, provider,
model, Solver, or production deployment path.

## Version and feature boundary

- `AUTHORITY_SCHEMA_VERSION = 2` is independent of the legacy workflow
  `schema_info` value. The installer accepts legacy schema versions 1 through
  9 and never changes that legacy value.
- `AUTHORITY_SCHEMA_V2_WRITE_SHADOW = false` is the required default. The
  repository refuses every write unless its constructor receives the explicit
  test/review-only `write_shadow=True` argument.
- Existing tables, rows, events, checkpoint projections, writers, and runtime
  imports are not deleted, updated, renamed, or redirected. Older code can
  ignore every `authority_*` table.
- Missing legacy facts are represented as `legacy_unknown`, or as SQL `NULL`
  paired with an explicit `legacy_unknown` availability field when the fact is
  typed as an integer. No generation, revision, owner, receipt, or pin is
  inferred from time, PID, path, current source, or a neighboring field.

## Migration state machine

The durable state values are `RUNNING`, `INTERRUPTED`, `READY`, and
`MIGRATION_BLOCKED_OWNER_AMBIGUOUS`. A caller supplies a non-empty owner token.
The token is recorded before migration steps are committed; a different owner
fails closed, while the same owner can resume an interrupted run. One owner
token is an operator-controlled crash-resume identity, not a lease: exactly one
runner may actively use it at a time. Concurrent use of the same token is not a
supported operation and one runner may receive a lock-ownership failure; the
database transaction and exact history-prefix checks prevent partial history.

Before bootstrap, the runner records
`authority-legacy-source-identity-v1`: a canonical fingerprint of the
`schema_info`, `project_state`, and `stage_checkpoints` sqlite_master objects
and rows that the backfill actually consumes. Every step and the final state
revalidate both this fingerprint and the legacy schema version in one
`BEGIN IMMEDIATE` transaction. Any drift interrupts progress. Restoring the
exact source lets the same owner resume; combining facts from different source
coordinates is forbidden. Repository READY checks repeat this source fence.

The migration identifiers, in order, are:

1. `A2_0001_BOOTSTRAP`
2. `A2_0002_WORKFLOW_REVISION`
3. `A2_0003_CONTRACT_PINS`
4. `A2_0004_COMMAND_EVENT_RECEIPT_IDEMPOTENCY`
5. `A2_0005_ARTIFACT_CHECKPOINT_LEDGER`
6. `A2_0006_REOPEN_PLAN_OUTBOX`
7. `A2_0007_EXECUTION_SCOPES`
8. `A2_0008_PROJECT_SNAPSHOT_APPEND_ONLY`
9. `A2_0009_LEGACY_CHECKPOINT_BACKFILL`

A legacy schema greater than 9 is rejected before any authority table is
created. An authority schema greater than 2 is rejected rather than
downgraded. An applied record must be the exact ordered prefix, with its
one-based `applied_order`, recorded source version, statement bytes, bootstrap
DDL, and named hook implementation identity included in its checksum. The
actual authority table/index/trigger definitions in `sqlite_master` must equal
the definitions for that prefix, so an earlier same-name `IF NOT EXISTS`
object fails closed.

Migration history and all immutable authority records have UPDATE and DELETE
rejection triggers. They also have `BEFORE INSERT` guards for every primary or
other UNIQUE identity. Those guards reject `INSERT OR REPLACE` conflicts
without depending on the connection-local `recursive_triggers` PRAGMA.

## Additive tables

The schema adds separate tables for:

- workflow identity and monotonic revision allocation;
- recorded ContractPinSet, CommandEnvelope, EventEnvelope, ReceiptEnvelope,
  and scoped idempotency records;
- artifact records and a typed checkpoint ledger;
- immutable reopen plans and transaction-bound outbox messages;
- invocation, attempt, and process scopes;
- recorded Project Snapshot values;
- migration state, lock, and applied migration identities.

The outbox row is inserted in the same SQLite transaction as the command,
allocated revision, event, receipt, idempotency record, and workflow revision.
There is deliberately no outbox consumer or delivery-state update in this
phase.

## Legacy checkpoint import

Every imported current checkpoint uses checkpoint kind
`LEGACY_CURRENT_IMPORTED`. Its assurance is exactly `LEGACY_IMPORTED` when a
legacy receipt is structurally present, otherwise `UNKNOWN`; migration never
upgrades either value to verified assurance. An explicit legacy `stage_id` is
preserved as owner evidence only for a concrete `stage_checkpoints` row.
`project_state.active_stage` and `last_completed_stage` are context, not proof
of a unique checkpoint owner; the fallback retains both in payload but leaves
`owner_stage` null. When no unique explicit owner exists, the ledger row and
overall migration state use
`MIGRATION_BLOCKED_OWNER_AMBIGUOUS`, and authority writes remain unavailable.

## Persistence boundary

`AuthorityRepository.persist_command_bundle()` validates immutable envelope
bytes and cross-links, checks the current recorded workflow revision, claims a
scoped idempotency key, allocates one revision, and persists the command,
event, receipt, outbox message, pin set, and idempotency receipt atomically.
The formal idempotency request schema is
`authority-command-envelope-request-v1`, whose payload is the stable
CommandEnvelope bytes. Same-key/different-command replay is a conflict.
Same-key/same-command replay returns the recorded result only when the supplied
Event, Receipt, and Outbox stable identities exactly match that immutable
recorded bundle; different companion envelopes fail instead of being ignored.

Transaction rollback restores the allocator and leaves no partial outbox or
envelope rows. The repository also fails closed if the allocated revision is
not exactly `current_revision + 1`. `persist_project_snapshot()` is the only
other mutation and accepts only a validated snapshot bound to the current
workflow revision, generations, and recorded pin coordinate. There is no
public generic transaction, unit-of-work, allocator, or individual-envelope
mutation API, so callers cannot commit an allocator-only or incomplete bundle.
Both supported mutation methods revalidate explicit `write_shadow=True`, the
READY state, the source fingerprint, ordered migration history, and actual
SQLite schema identity inside their write transaction.

This slice does not authorize command execution or CAS acceptance. It does not
switch the current writer, consume the outbox, migrate a real production DB,
call a provider/model/Solver, expose an API/UI, deploy, or change delivery
authorization.
