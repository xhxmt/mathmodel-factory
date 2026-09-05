# Phase 2 Production Authority Foundation

Status: production-capable persistence and operations foundation, installed only
through an explicit standalone operator command. It has not received production
traffic and is not imported by the active Scheduler, Service, Web/API/frontend,
`factory_core.cli`, legacy launcher, model, Solver, provider, or Phase 3-8 code.
The persisted default is `V1_ONLY`; v1 remains the only active production route.

## Frozen acceptance baseline

The prerequisite Phase 2-8 joint Shadow acceptance received a final Pro verdict
of **PASS** with no required fixes. Its archived local audit is:

- file: `PHASE2_8_SHADOW_ACCEPTANCE_PRO_AUDIT_20260826_FINAL2.zip`;
- size: `930,349` bytes;
- SHA-256: `2ca331ef3f9952326119a4aa415755675129fa4163d5f0404c873038deb7811a`;
- final-state regression: `467 passed in 21.43s`.

`shadow_contracts/phase2_8_integration.py`,
`tests/test_phase2_8_shadow_integration.py`, and
`docs/architecture/PHASE2_8_SHADOW_INTEGRATION_ACCEPTANCE.md` remain
direct-test-only. This foundation does not import, alter, package as a runtime
orchestrator, or add a production caller for that composition.

## Migration prefix and compatibility

The published Authority Schema V2 prefix is byte-for-byte unchanged:

| Migration | SHA-256 |
| --- | --- |
| `A2_0001_BOOTSTRAP` | `320c9e5fe62c641d085dfa18fae7aefbaa68c6c5124eebccb7902686fe705a04` |
| `A2_0002_WORKFLOW_REVISION` | `467767eb1006d9c6d1277ad4fe2f00fcd7eef38611904a9ac799a2349339add4` |
| `A2_0003_CONTRACT_PINS` | `27e722149118f3a550e3b818a53b79f15fdb8771d5de8ad5d4bca1979e940e0c` |
| `A2_0004_COMMAND_EVENT_RECEIPT_IDEMPOTENCY` | `3d3bfe695d1c9df3cc1420c4daa917bf2faf6de6093b2c5b671b0af01cac5aa5` |
| `A2_0005_ARTIFACT_CHECKPOINT_LEDGER` | `d1776f417a4d12b59e3f357e885b0ef32e5784039b046709e9dd4d2bca32a5f2` |
| `A2_0006_REOPEN_PLAN_OUTBOX` | `74d6721a92900cd2066174e63edc9798da9a850876d47088c901584cee86c2dd` |
| `A2_0007_EXECUTION_SCOPES` | `8cffbb5d8a8f54792cccc7de2f92ca4f7fa19479cae70b87457210207ac13d8c` |
| `A2_0008_PROJECT_SNAPSHOT_APPEND_ONLY` | `94d3dbc018bd90c67a775daadeab0c11e901f076d8d3848cb28815f0b77027bf` |
| `A2_0009_LEGACY_CHECKPOINT_BACKFILL` | `60ee72de6a201dc788511aec19ec01543bfd41671638e9850b8749e32de835f8` |

The production foundation appends a separately recorded and verified suffix.
Its objects all use `authority_production_*`, and the original verifier ignores
only that reserved prefix; unknown or altered original `authority_*` objects
still fail closed.

