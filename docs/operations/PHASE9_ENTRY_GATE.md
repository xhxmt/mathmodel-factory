# Phase9 candidate-bound entry gate

Status: the implementation contract is **fixed offline**. A particular
candidate's immutable identity and test outcomes are established only by that
package's manifest, command records, raw logs and derived summary; this document
is not an independent audit verdict. Production is `BLOCKED`; formal Phase9-A
and Run4 have **not run**, Phase 9 is incomplete, and Phase10-B has **not
started**. This document is not an operator authorization or production receipt.

## Run-generation boundary

`factory_core.phase9_run_generation.Phase9RunGenerationService` is the only
supported create/rotate service. The narrower Authority operation and operator
CLI delegate to it without exposing a general SQLite update surface. A
confirmed call still requires all of the following:

- a complete A2_0019 Authority installation, `V1_ONLY`, writer/consumer off;
- the fixed tuple `FORENSIC_REPLAY`, `LEGACY_NOT_APPLICABLE`, `DISABLED`;
- one exact project, workflow, revision and project/run/runtime/scheduler
  coordinate, with no `legacy_unknown` component;
- a live Git commit/tree/single parent and a byte-verified inventory of every
  tracked regular file, checked at start and before commit;
- a strict official-input snapshot and canonical execution-context receipt,
  each checked at start and before commit;
- an unexpired controlled-account authorization over the complete canonical
  operation target; and
- for rotation, the exact current predecessor, its creation receipt, immutable
  Phase9 terminal receipt, terminal hash and current revision.

The authorization target includes the idempotency key, operation, candidate
and source inventory, project/workflow, predecessor/target, pins, official
inputs, execution context, mode, contract, capability and derived generation.
Its statement hash is recomputed. Authorization is consumed once; only an
exact same-request idempotent replay may return the recorded result. All writes
and the current-pointer CAS occur in one `BEGIN IMMEDIATE` transaction.

The shared official-input validator opens a stable root descriptor, performs
no-follow traversal, records each directory identity, reads members through
dirfd-relative descriptors, and rechecks every member, ancestor and final path.
It rejects links, hard links, special files, path escape/casefold/Unicode
collisions, extra/missing members, unreadable subtrees and every traversal or
replacement error. Entry and run-generation use the same validator.

## Query-only live state

`collect_phase9_entry_state` opens only an existing explicit Authority database
with `mode=ro`, enables `query_only`, and reads one consistent transaction. It
does not create a database, enable WAL or invoke schema upgrade. The collector
requires the complete migration prefix through A2_0019 and semantically joins:

- current generation, creation receipt, consumed authorization and source
  inventory;
- concrete candidate/project/workflow/generation/pin/input/context facts;
- active process and pending/uncertain outbox counts;
- unresolved migration state and old-generation events after the boundary; and
- the Authority revision/state hash used by the gate and finalizer; and
- the Authority-issued P0 runner authorization, one-time nonce consumption and
  immutable successful execution attestation.

Any unavailable, malformed, semantically inconsistent or non-quiescent state
is `BLOCKED`; it is never normalized to an empty or completed state.

## Formal P0 evidence domain

The gate requires exactly these nine requirement IDs:

- `AR_007_DELIVERY_BYPASS`
- `HUMAN_DECISION_SINGLE_WRITER`
- `PACKET_ZERO_DISPATCH_EFFECTIVE_VERDICT`
- `COMMAND_READ_SET_CAS`
- `WORKER_OUTBOX_PROCESS_TREE_RECEIPTS`
- `OWNER_CHECKPOINT_REATTEST`
- `REVISION_ATOMIC_SNAPSHOT`
- `RUN_MODE_GENERATION_DELIVERY_PINS`
- `OFFICIAL_INPUT_EXECUTION_CONTEXT`

Formal receipts and test fixtures are different typed domains. A
`TEST_FIXTURE`, test-only producer or hand-built JSON can exercise rejection
logic but can never contribute to a formal `READY`. Each formal receipt binds
the candidate commit/tree/parent, complete executed-source inventory,
project/workflow/generation, one fixed P0 requirement and its exact test nodes.
Its byte-length/SHA-256 references bind the canonical command record, complete
raw log, parsed outcome and supporting attestations. The referenced command
record in turn binds the fixed runner/spec, full argv, cwd, Python identity,
sanitized environment, trusted start/finish times and observed exit status.

The only formal local evidence runner consumes an Authority-issued short-lived
nonce for the exact live coordinate, then executes one fixed allowlist of exact
pytest nodes from a pristine candidate source inside a mandatory no-network
`bwrap` namespace. The host root and candidate are read-only, the dependency
checkout is masked except for its read-only virtual environment, and only
dedicated report/temp directories are writable. It preserves the complete log
and JUnit report and writes an immutable Authority attestation only after
rechecking live state, source, interpreter, OS identity and expiry. Validation
parses the real pytest collection and terminal outcomes and
rejects a nonexistent node, arbitrary `PASS` text, self-reported exit status,
empty or truncated output, setup/collection errors, non-PASS outcomes, wrong
cwd/interpreter/source inventory, cross-coordinate reuse or command/log
mismatch. It also checks the exact ordered 13 JUnit testcase identities,
producer type/version/source blob, sandbox binary/argv and candidate import
origins. A self-rehashed file tree without its matching Authority attestation
cannot yield `READY`. The complete evidence root is inventoried with the same
no-link/no-special/path-conflict rules and is read before and after validation.
Missing, extra, aliased, replaced or changing evidence blocks the gate. Its root
SHA-256 is bound into the operator authorization and gate result.

## Candidate gate and trusted time

`scripts/phase9_entry_gate.py verify` combines the live state, byte-verified
candidate source, strict official inputs and context, all nine formal P0
receipts and a controlled-account entry authorization. The CLI supplies a
trusted timezone-aware UTC clock; caller-provided `evaluated_at` is audit
metadata only. `issued_at`, `expires_at` and the 300-second skew bound are
checked against trusted time. Any missing, expired, stale, cross-coordinate or
mutated component produces `BLOCKED` and no formal READY receipt.

A `READY` result binds the exact Authority state receipt/revision and P0
evidence-root hash. It is intentionally entry-only and cannot be reused as a
Phase9-A start grant. Replay requires a distinct short-lived, one-use start
authorization/nonce that binds the entry result. The replay service reacquires
the same shared live gate at transaction start and immediately before terminal
commit. A new active process, pending/uncertain outbox row, migration change,
predecessor/current change, source/input/context change, expiration or nonce
reuse rolls back terminal and pointer writes.

## Delivery and stage boundary

Every Phase9 generation has delivery capability `DISABLED`. Release, final
acceptance and final submission independently read current Authority state
before any side effect and reject forensic, technical, ablation, stale,
override or cached decisions. The entry result grants none of provider/network,
production outbox, delivery, release, deployment, migration or cutover.

Repository contracts/tests and packaged offline test records are not real
production P0, replay, process, provider, outbox, acceptance or terminal
receipts. Their exact candidate binding and outcomes must be recomputed from the
package; they do not establish an independent verdict by themselves. A real
Authority database, verified backup, official input, execution context and live
authorizations were not supplied. Production is therefore `BLOCKED`; A2_0016
through A2_0019 are `NOT APPLIED` in production, formal Phase9-A and Run4 are `NOT
RUN`, Phase 9 is incomplete and Phase10-B is `NOT STARTED`.
