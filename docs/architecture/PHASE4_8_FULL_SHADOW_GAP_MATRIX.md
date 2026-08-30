# Phase 4-8 Full-Shadow Dependency and Gap Matrix

Status date: 2026-08-30

This document is the current implementation map for the Phase 4-8 full-shadow
slice.  It does not authorize a production cutover.  The sole frozen Phase 3
integration baseline remains
`PHASE3_FULL_SHADOW_PRO_REAUDIT_20260827_FIX5_20260827T224545Z.zip` with
SHA-256
`3faad2126a4aa5343b9ba12b6992d3c990bd2745e4dd347a6a26dc5920f733b2`.
That archive and the contracts it verified are not modified by this work.

## Inventory

| Slice | Prior/current state | Current full-shadow status or gap | Dependency / conflict |
|---|---|---|---|
| Phase 4A durable-operation contract | Complete for the selected pure identity, transition, nonce, generation, uncertain-reconciliation and receipt slice | No persistence, atomic intent/current-state write, lease recovery, exact replay or transaction fault testing | Keep `durable_operation.py` pure.  The Phase 2 delivery outbox is provider-delivery state and is not a Phase 4 operation store. |
| Phase 4B durable runtime/outbox | Missing | Durable lifecycle, claim/retry/reconcile, crash/restart, exact replay, rollback, default-off/no-dispatch | Use an explicit independent shadow SQLite path.  Do not extend the production Authority writer/read surface or frozen migrations. |
| Phase 5 pause policy | Complete for the selected pure 2-mode by 4-scope decision matrix | No supervisor lifecycle, scope inventory, replay/recovery or side-effect boundary | Active `ProcessSupervisor` and service pause/kill paths perform real process operations and must remain outside the shadow import graph. |
| Phase 5 supervisor | Missing | Durable scoped request/receipt state, ownership fencing, restart/replay/reconcile and injected no-op/recording port | Bind workflow, invocation, attempt, process scope and Phase 4 operation identity. |
| Phase 6A project snapshot UI | Frozen seven-state, single-coordinate projection retained and integrated behind a strict build-time flag in this candidate | Pure projection remains independently usable and fail-closed; full-shadow data must pass the Phase 6B Web boundary first | `ProjectSnapshotV0` remains permanently non-authoritative. Web ACL and delivery override are separate control-plane contracts. |
| Phase 6B verified snapshot/scoped grants | Implemented and locally verified in this candidate; independent Pro review remains pending | Canonical source/snapshot binding, independent durable store, scoped grant lifecycle, exact replay/restart and ACL-first Web read path are present; no cutover is authorized | Authority transfer remains impossible. No sixth Authority reader method or additional production writer was added. |
| Phase 7A grounding hardening | Complete for strict role envelopes, manifests, context/chunk/quote checks and fail-closed reports | No path-free input identity, durable verdict/receipt, exact replay, restart or Phase 6 binding | Preserve aggregate v1 compatibility and its existing sidecar schema.  Avoid the historical registration-only Phase 3 adapter. |
| Phase 7B grounding runtime | Implemented in this local full-shadow candidate | Bind canonical Phase 3 aggregate state and revision occurrence plus the exact Phase 6 current proof and role/manifest/context bytes; persist/replay the path-free effective verdict | Independent SQLite; no provider, network, outbox, production workflow callback, or authority transfer. |
| Phase 8A reference evidence | Complete for deterministic in-memory reference records | No trusted binding to artifact/snapshot/grant, persistence or recovery | Classification never grants authority. |
| Phase 8A data egress | Complete for pure staging/approval decisions with `dispatch_performed=false` | Caller-supplied approval is not a durable scoped grant; no expiry/revoke/replay ledger | Preserve `dispatch_capability=false`; do not add a consumer. |
| Phase 8B evidence/egress runtime | Implemented in this local full-shadow candidate | Durable PDF/CAS reference binding, scoped approval lifecycle, decision replay/current recovery and rollback | Binds Phase 3 aggregate/occurrence, Phase 6 current proof and Phase 7 receipt/current head; always no-dispatch. |
| Phase 2-8 joint acceptance | Implemented as an explicit-enabled synchronous local sidecar | Durable work ledger, restart, revoke/expiry, current-head rollback, exact replay and real CLI/service/worker/Web composition | Default-off import/resource isolation; historical pure adapters remain compatible; no production outbox or cutover. |

## Freeze-node status

Phase 4B and the Phase 5 supervisor are implemented as standalone full-shadow
runtimes. The first independent archive received
`CHANGES_REQUIRED` for two contained defects: its ownership checks occurred
after a read-write SQLite open/lock attempt, and Phase 5 appended stage suffixes
to otherwise valid long caller idempotency keys. FIX1 closed those defects but
its independent review found three more ownership edge cases: Phase 5 omitted
persistent indexes/views from its exact schema inventory, its companion marker
did not bind the parent directory inode, and both runtimes retained an empty
file after failed exclusive initialization. The FIX2 candidate expands the
exact object profile, binds/fences the Phase 5 parent identity, and performs
descriptor-bound quarantine cleanup only for uncommitted exclusive creations;
committed stores remain restartable.