| Migration | SHA-256 |
| --- | --- |
| `A2_0010_PRODUCTION_MIGRATION_HISTORY` | `769bbdd0dd0baac2a10238b6ceadadce9242a51e9923dee1c933db47ffffe418` |
| `A2_0011_WRITER_FENCE_AND_SWITCH` | `ec09ec8325b1b3d3c08f26ae1e43871cc8622145068a2514f2faee2e5a16d5bf` |
| `A2_0012_TRANSACTIONAL_OUTBOX_DELIVERY` | `721a36e275de130172a06faf82d5b298d7418bf0f7561177ea580e79b7d7caf2` |
| `A2_0013_OPERATIONAL_EVIDENCE` | `be46c339e1e564aa40aa205fbb249e907281c8cf0b5e31a7833d90cd0e0f8c25` |
| `A2_0014_DATABASE_IDENTITY_AND_BACKUP_LINEAGE` | `1cdf905f5712eb04445eed9da73ac8a48cf3fb3c6115a02c4a6ccb1c54b99a75` |
| `A2_0015_PHASE9_RUN_GENERATION` | `69f50ea0989018d6dc7db8743fdf7f2875152f292933054bb21b5760fcd42b15` |
| `A2_0016_PHASE9_FORENSIC_REPLAY` | `6fa6c71a5f76388eab41a6d9e301cbce1fdce7ee041b71c9f72824eee1cb7e37` |
| `A2_0017_PHASE9_AUDIT_HARDENING` | `c886700e817098e04c325d414a0b0ed7f267a84ea60a05a0f0501e95f86fcc4d` |
| `A2_0018_PHASE9_P0_RUNNER_ATTESTATION` | `eca9538c259285853547bf451a8fbf56ca8580d3963ff9b2537f979e162d5121` |
| `A2_0019_PHASE9_REPLAY_EVIDENCE_ATTESTATION` | `355c9419f56aa6266b3676f741e0e37a63856e820fa054e9bc217027f23646db` |

This table records the implemented append-only migration contract, not live
installation evidence. A2_0016 through A2_0019 are `NOT APPLIED` in production at
this checkpoint. Formal Phase9-A/Run4 is `NOT RUN`, Phase 9 is incomplete,
Phase10-B is `NOT STARTED`, and production remains `BLOCKED`. Applying any
migration suffix requires the separate verified-backup, durable-journal and operator
authorization gates described below.

The suffix is resumable by the same explicit owner token. Every step checks:

1. a complete real `SQLiteStateStore` schema-v9 shape, not a reduced fixture;
2. the exact schema-v9 object identity;
3. the legacy-source row/object fence consumed by the backfill;
4. READY A2_0001…A2_0009 state and its canonical prefix identity;
5. exact ordered production migration IDs, checksums, and SQLite objects;
6. the persisted immutable database identity and initial pre-Authority backup
   lineage introduced by A2_0014;
7. the Phase9 run-generation lineage, receipt, idempotency, and current-pointer
   tables introduced by the additive A2_0015 suffix;
8. the Phase9-A replay/event/terminal-receipt/idempotency/current graph added
   by A2_0016;
9. the A2_0017 source-inventory, one-use authorization, typed replay-evidence,
   entry-gate-consumption, mode/contract/delivery and predecessor-terminal
   guards;
10. the A2_0018 Authority-issued, one-use P0 runner authorization,
   consumption and immutable successful execution-attestation graph;
11. the A2_0019 Authority-issued replay/runtime observation authorizations,
   trusted runner-event attestation and typed runtime/component/acceptance
   provenance; and
12. the same durable migration owner inside `BEGIN IMMEDIATE`.

Future schema versions, missing facts, owner changes, SQLite busy locks,
source-row drift, DDL drift, or interrupted prefixes fail closed. No generation,
owner, revision, writer, or delivery fact is inferred from PID, time, mtime,
file adjacency, or nearby unrelated database rows.

A2_0015 adds only a default-off Phase9 run-generation creation/rotation
boundary. A2_0017 hardens that existing graph without changing the published
A2_0010-A2_0016 statement bytes. A2_0018 likewise appends without changing
A2_0010-A2_0017 statement bytes, and A2_0019 appends without changing
A2_0010-A2_0018 statement bytes. The service binds a live Git
commit/tree/single-parent identity and the complete tracked-source byte
inventory,
source-authorized contract pins, typed official-input byte-hash evidence,
typed execution-context and operator-authorization evidence, and exact
project/workflow/revision/project/run/runtime/scheduler coordinates in one
`BEGIN IMMEDIATE` transaction. It requires `V1_ONLY` with both writer and
consumer disabled. It neither starts Phase9-A nor enables delivery, providers,
outbox dispatch, release, deployment, migration, or cutover.
The service reads every manifest-listed official-input file through a stable
directory-descriptor tree, never follows links, rejects hard links and special
files, and fails closed on any traversal error, path collision, extra path,
member or ancestor replacement. It repeats the exact byte/hash inventory before
commit. It likewise rereads the complete tracked source inventory and an
explicit canonical execution-context receipt immediately before commit.
Initial `project_generation` is content-derived rather than a caller label.
Each authorization covers the complete canonical operation target, including
the idempotency key and predecessor/target coordinates, has a recomputed
statement hash and trusted-time window, and is consumed once. Only an exact
idempotent replay may return its recorded result. Rotation additionally
requires the current predecessor's immutable Phase9 terminal receipt and a CAS
over the current revision. Creation authorization currently supports only a
controlled OS account whose UID and account name match the executing process;
the API does not claim unimplemented detached-signature verification.

