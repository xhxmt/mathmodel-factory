# Phase 3 Full Artifact Shadow Foundation

Status: packaged and testable, explicitly default-disabled, non-authoritative,
and disconnected from active Scheduler, Service, CLI, Web, process, provider,
model, and Solver routes. V1 remains the only production authority and the only
active route. This document does not authorize cutover.

## Canonical implementation

- `factory_core/phase3_artifacts.py` owns the immutable typed domain and pure
  classifiers.
- `factory_core/phase3_shadow_runtime.py` owns the explicit full-shadow runner.
- `AuthorityProductionWriter.persist_command_bundle` remains the sole public
  production-capable write entry. Its optional typed `phase3_mutation` is the
  only Phase 3 persistence path.
- `AuthorityReadRepository.command_bundle()` reconstructs one complete,
  revision-atomic `Phase3Mutation`; `phase3_artifact_state()` reconstructs the
  latest present record, blocker, or tombstone occurrence for each path.
- The supported query-only repository surface is exactly
  `workflow_coordinate()`, `command_bundle()`, `phase3_artifact_state()`,
  `revision_snapshot()`, and `outbox_delivery_state()`.
- `shadow_contracts/artifact_registry.py` and
  `shadow_contracts/phase3_foundation.py` are historical compatibility facades,
  contain no second implementation, and remain outside package discovery.
- `scripts/__init__.py` makes the already configured `scripts*` package
  importable from a wheel; it adds no runtime route or behavior.

## Complete graph and tracked inventory

`ArtifactManifest` v2 hash-binds a sorted, normalized `tracked_paths` inventory
as well as records and typed blockers. Except for a root-safety blocker, every
tracked path is represented by exactly one current record or blocker. Duplicate,
untracked, partially covered, or altered inventories fail validation.

`ChangeSet` v2 is recomputed from the exact previous/current manifests and an
explicit tuple of immutable `ArtifactRemoval` decisions. Each path represented
on the previous side must close in exactly one way:

- a current record;
- a current typed blocker; or
- an explicit removal/untracking decision bound to the previous semantic value,
  frozen owner policy, owner/stage/dirty facts, and reason.

Omitting a previous record yields `TRACKED_PATH_OMITTED`; omitting a previous
unreadable blocker yields `PREVIOUS_BLOCKER_OMITTED`. Both block the round.
Blocker-to-record is a typed `RESOLVED` change. Blocker-to-explicit-removal is
also closed and auditable. A missed scan can therefore never be interpreted as
clean or as an implicit delete.

`Phase3Mutation` v2 directly contains and hash-binds the previous manifest,
current manifest, ChangeSet, current record/blocker/removal collections,
checkpoint entries, and ReopenPlan. Validation recomputes the graph and requires:

- exact previous/current manifest identities on the ChangeSet;
- exact current record/blocker/removal collections;
- every checkpoint input manifest to equal the current manifest;
- exact ReopenPlan ChangeSet and previous-side read-set coverage; and
- checkpoint scope, owner, target Stage, and dirty-path agreement with the plan.

Cross-round splicing, a checkpoint for another manifest, a partial bundle, or a
self-consistent but graph-inconsistent payload fails before persistence and
again during repository reconstruction.

## Domain and filesystem safety

All public values are frozen dataclasses with typed enums, canonical JSON, and
recomputed SHA-256 identities. Builders sort unordered inputs; reconstruction
rejects duplicate identities, altered hashes, invalid state shapes, and
non-canonical collections. Checkpoint keys use the explicit `phase3:` namespace,
for example `phase3:stage4.results`.

Artifact paths have one project-relative lexical spelling. Absolute,
drive-qualified, empty-segment, dot-segment, traversal, control-character, and
root/symlink escape cases fail closed. Illegal non-string paths map to the fixed
deterministic sentinel `__phase3_invalid_path__/non_string`; unsafe string paths
use a deterministic safe sentinel and never become filesystem paths.

Project roots containing symlink components fail closed. Artifact components are
opened relative to verified directory descriptors with `O_NOFOLLOW`; only stable
regular files are hashed. Missing, symlinked, non-regular, unreadable, and
changed-during-read results become typed blockers. No path presence, mtime,
clock, PID, or neighboring record is treated as authority.

Owner registrations freeze the source-compiled owner policy, matching rule
identities, authorized winner, owner ID/Stage, semantic domain, dirty flag, and
final/submission memberships. Owner compilation drift or a frozen registration
change is `MIGRATION_REQUIRED`, never an implicit rewrite or dirty decision.

An owner-resolution failure uses a separate v2 operator-authorization path. Its
typed claim binds workflow, source revision, target command, normalized path,
the complete reconstructable owner compilation and policy SHA, authorized owner
ID/Stage, dirty flag, operator subject, and reason. The trust root is an
immutable `authority_production_control_receipts` row created only while a real
`V1_ONLY` writer-configuration CAS advances the writer epoch. The receipt
contains the exact claim set and its canonical identity; the runtime writer can
reference this grant but cannot issue one through `persist_command_bundle()`.

