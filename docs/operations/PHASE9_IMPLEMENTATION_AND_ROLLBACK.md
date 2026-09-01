# Phase9-A implementation, operation, and rollback

Status: control-plane implementation and offline tests are complete. Production
execution is `BLOCKED` until the exact external inputs listed below exist. This
document does not authorize migration, provider/network use, outbox dispatch,
delivery, release, deployment, or cutover.

## Implemented boundary

A2_0015 owns candidate/input/context-bound run-generation creation and
rotation. A2_0016 adds the Phase9-A forensic evidence-finalization graph:

1. `scripts/phase9_entry_gate.py` produces the candidate-bound entry
   `READY/BLOCKED` result from a query-only Authority snapshot and nine P0
   receipts, including AR-007.
2. A separate controlled-account start authorization may grant only Phase9-A
   finalization. Every other capability remains false.
3. `Phase9ForensicReplayService` reads one exact, bounded, no-follow,
   non-hardlinked evidence inventory. It verifies the Step-13 packet, missing
   claim zero-dispatch rule, three new role generations or typed no-judge
   ablation, raw/protocol/grounding/effective verdicts, one revision coordinate
   for every snapshot section, outbox/process receipts, all minimum acceptance
   cases, terminal reason, and delivery fence.
4. One `BEGIN IMMEDIATE` appends the replay, six-event predecessor-hash chain,
   terminal receipt, and idempotency row, then inserts or CAS-rotates the
   guarded current pointer. Evidence and Git source are reread before commit.
5. `collect_phase9_forensic_replay_state` reconstructs that complete graph
   through `mode=ro`, `query_only`, and one read transaction.

The finalizer records already-produced local immutable evidence. It does not
produce role output, launch a process, invoke Solver/model/provider code, send
network traffic, claim or dispatch an outbox row, publish a release, or apply a
migration. Test fixtures exercise the contract but are not production facts.

## Default-off configuration

`scripts/phase9_forensic_replay.py execute` returns `BLOCKED` before parsing a
configured path unless `PHASE9_ENABLED=true`. When enabled, all four values are
mandatory explicit absolute paths/identities:

```text
PHASE9_AUTHORITY_DB_FILE
PHASE9_AUTHORITY_SOURCE_FENCE_SHA256
PHASE9_SOURCE_REPOSITORY
PHASE9_EVIDENCE_ROOT
```

An enabled command is still a dry run unless `--confirm` is supplied. Delivery
has no enable setting: every request, migration constraint, event, receipt and
current row fixes it to `DISABLED`.

## Formal operation sequence

The migration command is an operational mutation and requires the repository's
existing verified pre-Authority backup and evidence-journal procedure:

```bash
python3 scripts/authority_operator.py migrate \
  --database /absolute/authority.db \
  --database-id <bound-database-id> \
  --expected-source-fence <sha256> \
  --backup /absolute/pre-authority.backup.db \
  --evidence-output /absolute/migration-evidence.json \
  --owner-token <controlled-owner-token> \
  --occurred-at <unix-seconds> --confirm
```

After independently generating a real entry request and all real evidence:

```bash
python3 scripts/phase9_entry_gate.py verify --request /absolute/entry-request.json

python3 scripts/phase9_forensic_replay.py preflight \
  --request /absolute/replay-request.json \
  --evidence-root /absolute/evidence-root

PHASE9_ENABLED=true \
PHASE9_AUTHORITY_DB_FILE=/absolute/authority.db \
PHASE9_AUTHORITY_SOURCE_FENCE_SHA256=<sha256> \
PHASE9_SOURCE_REPOSITORY=/absolute/frozen-source \
PHASE9_EVIDENCE_ROOT=/absolute/evidence-root \
python3 scripts/phase9_forensic_replay.py execute \
  --request /absolute/replay-request.json --confirm

python3 scripts/phase9_forensic_replay.py collect \
  --database /absolute/authority.db \
  --expected-source-fence <sha256> \
  --workflow-id <workflow-id>
```

Do not substitute templates, test databases, test receipts, invented role
outputs, or self-reported external results for these inputs.

## Failure and rollback

- Validation failure or any exception before commit rolls back every A2_0016
  row and leaves the prior current pointer unchanged. Exact same-key replay
  returns the original terminal receipt; different request bytes conflict.
- A committed replay is immutable. Roll forward through an explicitly created
  successor run generation and a `ROTATE` request; do not delete or rewrite
  history.
- A2_0016 is append-only and has no down migration. If the migration itself
  must be reversed, stop all use and invoke the existing Authority restore
  workflow against the exact verified pre-Authority backup. Never copy tables
  or edit schema-state rows manually.
- Preserve the database, backup, migration journal, source, official inputs,
  evidence set, request, and receipts for incident review.

## Current production blocker

No authorized production Authority database, official input bytes/manifest,
execution-context receipt, controlled-account Phase9 start authorization,
real role/process/outbox/snapshot evidence, or independent Pro verdict was
provided for this delivery. No production migration or replay was attempted.
Production status therefore remains `BLOCKED`. It may change only after all
those exact inputs are supplied, the candidate identity is frozen and reviewed,
and both entry and replay preflight return `READY` against the live read-only
state.