A2_0016 adds the default-off Phase9-A evidence-finalization state machine;
A2_0017 makes its typed receipt set and one-use live-gate authorization durable.
A2_0018 makes formal P0 evidence depend on a consumed Authority nonce and a
DB-backed execution attestation; file-only/self-rehashed evidence cannot become
entry `READY`. A2_0019 makes formal replay evidence depend on consumed Authority
authorizations, persisted trusted runner events and immutable typed runtime/
component/acceptance provenance; caller-authored summaries are not Authority
completion facts. It
does not run a worker or provider. After a separately produced entry `READY`
result and controlled-account start authorization, the narrow service verifies
the exact packet bytes; typed role-process, role-provider and process-scope
receipts; three new role generations (or the typed no-judge ablation);
raw/protocol/grounding/effective verdict layers; one revision-atomic snapshot;
and all 17 typed acceptance-case receipts with their command, raw-log and test-
result bytes. It inventories the entire evidence root and rejects missing,
extra, duplicate, aliased or cross-coordinate evidence. The service reacquires
the shared live entry state at start and immediately before commit, binds the
entry Authority revision/state receipt, and atomically consumes the short-lived
start authorization. One `BEGIN IMMEDIATE` transaction appends the replay and
six-event hash chain, typed receipts, terminal receipt and idempotency row, then
inserts or CAS-rotates the guarded current pointer. Exact replay returns the
recorded receipt; conflicts and injected failures roll back every Phase9 row.
The query-only collector typed-decodes and semantically reconstructs the
request, generation, event sequence, terminal and current pointer instead of
accepting a merely hash-consistent SQL graph. It does not create WAL/SHM state.
Neither migration installation nor API availability is production
authorization.

## Backup and restore boundary

`factory_core.authority_operations` accepts only one explicit non-symlink
regular SQLite path and one explicit output. It never scans `ongoing/`,
`complete/`, `.factory/`, or a production root.

Before migration, `create_authority_backup()` opens the source read-only,
starts a consistent snapshot, validates schema/source identity and
`integrity_check`, and uses SQLite's backup API to create a staging database.
It then reopens and verifies the staging database, fsyncs it, atomically
publishes it, fsyncs the containing directory, and rechecks hash and size.
Evidence records the caller-supplied database identity and time, schema and
source-fence identities, the canonical content identity of all schema-v9
non-Authority tables, source main-file hash/size, canonical backup hash/size,
integrity result, base Authority state, production suffix state, last migration,
and production prefix identity. Filesystem paths do not participate in the
semantic evidence hash. During the first production migration A2_0014 persists
an immutable database binding to the exact pre-Authority backup and its lineage;
thereafter a caller-supplied `database_id` is accepted only when it matches that
binding. Health and restore accept only a backup lineage recorded in that same
database, so equal project/source-fence rows cannot make two physical databases
interchangeable.

Confirmed migrate and restore reserve the explicit evidence output as a
fsynced, canonical external operation journal before creating a backup or
replacing a database. Migrate durably advances through prepared, backup
published, base ready, each suffix migration, database ready, internal receipt,
and final evidence states. An exact replay reuses the immutable original backup
and can publish missing final evidence after the database is already READY;
once suffix migration starts it never creates a replacement “pre-Authority”
backup. Output collisions, invalid parents, symlinks, or unrelated existing
backups fail before database or backup mutation.
Before reserving a brand-new operation journal or creating its backup, migrate
also requires both the base Authority state and production suffix state to be
`ABSENT`. If either has started, the operator must replay the original owner,
backup path, and evidence path; a new operation cannot publish a replacement
rollback point from a partially migrated database.

