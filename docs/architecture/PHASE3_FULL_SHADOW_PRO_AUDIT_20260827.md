# Phase 3 Full Shadow Pro Re-review — 2026-08-27

Status: post-`CHANGES_REQUIRED` implementation and local evidence for Pro
re-review. The four reported Major findings are closed in the code and focused
regression suites. This is not production activation, routing, deployment, or
cutover approval.

## Required declaration

Phase 3 is **default disabled**, **non-authoritative**, and **not cut over**.
V1 remains the sole production authority and sole active production route.
Nothing here connects Phase 3 to Scheduler, Service, CLI, Web, process,
provider, model, or Solver dispatch. No production database was accessed; no
deployment, commit, or push was performed.

## Major finding closure

### 1. Cross-round component splicing

Closed. `Phase3Mutation` v2 directly binds the previous/current
`ArtifactManifest`, recomputed `ChangeSet`, exact current record/blocker/removal
sets, checkpoint entries, and `ReopenPlan`. Domain validation, the sole writer,
and read-only reconstruction all enforce the same graph:

- ChangeSet manifest hashes equal the embedded manifests;
- current values/removals exactly cover the current side;
- every checkpoint consumes the current manifest;
- the ReopenPlan consumes the same ChangeSet and exact previous-side read set;
- checkpoint scope/owner/target matches the plan; and
- all persisted occurrence/wrapper rows bind the same command, mutation, and
  committed revision.

Cross-round splicing, a wrong checkpoint manifest, partial graph data, extra or
missing rows, and tampered wrapper bytes fail closed.

### 2. Previous unreadable blocker omission

Closed. `ArtifactManifest` v2 hash-binds its normalized tracked-path inventory.
Every prior record or blocker must become a current record, current blocker, or
explicit typed removal. Omitted records and blockers produce blocking
`TRACKED_PATH_OMITTED` and `PREVIOUS_BLOCKER_OMITTED` decisions respectively;
the runner returns blocked and does not create a reopen plan. Blocker-to-record
is typed `RESOLVED`; blocker-to-removal is explicit and auditable. Non-string
invalid paths use the fixed sentinel
`__phase3_invalid_path__/non_string`.

### 3. Semantic identity versus occurrence identity

Closed. Artifact and checkpoint semantic hashes still mean equal content.
Persisted `ArtifactLedgerOccurrence` and `CheckpointLedgerOccurrence` IDs bind
workflow, committed revision, command, mutation, and semantic value; checkpoint
predecessors and ReopenPlan CAS use occurrence identities. This removes global
PK collisions while retaining semantic comparison. Tests cover Artifact
A-to-B-to-A, equal records in two workflows, equal initial checkpoints in two
workflows, and a later revision returning to an equal semantic checkpoint.

### 4. `REMOVED` persistence closure

Closed. Immutable `ArtifactRemoval` decisions bind workflow owner facts,
normalized path, prior semantic record or blocker, owner-policy identity, dirty
classification, and reason. The writer turns each into a revision/command/
mutation-bound artifact tombstone occurrence inside the same `BEGIN IMMEDIATE`.
Pure deletion, mixed modification/deletion, exact replay, failure rollback,
future absence/presence CAS, later recreation, command reconstruction, and
latest-state queries all retain and validate the tombstone.

## Preserved boundaries

- `AuthorityProductionWriter.persist_command_bundle` remains the only public
  production-capable mutation entry. No connection, allocator, transaction, or
  second writer was added.
- `phase3_mutation=None` retains frozen v1 request and bundle formulas, bytes,
  replay, result shape, and conflict behavior.
- Phase 3 v2 identity includes the complete mutation hash.
- `authority_artifact_records`, `authority_checkpoint_ledger`, and
  `authority_reopen_plans` are reused. `A2_0001` through `A2_0014` IDs,
  checksums, and statement bytes are unchanged.
- The reader stays URI `mode=ro`, `PRAGMA query_only=ON`, and transactionally
  consistent, with explicit companion cardinality checks.
- Checkpoint keys have the `phase3:` namespace. The explicit shadow runner
  returns before filesystem access when disabled and always records
  `authoritative=False`, `dispatch_performed=False`.

## Verification results

All commands ran in
`/home/tfisher/.codex/worktrees/0f77/paper_factory`.

```text
python3 -m pytest -q -p no:cacheprovider \
  tests/test_phase3_pro_major_regressions.py
12 passed in 1.78s
```

This file begins with four tests that reproduced the four Major defects against
the pre-fix stable implementation; all four failed before repair and pass after
repair. It now contains 12 focused regression cases spanning graph splicing,
inventory/blocker closure, occurrence identity, tombstones, mixed deletion,
cross-workflow equality, repeated semantic checkpoints, namespace, and
deterministic invalid-path handling.

```text
python3 -m pytest -q -p no:cacheprovider \
  tests/test_phase3_artifact_foundation.py \
  tests/test_phase3_authority_writer.py \
  tests/test_phase3_authority_read.py \
  tests/test_phase3_packaging_isolation.py \
  tests/test_phase3_artifact_registry_integration.py \
  tests/test_phase3_artifact_registry_shadow.py \
  tests/test_phase3_pro_major_regressions.py
72 passed in 7.99s
```

