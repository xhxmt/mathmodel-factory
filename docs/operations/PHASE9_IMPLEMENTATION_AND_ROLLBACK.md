# Phase9-A implementation, operation, and rollback

Status: the implementation contract is **fixed offline**. A particular
candidate's immutable identity and outcomes belong to its manifest, command
records, raw logs and derived summary; this document is not independent audit
approval. Production is `BLOCKED`; A2_0016 through A2_0019 are **not applied** in
production, formal Phase9-A and Run4 are **not run**, Phase 9 is incomplete, and
Phase10-B is **not started**. This document does not grant permission to mutate
a production Authority database or call a provider, worker, outbox, delivery or
release path.

## Implemented boundary

The production migration prefix is append-only:

- A2_0015 owns candidate/input/context-bound run-generation create/rotate;
- A2_0016 owns the replay/event/terminal/idempotency/current graph; and
- A2_0017 adds full tracked-source inventories, one-use canonical operation
  authorizations, typed replay receipt storage, one-use entry-gate consumption
  and database guards for fixed mode/contract/capability, predecessor terminal
  and exact receipt counts; and
- A2_0018 adds Authority-issued/consumed P0 runner nonces and immutable
  successful-execution attestations bound to the complete external evidence root;
  and
- A2_0019 adds Authority-issued/consumed replay-evidence and runtime observation
  authorizations, immutable runner-event attestations and formal typed runtime/
  component/acceptance provenance.

A2_0017 does not change any A2_0010-A2_0016 statement byte; A2_0018 does not
change any A2_0010-A2_0017 statement byte; A2_0019 does not change any
A2_0010-A2_0018 statement byte. Migration is still
an explicit operator operation requiring an existing real schema-v9 database,
verified pre-Authority backup, durable journal, stable owner and disabled
writer/consumer state. The API and tests do not apply it automatically.

## Entry and generation

The formal P0 producer/consumer contract is separate from `TEST_FIXTURE` data.
It binds actual candidate bytes and parsed command/log semantics for exactly
nine P0 requirements. The supported producer must consume a short-lived
Authority nonce and run the exact 13-node suite in a read-only, no-network
`bwrap` namespace before Authority records its successful attestation;
arbitrary JSON, PASS text, a file-only self-rehash, self-reported exit status or
partial evidence cannot produce formal `READY`.

`Phase9RunGenerationService` fixes every Phase9 generation to:

```text
run_mode=FORENSIC_REPLAY
modeling_consultation_contract=LEGACY_NOT_APPLICABLE
delivery_capability=DISABLED
```

The Python validator and A2_0017 triggers both enforce that tuple. The service
verifies the complete tracked-source inventory, official-input descriptor tree
and execution-context receipt at transaction start and before commit. Its
authorization covers the complete canonical request and is consumed once.
Rotation requires the current predecessor's exact immutable terminal receipt
and a CAS over predecessor, terminal hash and current revision. A creation
receipt is not a terminal receipt.

## Replay finalization

The formal replay-evidence producer consumes Authority-issued one-use
authorizations and persists the actual aggregate acceptance command, raw log,
trusted reporter events and JUnit output together with typed runtime/component
facts. `Phase9ForensicReplayService` is the separate local finalizer: it cannot
upgrade caller-authored JSON or a pre-populated SQL graph into formal evidence.
It inventories the complete bounded evidence root and validates the contents of
each typed receipt. Technical replay requires exactly three new
role process/provider pairs with output bytes and provenance. Both technical
and typed no-judge replay require three process-scope receipts and all 17
canonical acceptance-case receipts. Each acceptance receipt binds its command
record, complete raw log and parsed result. Packet claims are recomputed from
the exact packet bytes; snapshot coordinates and layered verdicts are checked
independently. Cross-generation/scope/invocation, inherited, renamed,
duplicated, missing, extra or digest-only evidence fails closed.

The raw packet is canonical JSON with exactly this shape:

```json
{
  "schema": "authority-phase9-packet-v2",
  "rebuild_start": "STEP13_PACKET_REBUILD",
  "required_claims": ["<sorted-unique-claim-id>"],
  "claims": [
    {
      "claim_id": "<sorted-unique-present-claim-id>",
      "content_sha256": "<64-lowercase-hex>"
    }
  ]
}
```