Rollback means exact restore from that verified backup; there is no reverse SQL
migration. Restore requires all of the following in one explicit operation:

- exact current source fence, backup SHA-256, and switch epoch;
- `V1_ONLY`, disabled Authority writer, disabled outbox consumer;
- no `CLAIMED` or `RECONCILIATION_REQUIRED` delivery;
- successful exclusive SQLite quieting and no WAL/SHM companion left behind;
- a regular non-symlink backup with schema-v9, matching fence, and `integrity_check=ok`.

The backup bytes are copied to an fsynced same-directory staging file and
atomically replace the explicit target. The restored bytes must exactly equal
the backup SHA-256 and size; schema, integrity, source fence, authority state,
and business rows are re-read afterward. A pre-Authority backup therefore
restores the original schema-v9 tables and rows with no `authority_*` objects.
Restore evidence is returned or atomically written to an explicitly supplied
operator evidence path; restoring the old database cannot write a receipt into
tables that intentionally no longer exist.

The restore journal persists the verified current database identity, registered
backup lineage, switch epoch, source fence, and backup hash before replacement.
If a process exits after atomic replacement, exact replay recognizes the target
bytes as the journaled schema-v9 backup, repeats integrity/source verification,
and publishes the missing final evidence without requiring the deliberately
removed Authority tables. An evidence-output conflict is detected before
replacement and leaves the target byte-for-byte unchanged.

## Unique Authority writer and revision allocator

`AuthorityProductionWriter` is the sole production-capable command mutation
facade. Its only public mutation is `persist_command_bundle()`. It exposes no
connection, generic transaction, unit of work, allocator-only call, individual
envelope insert, or table-count API.

The earlier `AuthorityRepository(write_shadow=True)` remains available only on
databases without the production suffix. Once
`authority_production_schema_state` exists, its transaction boundary rejects
all shadow writes, so the test/review flag cannot bypass the production writer
fence. Its existing no-suffix Phase-2 tests and direct Shadow acceptance remain
compatible.

A writer is usable only when the durable singleton records all of:

- mode `CANARY` or `AUTHORITY_PRIMARY`;
- `writer_enabled=1`;
- the exact caller-supplied durable writer ID and positive writer epoch;
- the exact recorded source fence and switch epoch.

Writer configuration and handoff are allowed only under `V1_ONLY` and use CAS
on writer and switch epochs. Every material handoff or disable increments the
writer epoch and appends a canonical control receipt. A transition back to
`V1_ONLY` disables both writer and consumer; reactivation requires new explicit
fenced configuration.

Inside one `BEGIN IMMEDIATE`, a commit revalidates migration/source/writer
facts, requires allocator `next_revision == current_revision + 1`, allocates
exactly once, and inserts Command, Event, Receipt, immutable outbox intent,
idempotency record, production writer receipt, initial delivery state, and the
workflow revision. Any failed step rolls back all rows and the allocator.
Same-key/same-bytes replay returns the immutable recorded bundle; same-key with
different command or companion bytes is a conflict. Concurrent writers and
stale epochs fail closed.

The original v2 workflow and ContractPin rows retain their published
`*_SHADOW` compatibility vocabulary. Production meaning is never inferred from
those labels: each production commit has a separate immutable row binding its
bundle hash, writer ID/epoch, switch epoch, and exact `CANARY` or
`AUTHORITY_PRIMARY` mode at commit time.

This does not change the current v1 `TransitionCoordinator` or its known direct
`SQLiteStateStore` exceptions. It creates a unique boundary for the future
Authority mode; it does not falsely claim that the still-active v1 writer
inventory has already been consolidated, and it never dual-writes v1 state.

## Supported read repository

`AuthorityReadRepository` uses a `mode=ro` URI, `PRAGMA query_only=ON`, and an
explicit read transaction. Every query revalidates real schema-v9, source
fence, both migration prefixes, SQLite objects, and row/envelope identities.
It exposes only:

- frozen workflow coordinates;
- immutable command/event/receipt/outbox bundles;
- revision-bounded event snapshots;
- typed outbox delivery state.