```text
python3 -m pytest -q -p no:cacheprovider \
  tests/test_authority_operations.py \
  tests/test_authority_outbox_delivery.py \
  tests/test_authority_production_migration.py \
  tests/test_authority_production_writer.py \
  tests/test_authority_read_repository.py \
  tests/test_authority_repository.py \
  tests/test_authority_schema_v2.py \
  tests/test_phase2_8_shadow_integration.py \
  tests/test_phase3_artifact_foundation.py \
  tests/test_phase3_artifact_registry_integration.py \
  tests/test_phase3_artifact_registry_shadow.py \
  tests/test_phase3_authority_read.py \
  tests/test_phase3_authority_writer.py \
  tests/test_phase3_packaging_isolation.py \
  tests/test_phase3_pro_major_regressions.py \
  tests/test_phase4_durable_operation.py \
  tests/test_phase4_durable_operation_integration.py \
  tests/test_phase5_pause_policy.py \
  tests/test_phase5_pause_policy_integration.py \
  tests/test_phase6_project_snapshot_ui.py \
  tests/test_phase8_data_egress.py \
  tests/test_phase8_reference_evidence.py \
  tests/test_phase8_shadow_isolation.py \
  tests/test_evidence_grounding.py \
  tests/test_aggregate_judges.py
383 passed in 38.39s
```

The broad whitelist covers migration freeze, transaction failure injection and
rollback, idempotency/replay/concurrency, source/revision/CAS fences, reader
malformation/cardinality failures, and retained Phase 2-8 composition.

```text
python3 -m pytest -q -p no:cacheprovider \
  tests/test_authority_production_migration.py::test_published_shadow_migration_checksums_are_unchanged_and_suffix_is_append_only \
  tests/test_authority_production_migration.py::test_a2_0001_through_a2_0014_statement_bytes_are_frozen \
  tests/test_phase3_authority_writer.py::test_no_mutation_keeps_frozen_v1_request_and_bundle_hashes \
  tests/test_phase3_authority_writer.py::test_phase3_exact_replay_and_mutation_hash_conflict \
  tests/test_authority_production_writer.py::test_same_idempotency_and_exact_companion_bytes_replay_without_new_revision
5 passed in 0.99s
```

`compileall` with a `/tmp` pycache prefix passed, and `git diff --check` passed.

## Wheel and import isolation

A real wheel was built offline from an isolated `/tmp` source copy. The first
isolation attempt identified that the configured `scripts*` package lacked an
`__init__.py`, so zipimport could not resolve the writer's existing
`scripts.model_dispatch_config` dependency. The minimal package marker was
added and covered by the packaging test.

Final wheel evidence:

```text
uv build --wheel --offline
Successfully built modeling_factory-2.0.0-py3-none-any.whl
unzip -tq: no errors
wheel SHA-256: 23b662a044a7fbbb7d7f6a186411e927e8eb7a6ae5ab92f13983a11cfc222998
```

Fresh `python3 -I` interpreters imported the domain, disabled runner, sole
writer, and read repository directly from the wheel. A separate fresh process
imported CLI, Service, and Scheduler and confirmed none of the four Phase 3
modules was loaded. The wheel contains the canonical Phase 3 modules and no
`shadow_contracts/` member.

## Full-suite and dependency evidence

The last completed project-wide run before this repair reported:

```text
2400 passed, 14 failed, 4 warnings in 295.04s
```

The repaired tree reran all 14 failing node IDs with long tracebacks and
reproduced exactly `14 failed, 31 passed in 6.04s`. Thirteen failures are in
`tests/test_web_frontend_runtime_helpers.py` and fail because this worktree has
no installed frontend `axios`, `katex`, `vue`, or `vue-router`; `npm ls` reports
an empty dependency tree while `package.json` and `package-lock.json` declare
the locked dependencies. The fourteenth is the tracked historical
`tests/test_m01_runtime_parity.py` rule, whose current bytes equal `HEAD` and
whose 2026-08-25 baseline forbids canonical/owner modules used by the already
reviewed additive Authority foundation. Phase 3 did not edit or weaken that
test. The repair ZIP includes all node IDs, complete raw tracebacks, dependency
output, the historical test source, its SHA-256/HEAD comparison, and attribution.

The current managed shell sandbox also makes this target worktree's
`run_state/solver_jobs` read-only. A separate rerun of the eight Solver routing
tests therefore produced four passes and four `EROFS` failures at the expected
test-only receipt writes. The earlier completed full run used the repository's
fake cloud client and did not perform real provider dispatch. This execution
constraint is recorded, not presented as a product regression or hidden by a
skip.

## Remaining risks

- There is no owner-policy migration executor. `MIGRATION_REQUIRED` remains an
  intentional hard stop.
- Re-attestation remains a dry-run classifier and does not invoke validators or
  persist checkpoints.
- Parity receipts remain evidence only; no route, threshold, or operator
  decision consumes them.
- The optional mutation is production-capable persistence code but has no
  active caller. Any activation requires a separate review of writer ownership,
  operational fencing, recovery, observability, and rollback.
- Descriptor-relative `O_NOFOLLOW` reads target Linux/Unix; environments without
  the required safe-open primitives fail closed.
- The shared tables contain legacy/backfill shapes. Phase 3 reconstructs only
  complete revision-bound typed bundles and intentionally refuses repair of
  mixed or malformed rows.
- A single uninterrupted current all-node run was not completed inside the
  managed sandbox because test-only Solver receipt paths are read-only. Current
  confidence rests on the 383-test touched-contract whitelist, the exact
  14-failure replay, independent Solver-routing evidence, and the prior complete
  full-suite run; this limitation is explicit in the audit package.
