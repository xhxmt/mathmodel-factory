# Phase9 candidate-bound entry gate

This document owns the current reviewed entry boundary. It does not authorize
Phase9-A forensic replay or any production behavior.

## Generation creation and rotation

`factory_core.phase9_run_generation.Phase9RunGenerationService` is the only
reviewed create/rotate service. The narrower
`AuthorityOperations.create_or_rotate_run_generation` method and
`scripts/authority_operator.py run-generation` command delegate to it without
exposing a SQLite connection or general update surface.

The operator command is a dry run unless `--confirm` is present. A confirmed
call still requires all of the following explicit inputs:

- exact Authority database and source-fence identity;
- current Git repository whose HEAD commit/tree/single parent match the typed
  request both before the transaction and immediately before commit;
- an official-input root whose regular, non-hardlinked file inventory and raw
  bytes exactly match the typed manifest before and after the transaction;
- a canonical execution-context receipt file, also rechecked before commit;
- a currently valid `CONTROLLED_OS_ACCOUNT` authorization whose UID and account
  match the process actually invoking the service;
- source-authorized contract pins and the live project/workflow/revision,
  runtime, and scheduler coordinates; and
- `V1_ONLY`, writer disabled, consumer disabled, and delivery capability
  `DISABLED`.

Initial project and run generation identities are content-derived. Rotation
requires the exact current predecessor generation and creation-receipt hash.
Same idempotency key plus identical canonical request bytes returns the original
receipt; different bytes conflict. Every write and current-pointer transition
is one `BEGIN IMMEDIATE` transaction, so failure leaves no partial generation.
Direct SQL and caller-invented generation labels are unsupported.

## Read-only collector

`collect_phase9_entry_state` uses the supported read-only Authority connector,
sets `query_only`, and joins all entry facts inside one SQLite snapshot. It
verifies the complete base/production migration installation, immutable
generation/current/receipt companions, concrete project/run/runtime/scheduler
coordinates, exact candidate commit/tree/parent, contract pins, delivery
fences, active project/solver processes, current-generation pending outbox, and
old-generation writes after the generation boundary. It never calls the state
store upgrade path, diagnostics, the Phase7/8 operator, or a mutation API.

## Candidate gate

`scripts/phase9_entry_gate.py verify --request <canonical-json>` additionally
requires:

- a clean exact Git source or a no-`.git` extraction whose complete frozen
  inventory recomputes the expected Git tree;
- exact original official-input bytes and the frozen execution-context receipt;
- a nonexpired candidate/project/workflow/run-bound controlled-account entry
  authorization; and
- exactly nine canonical P0 receipts:
  `AR_007_DELIVERY_BYPASS`, `HUMAN_DECISION_SINGLE_WRITER`,
  `PACKET_ZERO_DISPATCH_EFFECTIVE_VERDICT`, `COMMAND_READ_SET_CAS`,
  `WORKER_OUTBOX_PROCESS_TREE_RECEIPTS`, `OWNER_CHECKPOINT_REATTEST`,
  `REVISION_ATOMIC_SNAPSHOT`, `RUN_MODE_GENERATION_DELIVERY_PINS`, and
  `OFFICIAL_INPUT_EXECUTION_CONTEXT`.

Each P0 receipt binds the candidate identity, a zero-exit command record, its
raw test result, and a sorted source/evidence member set. Missing, tampered,
cross-candidate, nonzero, or side-effecting evidence fails closed.

The result is `READY` only when every receipt and live count passes. `READY`
does not authorize Phase9-A, provider/network access, production outbox or
delivery, release, deployment, migration, or cutover. When a controlled runtime
database, official input, execution context, or formal authorization has not
been supplied, the only honest result is `BLOCKED`.
