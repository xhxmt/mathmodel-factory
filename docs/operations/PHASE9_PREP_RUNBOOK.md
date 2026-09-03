# Phase9-A preparation runbook

Status: this runbook defines the **fixed-offline implementation and immutable
package-evidence process**. Exact candidate identity and outcomes belong to the
generated package's manifest, command records, raw logs and derived summary;
the package remains a candidate until a separate independent audit passes.
Production remains `BLOCKED`; A2_0016 through A2_0019 are `NOT APPLIED` in
production; formal Phase9-A and Run4 are `NOT RUN`; Phase 9 is incomplete; and
Phase10-B is `NOT STARTED`.

Nothing in this runbook authorizes a production mutation, provider/network
call, worker, Solver, outbox dispatch, delivery, release, deployment or
cutover.

## Phase names

- **Phase9-A — Run4 Forensic Replay** starts with a new generation at
  `STEP13_PACKET_REBUILD` and is always delivery-disabled.
- **Phase10-B — Fresh Clean-room Acceptance** is a later new project and
  generation running the complete workflow without reuse or override.

Preparation, test fixtures and a valid audit package are not Phase9-A. Phase9-A
does not complete Phase 9 until its real terminal evidence receives the
required independent verdict. Phase10-B cannot start before that separate gate.

## Offline candidate preparation

Build and review only from an immutable single-parent commit. A source run may
execute only when tracked/index bytes are clean. A fresh run is exported from
that commit without `.git` or untracked files. Both forms must independently
recompute commit/tree/parent and the complete tracked-source byte inventory.

The audit runner uses an explicit Python executable, cwd, argv, isolated HOME
and cache, no user site, no bytecode and disabled pytest plugin autoload. It
does not inherit unrecorded provider, production database, release, deployment
or cutover configuration. Every attempt gets a unique append-only command
record and full raw log; a later success never overwrites a failed attempt.

The required suite graph is owned by
`docs/operations/PHASE9_TEST_SUITE_CONTRACT.json`. It requires independent
source and fresh executions for:

1. `phase9_focused`;
2. `entry_ar007`;
3. `a2_migration`;
4. `phase1_8_continuous`;
5. `phase7_8_regression`;
6. `release_workflow`; and
7. `full_repository`.

`tools/build_phase9_test_summary.py` reparses the raw logs, enforces the exact
suite/environment set and required targets, checks every file reference and
source inventory, accounts for all outcome categories and warnings, and
requires exact source/fresh outcomes. Passed counts are claimed only when the
corresponding complete raw log and command record exist; unbound aggregate
counts are not evidence.

The package builder accepts only that reproducible summary and an honestly
`BLOCKED` production status. It reads candidate files from immutable Git
objects, excludes untracked/runtime/credential/database/archive content and
produces one-root, fixed-time, normalized-mode manifest/checksum closure.

## Formal entry prerequisites

After the new candidate ZIP passes an independent audit, formal entry still
requires all of the following real material:

1. exact reviewed commit/tree/parent and full source-byte inventory;
2. a real Authority database, verified backup, stable migration owner and
   approved ordered A2_0016-A2_0019 application journal;
3. exact project/workflow/revision and project/run/runtime/scheduler
   generations, with no `legacy_unknown`;
4. frozen contract pins, strict official-input bytes and execution-context
   receipt;
5. nine formal, current-candidate P0 receipts produced by the trusted local
   runner—not fixture or hand-built receipts;
6. zero active processes, pending or uncertain outbox rows and unresolved
   migrations, with no old-generation events past the boundary;
7. an entry authorization valid against trusted UTC time; and
8. a distinct short-lived one-use start authorization binding the exact entry
   result and Authority state hash.

The query-only collector and P0 verifier are described in
`PHASE9_ENTRY_GATE.md`. Any missing, stale, altered or cross-coordinate input
returns `BLOCKED`. A `READY` entry result is not a provider, migration, replay,
delivery or release grant.

## Formal forensic evidence contract

The checked-in `PHASE9_FORENSIC_EVIDENCE.template.json` is a non-receipt
skeleton. It intentionally contains only `NOT_COLLECTED`, `NOT_CREATED` and
`NOT_RUN` placeholders. Never edit it to simulate production completion.

The real external evidence root must be a strict no-link/no-special inventory
and include the canonical control files, exact packet bytes and all referenced
typed receipts. Each receipt binds candidate, project/workflow, generation,
invocation, attempt, scope, packet/output/dependency hashes, producer,
occurred-at and predecessor/event coordinates as applicable. The finalizer
reads receipt content; a 64-hex digest or aggregate self-report is insufficient.

