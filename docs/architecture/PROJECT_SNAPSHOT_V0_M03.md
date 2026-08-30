# Project Snapshot V0 M0.3

Status: read-only shadow/prototype. No API, UI, table, migration, production
feature flag or scheduler path consumes it.

## Transaction contract

`build_project_snapshot_v0()` accepts an explicit SQLite database path. It
uses `lstat` to reject a final symlink/non-regular file, then opens exactly one
SQLite URI connection with `mode=ro`, `uri=True`, `isolation_level=None`,
`sqlite3.Row`, `PRAGMA query_only=ON`, and an explicit `BEGIN`. It does not use
`SQLiteStateStore`, schema upgrades, public getters, `immutable=1`, `nolock=1`
or `PRAGMA journal_mode`.

The transaction first reads `schema_info` and `project_state`, reads every
section on that connection, re-reads the coordinate, then rolls back and
closes. An authorizer rejects SQL mutation/DDL/attach operations and the result
retains an analysis-only SQL trace. Required schema/project/event failures,
foreign projects, invalid required JSON/hash, duplicate identity and future
row revisions return a typed top-level `ERROR`. Optional projector corruption
is a typed section `ERROR` and makes the snapshot partial.

Before any SQLite row becomes an `AVAILABLE` fact, `SnapshotPolicyV0` is
rebuilt from the frozen `WorkflowStatus`, Stage/Step/Gate catalogs,
`DirtyFlag`, checkpoint/dirty-receipt schemas, projector versions and separate
ordered Solver lifecycle and receipt event mappings. The lifecycle mapping covers
`SUBMITTED/submitted`, `SUBMITTING/submitting`, `QUEUED/queued`,
`RUNNING/running`, `CANCELLING/cancelling`, and all five supported terminal
states; the `submitting`/`submitted` generation-one compatibility rule remains
explicit. Receipt authorization is exactly
`SOLVER_JOB_RECEIPT_SUBMITTED/submitted` and
`SOLVER_JOB_RECEIPT_COMPLETED/completed`; receipt events do not increment the
job generation. Their event type, stage, job, path, three hashes, immutable
event reference, order and request identity are cross-bound, and one immutable
receipt fact is projected into both the Solver and immutable-reference
sections. The caller cannot supply or self-hash this policy. It
binds project cursors to the source catalogs; Stage checkpoints to their
source/completed Steps, completed revision, content-derived checkpoint ID and
receipt; dirty causes to their content-derived IDs and clear/rebase receipts
to the exact historical cause; a pending action to the unique open request,
source-derived request ID, kind, gate, generation, subject and options; projector
caches/failures to an approved name/version and the recorded event chain;
Solver rows to the contiguous recorded lifecycle generation and exact latest
event/status mapping. Submission events bind backend, runtime, timeout,
idempotency, owner and attempt; the most recent non-empty lifecycle event binds
the external ID; terminal events bind failure. Script, workdir, argv and
timestamps are explicitly legacy row-authoritative and receive strict
type/UTF-8/path/structure validation because the current submission event does
not duplicate them. Non-empty `result_refs_json` is a typed top-level
legacy-unbound Solver error because the current lifecycle event schema does not
record result refs; it is never silently promoted to a complete fact. Immutable
refs otherwise require DB-recorded hash-pinned mappings. Required-domain
drift is a typed top-level error. Projector or immutable-ref drift is a typed
section error and therefore a partial Snapshot, never an available fact.

Attempt-9 explicitly versions this trust boundary as `snapshot-policy-v3`,
`snapshot-event-head-contract-v3`, `snapshot-immutable-ref-contract-v3`,
`snapshot-solver-receipt-contract-v2`, and
`project-snapshot-v0-source-authorized-v3`. An event may enter the
`EVENT_HEAD_CHAIN` fact only after its exact `WorkflowEvent` row and v2
envelope are validated: event identity and canonical type are recomputed;
patch mode, patch vocabulary, before/after hashes, scheduler/Stage/Step
coordinates and strict JSON/UTF-8 structure are checked; the entire stream is
replayed with hash verification; and the final replay state must equal the
same-transaction project row. Missing or older envelopes are a typed legacy
boundary, never a complete event head.

`SnapshotPolicyV0` also contains the ordered
`snapshot-event-row-policy-v1` projection. Each accepted raw event type is in
an explicit closed set and binds an exact canonical type, payload family,
required payload fields, row Step mode and row attempt mode. Step modes cover
no Step, a fixed source-catalog Step, payload source Step, payload
Stage/subtask source Step, causal-subject source Step and result source Step;
attempts bind to zero, causal subject or result. The row Step must first be a
real Step catalog member. Stage success/reopen/invalidation, Solver
lifecycle/receipt, human decision, configuration, project initialization,
ordinary transition and finalization families then apply their narrower
binding. Unknown raw types, unknown dynamic `SOLVER_JOB_*` families, a known
canonical alias under an unapproved raw type, and coherent row/payload
Step/attempt drift return top-level `EVENT_CHAIN_INVALID` rather than entering
a complete event head. Canonical replay self-consistency is never treated as
source authorization.

