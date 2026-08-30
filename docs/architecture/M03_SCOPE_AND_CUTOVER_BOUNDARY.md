# Run4 M0.3 Scope and Cutover Boundary

M0.3 is one three-part shadow/prototype milestone:

- M0.3a separates pure dirty-classifier semantic/operational identity,
  persisted dirty-owner policy identity, additive Workflow V2 and explicit
  ContractPinSet/runtime pins.
- M0.3b builds Project Snapshot V0 from one read-only SQLite transaction with
  typed missing/error/redacted/paged facts.
- M0.3c validates a CommandEnvelope and structured read-set CAS entirely from
  explicit immutable inputs.

No individual slice is M0.3 completion. The WAL boundary is governed by the
higher-priority `APPROVE_M03_WAL_ADDENDUM` scheme-A rules; completion still
requires full focused/regression/adversarial/zero-side-effect gates,
five-seed replay, frozen PRE/POST source inventories and a deterministic
archive-only evidence bundle.

The attempt-5 implementation candidate also closes the three semantic defects
found in the rejected attempt-4 review: source-invalid SQLite domain rows no
longer become available Snapshot facts; required fact generations and
fingerprints are bound to command scopes before read-set reconstruction; and
the Snapshot-to-CAS bridge uses a fixed six-rule projection plus recoverable,
source-authorized pins. Attempt-4 remains rejected historical implementation
evidence and contributes no current pass result.

Attempt-6 retains those closures and fixes the sole P1 from the rejected
attempt-5 review: SnapshotPolicyV0 now derives both its accepted Solver status
vocabulary and event-history validation from one source-authorized lifecycle
mapping, including the production-written `queued`, repeated `submitting`, and
`cancelling` generations. Attempt-5 remains rejected implementation evidence
and contributes no current PRE, raw result, Cloud record, or pass count.

Attempt-7 retains the attempt-6 lifecycle closure and fixes its sole rejected
P1: Solver receipt events now have their own exact source-authorized mapping,
hash-pinned Snapshot facts, request/order/duplicate validation, and no effect on
job generation. Solver submission rows are cross-bound to every field recorded
by `SOLVER_JOB_SUBMITTED`; external IDs and failures are bound to the appropriate
lifecycle source. Fields absent from current events have an explicit strict
legacy row contract, while non-empty result refs fail as typed legacy-unbound
rather than being treated as fully sourced. Attempt-6 remains rejected
implementation evidence and contributes no attempt-7 PRE, raw, Cloud result or
pass count.

Attempt-8 retains every prior closure and addresses the two P1 findings from
the rejected attempt-7 review. Solver receipt order is bound to submission and
terminal lifecycle revisions, receipt/job identifiers cannot encode paths,
and immutable file paths use a strict project-relative POSIX contract. The
event head validates and replays the complete versioned envelope and binds its
current domain root to same-transaction business rows. Snapshot, event-head,
immutable-ref, CurrentFacts and CAS-decision identities are explicitly
versioned; unchanged command-envelope/read-set wire contracts retain their
versions. Attempt-7 remains rejected implementation evidence and contributes
no attempt-8 PRE, formal/Cloud raw or pass count.

Attempt-9 retains those closures and addresses the sole P1 from the rejected
attempt-8 review: the event head now consumes a source-compiled closed
WorkflowEvent row policy, not canonical/replay self-consistency as authority.
Every approved raw type selects exactly one payload family and binds its row
Step and attempt to the source catalog, payload, causal subject or result as
declared. Unknown raw/canonical-alias/dynamic families fail closed. Attempt-8
remains rejected implementation evidence and contributes no attempt-9 PRE,
formal/Cloud/archive-only raw or pass count.

## Deliberately absent

M0.3 adds no SQLite table or migration because its contracts and synthetic
complete fixture do not require one. It adds no API/UI, frontend change,
feature flag, production import, writer integration, `_owned_transition`
connection, durable idempotency receipt/outbox, scheduler switch, provider,
model, Solver or real Run4 replay. Frozen Legacy and current production
engine/service/storage files are unchanged. SQLite C/VFS auxiliary I/O is
limited to `<db>-wal`/`<db>-shm` and separately audited; Snapshot application
code still performs no workflow or application-initiated write.

Current schema-v9 snapshots remain partial and cannot pass CAS. Consequently,
the production stale-plan risk is described and modeled but is not yet closed
in the active command path. Phase 2 persistence/migration, Phase 4 writer
centralization/cutover, and Phase 6 cleanup remain deferred and require their
own design authorization.

## Identity roles

Behavior pins participate in shadow CAS. Operational implementation pins bind
the exact classifier source members and exact persisted-owner symbol spans.
The compiler/toolchain identity binds canonical/compiler/Workflow V1+V2 and
generated manifest source at build/evidence time; Python `re`, `fnmatch`,
`pathlib`, `json` and `hashlib` use a separate runtime pin rather than being
misrepresented as repository members. Source locators, conformance corpus and
evidence references are analysis identity only.

## Cutover stop conditions

Stop and return to design review if a frozen production Python file changes,
the historical classifier hash moves, the pure classifier manifest grows
beyond its three files, the owner policy hashes all of `engine.py`, Snapshot
uses a store getter or writes, a legacy partial Snapshot is accepted, or any
table/API/UI/feature flag/cutover/durable receipt appears. The WAL addendum
resolves only that boundary and does not approve the overall M0.3
implementation; M0.3 never authorizes Phase 2/4/6.

For the three-week competition, current v1 remains the sole authority. M0.3
is read-only, non-authoritative and default-off even after review, with no
competition DB, Web API/UI or Scheduler integration and no Phase 2–10,
migration, outbox or cutover work.