The raw packet payload must be canonical JSON with exactly the keys `schema`,
`rebuild_start`, `required_claims` and `claims`. Its fixed values are
`schema="authority-phase9-packet-v2"` and
`rebuild_start="STEP13_PACKET_REBUILD"`; `required_claims` is a sorted unique
array of nonempty claim IDs, and `claims` is sorted uniquely by `claim_id`, with
each object containing exactly `claim_id` and a 64-lowercase-hex
`content_sha256`. The `packet.json` control descriptor is a separate canonical
object with schema `authority-phase9-packet-evidence-v1` and exactly `schema`,
`required_claims`, `present_claims`, `packet_path`, `packet_sha256` and
`dispatch_count`. It must name the raw payload file, bind its exact bytes by
SHA-256, and repeat only the inventories derived from those bytes. A missing
required claim requires zero dispatch and blocks finalization.

For technical replay, the first packet rebuild requires three newly generated
roles—math, execution and paper—with process/provider receipts, exact output
bytes and provenance that does not point to an old generation, old role or old
Pro content. Typed `ABLATE_NO_JUDGE` is a separate nonzero terminal route with
no role output. The two routes are never mixed.

The acceptance inventory is exactly:

| Area | Canonical case IDs | Required fact |
| --- | --- | --- |
| Delivery | `AC-DEL-001`, `AC-DEL-002` | Technical and no-judge Phase9 remain disabled; no release/acceptance/submission side effect |
| Outbox | `AC-OUT-001`, `AC-OUT-002`, `AC-OUT-004` | Precommit failure launches nothing; committed work reclaims once; uncertain dispatch reconciles without resend |
| Packet | `AC-PACKET-001`, `AC-PACKET-002`, `AC-PACKET-003` | Exact packet bytes/claims; missing claim means zero dispatch; later reruns use dependency fingerprints |
| Replay | `AC-RUN4-001`, `AC-RUN4-002` | New generation, Step-13 rebuild, immutable typed terminal, no inherited content |
| Snapshot | `AC-SNAP-001`, `AC-SNAP-002` | One shared revision coordinate; read failure is `ERROR` |
| Supervisor | `AC-SUP-001`, `AC-SUP-002`, `AC-SUP-004` | Invocation/attempt/scope identity, durable pause/cancel semantics and no live descendant |
| Verdict | `AC-VERDICT-001`, `AC-VERDICT-003` | Raw/protocol/grounding/effective layers; contradictory aggregate fails closed |

A future formal evidence root must give each of these 17 cases its own receipt,
command record, full raw log and parsed test result. The only formal Run4 IDs
are `AC-RUN4-001` and `AC-RUN4-002`. Underscore spellings, legacy labels and
compatibility-only fixture/test names do not expand the formal inventory and
are rejected by the finalizer.

## Transaction and live-state checks

The finalizer performs the same live-state validation at transaction start and
immediately before commit. It additionally rereads candidate source, official
inputs, execution context and the full evidence root. Any active process,
pending/uncertain outbox, migration/predecessor/current drift, byte change,
authorization expiry or nonce reuse aborts the transaction.

Only a complete validated graph can append typed receipts, ordered events,
terminal and idempotency rows and then CAS the current replay pointer. Every
failure path leaves no terminal, pointer, acceptance, release or partial Phase9
state. The collector semantically reconstructs the committed graph in read-only
mode and returns `ERROR` for a hash-valid but semantically wrong graph.

## Production and rollback boundary

Phase9 fixes `run_mode=FORENSIC_REPLAY`,
`modeling_consultation_contract=LEGACY_NOT_APPLICABLE` and
`delivery_capability=DISABLED`. Release, final-acceptance and final-submission
APIs independently recheck current Authority state before side effects; old
PASS, override or cached decisions cannot lift the fence.

If a future authorized migration/replay fails, preserve all source, database,
backup, journal, input, authorization, evidence and log bytes. Uncommitted
transaction state rolls back. Committed immutable history is never edited;
recovery is an authorized successor generation. Schema rollback uses the exact
verified pre-Authority backup and existing Authority restore procedure, never
reverse SQL or manual row deletion.

## Phase10-B boundary

Phase10-B requires a separate authorization after completed, independently
accepted Phase9-A evidence. It uses a new project/generation, pinned runtime and
dependencies, verified official input and a complete no-reuse/no-override run
through all current Stages, Steps, Human Gates, Solver/provider receipts,
snapshot, verdict and delivery process. This repair candidate neither starts
nor authorizes Phase10-B.