Stored envelope UTF-8 bytes must be canonical JSON and reproduce the stored
SHA-256. Cross-links and production bundle hashes are rechecked. A revision
snapshot captures its coordinate and event boundary in the same transaction,
so a concurrent later commit cannot appear in only part of the result. Unknown,
stale, malformed, source-drifted, or schema-tampered reads fail closed. The
repository does not expose `table_count` and creates no database, WAL, or SHM.

## Transactional outbox delivery

`authority_outbox` remains immutable intent: consumers never update or delete
it. The suffix adds mutable delivery state plus append-only delivery audit and
provider receipts. Each intent has a stable delivery key derived from the
message ID and immutable envelope SHA-256.

`AuthorityOutboxConsumer` is fenced by a durable consumer ID/epoch, source
fence, and active canary/primary switch. All times, lease lengths, retry limits,
and backoff values are caller inputs. The ordinary flow is:

1. claim a due `PENDING`/`RETRY_WAIT` row by CAS, incrementing attempt and
   claim epochs and recording an append-only audit;
2. call an injected provider callback outside the SQLite transaction with the
   stable delivery key and immutable envelope bytes;
3. atomically persist a successful provider receipt and `DELIVERED`, or record
   explicit `RETRY_WAIT`/`DEAD_LETTER` state and audit.

A crash after claim or after an unrecorded provider side effect is not treated
as proof that delivery failed. Expired claims become
`RECONCILIATION_REQUIRED`; they are not claimable for blind resend. An injected
provider lookup must return one of: exact successful receipt, confirmed absent,
or unknown. Exact success closes delivery without re-dispatch; confirmed absent
returns to bounded retry/dead-letter policy; unknown stays in reconciliation.
Duplicate finalization, stale claim epochs, stale consumers, and process restart
are covered by deterministic fences.

After a hard fallback, new claims and delivery callbacks remain forbidden in
`V1_ONLY`. An operator may explicitly CAS-enable a new consumer epoch only for
expired-claim recovery and provider-receipt reconciliation; those methods
accept `V1_ONLY`, while `claim_next()` and `deliver_claim()` still reject it.
The consumer must be disabled again before restore. This closes an in-flight
provider uncertainty without reopening Authority traffic.

No callback implementation in this phase performs real network or provider
I/O. Provider authentication, adapter selection, and real dispatch remain
future integration work.

## Cutover evidence, health, and fallback

The persisted state machine is:

```text
V1_ONLY --explicit CAS--> CANARY --explicit CAS--> AUTHORITY_PRIMARY
    ^                         |                         |
    +------ explicit or automatic hard fallback ------+
```

Forward transitions are never automatic. `V1_ONLY -> CANARY` requires both a
fenced enabled writer and consumer. `CANARY -> AUTHORITY_PRIMARY` is a separate
operator receipt. Returning to `V1_ONLY` is always permitted with a fresh
switch-epoch CAS and disables Authority actors. These rows are evidence only in
Phase 2; they do not route Scheduler, Web, Service, CLI, or launcher traffic.

Read-only health evaluation takes an explicit policy and caller time. It
reports backlog depth, oldest pending age, in-flight and expired claims, retry
rate per thousand attempts, dead-letter count, source/schema/migration state,
backup freshness, and restore evidence. It does not hard-code a production
threshold. Missing/stale backup, missing/invalid required restore evidence,
source drift, incomplete migration, and threshold violations are stable hard
condition codes.

The operator `health` command accepts optional explicit `--backup-evidence`
and `--restore-evidence` JSON files. Inputs must be regular non-symlink files;
the migration wrapper hash and restore evidence hash are rechecked before the
typed evidence is bound to the current schema identity, source fence, backup
hash/size, database identity, and caller-supplied evaluation time.

The pure fallback decision can only return `NO_CHANGE` or
`FALLBACK_TO_V1`; it cannot advance Authority. Applying a hard fallback uses
switch-epoch CAS and writes an append-only receipt. A special structural
verifier permits this one downgrade after legacy source-row drift while the
normal writer/reader/outbox paths remain source-fence-disabled. If the switch
control tables themselves are structurally untrusted, the write fails closed;
before a future real cutover, deployment must also provide an external process
route/kill switch independent of the project database.

## Operator runbook

All commands require an explicit database. Mutating commands print a dry-run
record and perform no write unless `--confirm` is supplied.