`required_claims` is sorted and unique; `claims` is sorted uniquely by
`claim_id`, and each claim object has exactly `claim_id` and `content_sha256`.
A missing required claim is a blocked preflight with zero dispatch. The
canonical `packet.json` control descriptor
has schema `authority-phase9-packet-evidence-v1`, exact keys `schema`,
`required_claims`, `present_claims`, `packet_path`, `packet_sha256` and
`dispatch_count`, and binds the bytes at `packet_path` by SHA-256. The finalizer
derives both claim inventories again from those exact raw packet bytes. A
control-file assertion, digest without payload bytes, noncanonical payload or
renamed/extra packet field is not evidence.

At start, the service opens one `BEGIN IMMEDIATE` transaction, reacquires the
same query-only live gate used by entry, checks the entry Authority state hash,
validates trusted time and atomically consumes the start authorization nonce.
Immediately before terminal commit it reruns the shared live gate and rereads
source, inputs, context and evidence. Any drift, expiry or nonce reuse rolls
back the replay, receipt set, terminal, idempotency and pointer writes.

Only after all validation succeeds does one transaction append the replay,
ordered predecessor-hash event chain, typed receipt rows, gate consumption,
terminal and idempotency record, then insert or CAS-rotate the current replay
pointer. A same-key/same-request replay returns the immutable result; any other
reuse conflicts.

The query-only collector opens an existing database with `mode=ro` and
`query_only`, decodes every typed generation/request/event/terminal/current
field and reconstructs the expected event semantics. A hash-consistent but
semantically wrong SQL graph is `ERROR`, not `COMPLETED`; the collector never
repairs or normalizes state.

## Delivery boundary

Phase9 never has a delivery-allowed state. Before any project file, release
manifest, pointer, acceptance record or final-submission side effect, each
public entry reads current Authority generation and terminal state and checks
project/workflow/generation, run mode, modeling contract and capability.
Forensic replay, `TECHNICAL`, `ABLATE_NO_JUDGE`, `DISABLED`, stale terminal,
old cache and every override are rejected. A later delivery-capable phase must
have a separate, explicitly reviewed contract; it cannot reinterpret Phase9
evidence.

## Default-off and formal operating gate

The replay command is disabled unless explicitly configured and remains a dry
run without confirmation. Even when enabled, it cannot authorize provider,
network, production outbox, delivery, release, deployment, migration or
cutover. Before a production operator could consider execution, all of these
external gates must exist and be independently verified:

1. the exact new commit/tree/parent and audit ZIP pass independent review;
2. a real Authority database has a verified backup and the ordered A2_0016
   through A2_0019 migration journal approved for the controlled operation;
3. exact official input and execution context are frozen;
4. nine formal P0 receipts and the live query-only gate produce a current
   `READY` result;
5. a distinct short-lived, one-use controlled-account start authorization
   binds that entry result; and
6. the complete real packet/role/provider/process/outbox/snapshot/verdict/
   acceptance evidence root is present.

Templates, test databases, fixture receipts, old-generation output, old PASS
or delivery-decision files cannot satisfy these gates.

## Failure and recovery

- A validation failure, injected fault or live-state change before commit
  rolls back every transaction row and leaves both current pointers unchanged.
- An uncertain external dispatch is reconciled from durable Authority facts;
  it is never automatically resent merely because the local caller restarted.
- Committed generations, event chains, typed receipts and terminal receipts are
  immutable. Recovery is a new explicitly authorized successor; do not edit or
  delete history.
- A2_0017 through A2_0019 have no down migration. Database rollback means stopping all writers
  and restoring the exact verified pre-Authority backup through the existing
  Authority restore workflow. Never copy tables, remove triggers or edit
  schema-state/migration-history rows manually.
- Preserve the database, original backup, migration journal, candidate archive,
  official inputs, execution context, authorization, evidence root and command
  logs for incident review.

## Current production state

Contracts, schemas, tests and packaged source/fresh records are offline
candidate material, not real production receipts or an independent audit
verdict. No authorized Authority database, verified production backup, official
input, execution context, live authorization or replay evidence was supplied,
and no production action was attempted. Production remains `BLOCKED`; A2_0016
through A2_0019 remain `NOT APPLIED` in production; formal Phase9-A/Run4 remains
`NOT RUN`; Phase 9 is not complete; Phase10-B remains `NOT STARTED`.
