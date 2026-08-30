# Phase 5 Pause Policy Shadow

Status: pure decision matrix plus a durable, standalone full-shadow supervisor.
The modules are packaged under `factory_core` but are not imported or called by
current production entry points and have no runtime authority.

## Selected contract

`factory_core/adapters/infrastructure/pause_policy.py` maps one pause mode and
one owned process-scope kind to an immutable `PauseDecision`:

| Process scope | `pause` | `pause-and-cancel-solvers` |
| --- | --- | --- |
| `worker` | `terminate-scope` | `terminate-scope` |
| `model` | `terminate-scope` | `terminate-scope` |
| `attached-solver` | `terminate-scope` | `terminate-scope` |
| `durable-solver` | `continue` | `request-cancel` |

The API accepts the defined enums or their exact wire strings. Unknown modes or
scope kinds raise `PausePolicyError`. `PauseDecision.as_dict()` returns the
stable wire fields `mode`, `scope_kind`, `action`, and `reason_code`.

## Durable supervisor slice

`factory_core/phase5_shadow_supervisor.py` binds one pause request to the exact
workflow, invocation, attempt, process-scope, Phase-4 operation identity, and
scope kind. It uses a separate caller-supplied SQLite file and the lifecycle:

```text
REQUESTED -> EFFECT_CHECKPOINTED -> COMPLETED
                              \-> RECONCILIATION_REQUIRED -> COMPLETED
```

Request/current-state/append-only receipt/idempotency writes commit atomically.
Same-key exact replay returns the original state and receipt without a new row
or second port call; different bytes fail closed. A state CAS and exact scope
binding reject cross-workflow or stale-scope transitions. Reopen verifies all
canonical JSON and hashes. An interrupted post-checkpoint request is moved to
reconciliation after restart instead of blindly replaying an effect.

Caller idempotency keys remain valid through the documented 512-character
boundary. Checkpoint, observation and restart-recovery keys are not formed by
suffix concatenation: each is a bounded, reserved-namespace SHA-256 identity
over the base key, request ID, closed stage name and schema version. Caller keys
cannot enter that internal namespace. Boundary tests at 495, 496, 500, 511 and
512 characters cover normal completion, exact and conflicting replay, a port
crash after the durable checkpoint, restart into reconciliation and zero repeat
port calls.

Every store entry point uses the same fail-before-write ownership fence. New
database and companion marker files are `O_EXCL`-created and bind the absolute
path, database inode, parent-directory device/inode, exact allowed-object set
and schema profile. The exact `sqlite_master` inventory includes every
persistent table, index, view and trigger plus the deterministic SQLite
automatic indexes; extra ordinary, unique, partial or expression indexes and
views therefore fail closed. Existing files are anchored with `O_NOFOLLOW`,
checked through
`mode=ro&immutable=1`, and only then reopened read-write through the same
`/proc/self/fd` inode. Unknown objects, marker drift, prefix-only impostors,
WAL/SHM/journal sidecars and path replacement fail closed. A separate exact
legacy profile preserves restart/replay of stores created by the earlier
shadow runtime. Linux inotify/writer-lock tests prove foreign WAL rejection has
zero directory mutation and zero SQLite connection; commit-race and error-path
tests prove rollback and descriptor release. Failed uncommitted exclusive
initialization quarantines each candidate basename, verifies the retained
creation inode before deletion, and fsyncs the anchored parent. Foreign raced
replacements are restored without overwrite, while an already committed store
is retained and restart-verifiable.

The only injected boundary is `SyntheticEffectPort.record_would_apply`, which
records a typed test/audit observation after the durable checkpoint. It is not
a process or provider API. The repository supplies no production
implementation, and tests use recording/no-op fakes only. Unknown observations
remain reconciliation-required; conclusive synthetic observations may close
the shadow state.

## Isolation boundary

The pure policy does not inspect a process tree or state. The supervisor does
not import `ProcessSupervisor`, subprocess/socket/provider code, the production
Authority writer, or the production outbox. Every state/run fixes
`authoritative=false`, `authority_transferred=false`,
`dispatch_performed=false`, `process_signal_performed=false`, and
`provider_call_performed=false`. Logical time is caller supplied. When disabled,
the runner returns before SQLite or its injected port. Neither module is
imported by `factory_core.cli`.

The current v1 pause implementation remains the sole production authority.
Real process inventory, signal/cancel execution, provider reconciliation,
Authority persistence, production port implementation, rollout and cutover
remain separately approved work. This full-shadow lifecycle is evidence for
those contracts, not permission to perform them.