Every current event also carries the source-authorized twelve-domain effect
map. Each effect value is canonical SHA-256, the aggregate is recomputed, and
the top-level fields must exactly equal the `_workflow` fields. The current
head additionally equals hashes recomputed from the same read transaction's
business rows. Historical events prove their recorded map's internal
integrity; current rows are not misrepresented as historical table bytes.

Solver job IDs use a closed ASCII letter/digit/underscore/hyphen/colon
pattern; they are not paths. A receipt path must be the literal
`.factory/solver_receipts/{job_id}.{stage}.json`. A submitted receipt must
follow submission, while a completed receipt must follow the current terminal
lifecycle event and cannot exist for an active row. Every hash-pinned `path`
uses safe project-relative POSIX syntax: absolute paths, backslashes, NUL,
empty, `.` and `..` segments, every C0 control character and DEL fail closed.

The builder never reads recorded artifact/log reference targets, process IDs,
the live project filesystem, time, environment, random/UUID, network,
provider/model/Solver, or current source identities. All sections carry one
`SnapshotCoordinateV0` and outputs fix `authoritative=False`,
`performed_workflow_side_effects=()`, and
`application_initiated_write_operations=()`.

## Availability is data

Every section uses one registered status:

- `AVAILABLE`
- `ERROR`
- `UNAVAILABLE_LEGACY_UNBOUND`
- `REDACTED`
- `PAGED`

Only `AVAILABLE` represents a real collection, including a true empty
collection. Other states carry a stable error, gap, policy or page cursor and
cannot masquerade as empty facts. A missing database for a filesystem-only
legacy project returns top-level `UNAVAILABLE_LEGACY_UNBOUND`; it does not
invent a snapshot.

Current schema v9 has no durable project generation, run generation, M0.3
ContractPinSet, or delivery-authorization generation binding. Those values are
never synthesized from revision, attempt, lease/PID, time, path, or current
source. Therefore a current legacy DB produces a `PARTIAL` snapshot, and that
snapshot is categorically ineligible for CAS acceptance.

A structurally complete synthetic Snapshot carries the full recoverable
`ContractPinSetV1`, not only its hash. Its coordinate hash must equal the
canonical pin values; the Snapshot-to-CAS bridge then validates every pin
against the trusted Workflow V2 bundle. Snapshot facts carry explicit,
all-or-none entity and subject identities so scope cannot be inferred from an
unrelated row. This synthetic route is test-only and does not add persistence
or make the current schema-v9 builder complete.

## WAL sidecar addendum

`APPROVE_M03_WAL_ADDENDUM` selected scheme A for this boundary only. SQLite's
C/VFS may create, maintain, truncate or remove the exact auxiliary paths
`<db>-wal` and `<db>-shm` while the application connection remains `mode=ro`
and its SQL transaction remains read-only. That runtime auxiliary I/O is not a
workflow mutation. Application code still may not touch, create, open-write,
truncate, rename or remove a sidecar, and may not use `immutable=1`,
`nolock=1`, a copied DB, a read/write connection, a checkpoint, or a
journal/locking-mode change.

Quiescent evidence requires invariant main-DB bytes, schema/revision and
business-row digest, plus no project-tree change outside the two-path
allowlist. An outer evidence harness records sidecar before/after existence,
size, SHA-256 and `UNCHANGED|CREATED|MODIFIED|TRUNCATED|DELETED` change kind,
SQLite version and available VFS information. Sidecar values are
analysis/evidence-only and are never Snapshot semantic facts, read-set entries
or CAS pins; platform-dependent sizes are not golden values.

If SQLite cannot establish required WAL auxiliary state, the builder returns
top-level `ERROR/SQLITE_WAL_AUXILIARY_UNAVAILABLE`, never a legacy gap or empty
section. Active-writer tests assert complete N or complete N+1 snapshots and
do not impose file-byte invariance on a legitimate writer.

The original conflict probe remains immutable historical evidence and is
classified `historical_design_conflict`, resolved by the WAL addendum. The
addendum is not approval of the overall M0.3 implementation or Phase 2/4/6.

## Competition and authority boundary

The current v1 runtime remains the only authoritative competition workflow.
Even if reviewed, this M0.3 module stays read-only, non-authoritative and
default-off. It is not imported by the competition scheduler, project DB
writer, Web API or UI, and it performs no cutover. No Phase 2–10 work,
migration, outbox, durable receipt or production integration is included.