The current Phase 4-6 candidate additionally applies one shared explicit fd
ownership-transfer protocol across Phase 4, Phase 5 and Phase 6. It is intended
to close the later F1 review finding covering `dup` exhaustion, asynchronous
`BaseException` windows, double-close/leak risk and cleanup errors masking the
primary failure. Phase 6B is now implemented from the current source contract:
it binds exact Authority/source and Phase 3/4/5 identities, persists verified
snapshots and closed-scope grant lifecycles in a separate SQLite store, and
offers only an authenticated ACL-first, non-authoritative Web read. Complete
Local validation is complete in this audit round and independent Pro review
remains pending; this paragraph does not retroactively claim that a historical
Phase 4+5 archive implemented or verified Phase 6.

Phase 7B and Phase 8B are implemented in the current local candidate and are
assessed from current source, exact structured tests, and a fresh-extraction
audit. This status is not a production-cutover claim. The selected contract is
[`PHASE7_8_DURABLE_FULL_SHADOW.md`](PHASE7_8_DURABLE_FULL_SHADOW.md).

## Persistence decision

The published `A2_0001` through `A2_0014` migration statement bytes,
checksums and order are frozen.  Their tables and the five-method read surface
do not provide a safe home for all mutable Phase 4-8 shadow lifecycles without
changing a production-capable contract.  The full-shadow B slices therefore
use caller-supplied, independent SQLite files and phase-prefixed tables.  A
disabled runtime must return before opening that path.  These databases are
test/audit evidence, not a new authority database and not a migration target.

## Shared invariants

- Every persisted input and result has deterministic canonical JSON bytes and a
  SHA-256 identity.
- The same idempotency key and exact request bytes return the original stored
  result without appending rows; different bytes under that key fail closed.
- Current projections and append-only receipts commit in one transaction.
- Explicit failure injection proves whole-transaction rollback.
- Reopening the SQLite file verifies stored hashes before replay or recovery.
- Existing databases pass an immutable read-only exact marker/schema profile
  before any read-write connection; new databases are exclusively created and
  path/inode-bound. Unknown sidecars and path replacement fail closed.
- Phase 5's profile covers every persistent table, index, view and trigger and
  its marker binds the parent-directory device/inode. Failed uncommitted Phase
  4/5 creation is cleaned only after quarantine identity verification and
  parent-directory fsync; a completed commit is preserved.
- Logical time is supplied by the caller; domain code does not consult the wall
  clock for expiry or lease decisions.
- Default-disabled calls open no database and call no injected port.
- Enabled shadow calls may record a synthetic `would_dispatch` or observation,
  but never invoke a provider, process supervisor, network client or production
  outbox consumer.
- No Phase 4-8 result is authoritative and no result can transfer authority,
  activate a route or approve production traffic.

## Frozen non-regression boundary

The Phase 4-8 work must leave unchanged: `A2_0001`-`A2_0014`, V1 request and
bundle identity, the sole public production mutation surface, the five public
query-only reader methods, the three Phase 3 ledgers, the Phase 3 default-off
and no-dispatch behavior, and the FIX5 immutable-bootstrap archive.

## Historical Phase 4+5 freeze exclusions

The audit cadence changed after Phase 4 implementation had completed and while
later-phase branches were still being drafted. The following table records the
scope of that earlier Phase 4+5 freeze only. It must not be used as the current
Phase 6 status:

| Later phase | Dirty worktree path | Phase 4+5 status |
|---|---|---|
| Phase 6B | `factory_core/phase6_snapshot_grants.py` | The then-existing interrupted draft was excluded from every Phase 4+5 completion claim. The current candidate independently reviewed and implemented this path under the Phase 6 contract above. |
| Phase 7B | `factory_core/phase7_grounding_runtime.py` | Interrupted isolated draft; excluded from the source closure and every Phase 4+5 completion claim. |
| Phase 7B | `tests/test_phase7_grounding_runtime.py` | Interrupted draft test; not run or counted by the Phase 4+5 audit and excluded from its source closure. |
| Phase 8B | No draft path was created before interruption | No Phase 8B implementation or test is part of this freeze. |

The FIX5 regression boundary contains the earlier Phase 6A UI shadow and
Phase 8A pure reference-evidence/data-egress contracts. Those files must not be
confused with historical Phase 6B-8B completion. The present Phase 6B candidate
is assessed from its current source, tests and self-contained audit evidence,
not by inheriting a historical Phase 6 completion claim.