In the command transaction, the writer checks every claim coordinate against
the current workflow, source revision, command, path, policy, writer ID/epoch,
issuer receipt, and granted owner scalars. It reconstructs the frozen owner
compiler and independently runs normal registration first. If the path resolves
normally, operator authorization is rejected rather than overriding the real
owner. Missing, stale, cross-workflow, cross-command, altered, deleted, or
ambiguous receipt/grant evidence fails before any command or Phase 3 ledger row
is written. Exact replay and read-side reconstruction repeat the receipt and
owner-resolution verification.

## Semantic identity and occurrence identity

Artifact Record and Checkpoint IDs remain semantic identities for content
equality. They are not database occurrence primary keys.

`ArtifactLedgerOccurrence` and `CheckpointLedgerOccurrence` bind the workflow,
committed revision, command ID, complete mutation identity, and semantic value.
Artifact occurrences additionally bind the normalized path and one exact kind:
record, blocker, or removal tombstone. Checkpoint occurrences bind their
predecessor occurrence where the state transition requires one.

The existing ledger row IDs and checkpoint source keys store these occurrence
identities. CAS and replay use occurrence IDs; semantic comparison uses semantic
IDs. Consequently A-to-B-to-A, identical records in two workflows, identical
initial checkpoints in two workflows, and a later revision returning to an
identical semantic checkpoint all persist without collision and remain
revision-auditable.

## Removal tombstones

An `ArtifactRemoval` is immutable and bound to the previous semantic record or
blocker, workflow owner facts, owner-policy identity, normalized path, dirty
classification, and explicit reason. Its persisted artifact occurrence is a
revision-bound tombstone additionally bound to the mutation and command.

Pure removal and mixed removal/modification bundles use the same writer and
transaction as every other Phase 3 mutation. A tombstone is absence for future
state comparison only when the read set cites that exact tombstone occurrence.
Later recreation writes a new record occurrence. Exact replay checks tombstone
bytes, and `phase3_artifact_state()` returns tombstones rather than silently
dropping deletion history.

## Checkpoints, re-attestation, reopen, and parity

Checkpoint entries model initial valid/invalid records, invalidation, and
valid/invalid re-attestation with strict predecessor/state edges. Re-attestation
is a pure dry-run classified as `REUSED`, `REGENERATED`, or `STILL_INVALID`; it
does not invoke a validator or write a checkpoint.

`ReopenPlan` binds the source revision, earliest dirty owner Stage, exact
ChangeSet, and a sorted previous-side read-set CAS. Read expectations distinguish
record, blocker, tombstone-backed absence, and never-observed absence. Every
changed path has one expectation. Parity receipts remain non-authoritative
evidence and classify `MATCH`, `EXPECTED_DIFFERENCE`, `DIVERGENCE`, or `BLOCKED`.

## Persistence and read boundary

Phase 3 reuses only `authority_artifact_records`,
`authority_checkpoint_ledger`, and `authority_reopen_plans`. It adds no
migration and changes no ID, checksum, or statement byte in `A2_0001` through
`A2_0014`.

With `phase3_mutation=None`, the writer retains the frozen v1 idempotency
request, bundle hash, persisted bytes, result shape, replay, and conflict
behavior. An explicit complete mutation uses v2 request/bundle schemas and
binds the mutation hash into both identities.

Within the existing `BEGIN IMMEDIATE`, the writer validates the whole graph,
source/writer/switch fences, workflow revision, exact presence/absence read-set,
frozen owner policy, and checkpoint occurrence predecessor. It writes all
command companions, occurrence rows/tombstones, the ReopenPlan, delivery state,
production commit evidence, idempotency row, and new revision atomically. There
is no alternate connection, allocator, transaction API, or second writer.

The reader opens SQLite with URI `mode=ro`, sets `PRAGMA query_only=ON`, and uses
one explicit read transaction. Companion and ledger queries have explicit
cardinality checks. It validates wrapper bytes, column/payload agreement,
occurrence/semantic identity, command/mutation binding, the complete typed graph,
request identity, and bundle hash. Missing, extra, mixed, malformed, or
cross-revision rows fail closed.

## Runner and non-goals

`run_phase3_full_shadow()` defaults to disabled and returns before touching the
project root or artifact paths. Explicit enablement permits only local safe reads
and pure classification. Every result records `authoritative=False` and
`dispatch_performed=False`; the API has no persistence handle or dispatch
callback.

There is no Scheduler/Service/CLI/Web feature flag, background task, dual write,
process launch, provider/model/Solver call, production database operation,
deployment, traffic change, or cutover. Owner-policy migration execution,
authoritative checkpoint validation, parity-driven routing, and a production
activation decision remain separate future reviews.