```bash
python3 scripts/authority_operator.py preflight \
  --database /explicit/project/.factory/state.db \
  --database-id explicit-project-db

python3 scripts/authority_operator.py migrate \
  --database /explicit/project/.factory/state.db \
  --database-id explicit-project-db \
  --expected-source-fence <64-lowercase-hex> \
  --backup /explicit/evidence/state.pre-authority.db \
  --evidence-output /explicit/evidence/migration.json \
  --owner-token <operator-controlled-resume-id> \
  --occurred-at <explicit-unix-seconds>
```

Review the dry-run and preflight evidence, then repeat the exact mutation with
`--confirm`. Never substitute a directory, symlink, inferred “current” project,
or live database path discovered by scanning. Keep writer and delivery stopped.

For restore, first place persisted switch evidence in `V1_ONLY`, disable both
actors, resolve every in-flight/reconciliation item, verify the backup hash and
current switch epoch, run the command once without `--confirm`, then repeat:

```bash
python3 scripts/authority_operator.py restore \
  --database /explicit/project/.factory/state.db \
  --database-id explicit-project-db \
  --backup /explicit/evidence/state.pre-authority.db \
  --expected-source-fence <64-lowercase-hex> \
  --expected-backup-sha256 <64-lowercase-hex> \
  --expected-switch-epoch <integer> \
  --occurred-at <explicit-unix-seconds> \
  --evidence-output /explicit/evidence/restore.json \
  --confirm
```

On any failure, preserve the original backup and emitted journal/evidence. Do
not delete history or attempt reverse SQL. Replay the exact command, including
owner, database identity, paths, hashes, fence, epoch, and explicit time. An
interrupted migration resumes only with its recorded owner token and original
backup after restoring the exact source facts; a different owner, changed
source, changed operation request, or changed prefix requires operator review.

## Failure routing

| Failure | Required routing |
| --- | --- |
| Source/schema/object drift | Stop Authority writer, reader, delivery, and forward switch; alert and evaluate one-way fallback |
| SQLite busy or writer epoch conflict | No retry by heuristic; reacquire explicit operator/consumer fence |
| Migration interruption | Preserve committed prefix, external journal, and original backup; replay the exact operation with the same owner |
| Bundle failure/allocator drift | Whole transaction rolls back; investigate before a new explicit command |
| Claim expiry/unknown provider outcome | Reconcile by stable delivery key; never blind resend |
| Retry exhaustion/permanent provider failure | Durable dead-letter and alert |
| Backup stale/missing or restore evidence invalid | Block forward switch and refresh evidence explicitly |
| Restore precondition failure | Leave target untouched; stop actors and re-run preflight |
| Restore interruption after replacement | Preserve journal and backup; replay the exact operation to reverify bytes and publish final evidence |

## Non-goals and Phase 3 dependency

This phase does not connect the Authority writer to the active application,
perform a live migration, dispatch a provider request, deploy, cut over, or
dual-write. It does not implement Phase 3 artifact registry persistence,
Phase 4 leases/launcher, Phase 5 Execution Supervisor, Phase 6 API/UI, Phase 7
release gates, or Phase 8 materialization/approval/egress dispatch.

Phase 3 may build on this foundation only after defining revision-bound
artifact records and change/reopen commands that can be submitted as complete
Authority bundles. Phase 3 must not bypass the writer facade, mutate immutable
outbox intent, infer owner/generation, or import the joint Shadow harness. A
future traffic switch additionally requires consolidation of the active v1
writer inventory, real deployment fencing, authenticated operator authority,
provider adapters, and an external rollback control; none is claimed here.

## Phase9 runtime execution preparation (A2_0020)

`A2_0020_PHASE9_RUNTIME_EXECUTION` adds append-only dispatch grants, execution plans, committed attempts, OS launch observations, completion observations, terminals and receipt bindings. Its checksum is `45ddef2fb5e3b3a5e98fa6f779d8b9bb83d1906263ff1d41f819a5e2a6f3d391`. Production schema version is 8. Published A2_0010–A2_0019 statement bytes remain unchanged. This migration has not been applied in production. The coordinator integration is under validation; this text is not formal acceptance evidence.
