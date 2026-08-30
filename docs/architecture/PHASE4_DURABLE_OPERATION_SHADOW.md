# Phase 4 Durable Operation Shadow

Status: pure operation contract plus a durable, standalone full-shadow runtime.
Both are packaged as `factory_core` modules, remain outside current production
entry points, and have no runtime authority or dispatch capability.

## Selected slice

`factory_core/durable_operation.py` defines the worker-launch operation
identity, logical idempotency key, immutable state value, deterministic
transition receipt, and transition gate. `factory_core/phase4_shadow_runtime.py`
persists that contract in an explicit, caller-supplied standalone SQLite file.

The normal lifecycle is:

```text
PENDING -> CLAIMED -> DISPATCH_CHECKPOINTED -> ACTIVE -> SUCCEEDED / FAILED
```

The runtime atomically reserves one immutable synthetic launch intent, current
CAS state, append-only transition receipt, and exact idempotency result. It
records logical-time leases, owner epochs, expired pre-dispatch reclaim/retry,
checkpoint nonce, uncertain state, and synthetic reconciliation. Same key and
same canonical request returns the original state/receipt without new rows;
different bytes fail with a typed conflict. Reopening the SQLite file verifies
canonical bytes and hashes before reconstruction. Failure hooks after every
transaction stage prove whole-transaction rollback.

The SQLite ownership fence is part of the runtime contract. A new store is
created with `O_EXCL`, binds its absolute path, parent and created inode in an
immutable in-database marker, and is initialized through an anchored
`/proc/self/fd` connection. An existing store is first opened read-only with
`O_NOFOLLOW`; its rollback-journal header, exact marker and complete ordered
`sqlite_master` profile are checked through `mode=ro&immutable=1` before any
read-write connection or lock upgrade. The same descriptor then anchors the
read-write connection, and path-to-inode identity is checked around transaction
entry and commit. Exact legacy-v1 stores created by the earlier Phase 4 shadow
runtime remain readable/replayable through a separate frozen profile; empty,
prefix-only, extra-object and unknown-schema databases fail closed. Existing
WAL, SHM or journal sidecars are never opened or recovered implicitly.

Linux adversarial tests hold a writer lock on a foreign WAL database and use a
self-checked inotify watcher to prove rejection creates, changes and deletes no
database or sidecar file, opens no SQLite connection, and does not wait for the
writer lock. Separate path-replacement and initialization-error tests prove the
anchored commit fence, rollback and descriptor release. If exclusive creation
has succeeded but connection, schema construction or the actual commit fails,
the public basename is atomically moved to a private quarantine and compared
with the retained creation descriptor before it can be deleted. A raced foreign
replacement is restored without overwrite, the anchored parent is fsynced, and
the same path can be initialized after the failed uncommitted attempt. Once the
SQLite commit has actually completed, later fence failure preserves the full
store for restart verification.

The runtime has no callback or consumer that could launch a process, call a
provider, use the network, or dispatch an outbox message. Every state and
receipt fixes `authoritative=false` and `dispatch_performed=false`. Logical time
is always caller supplied.

## Isolation boundary

The current Scheduler, CLI, Web/API, TransitionCoordinator, direct worker
launcher, provider/model/Solver paths, Authority/legacy SQLite schemas, and
production writers do not import or call either Phase-4 shadow runtime. The
disabled runner returns before validating or opening a database path. No
feature flag or cutover is introduced. The v1 runtime remains the sole
production authority.

Production adoption remains a separate change requiring an approved authority
schema/read-write contract, consumer ownership, real process receipts,
old-launcher exclusion, OS crash-window evidence, operator rollout, and
cutover. None of those production capabilities is implied by the standalone
shadow database.
