# Phase 2-8 Joint Shadow Integration Acceptance

Status: accepted direct-test-only composition. Phase 8 received final Pro PASS
with no required fixes before this joint acceptance was implemented. This
document does not approve production cutover; current v1 remains the sole
production authority and route.

Phase 3 was subsequently expanded into the packaged, default-disabled full
shadow foundation documented in
`docs/architecture/PHASE3_ARTIFACT_REGISTRATION_SHADOW.md`. The acceptance flow
below intentionally retains its historical registration-only compatibility
adapter; its Phase 3 gap row records the acceptance-time baseline, not the
current foundation scope. Neither document authorizes production cutover.

## Purpose and boundary

`tests/test_phase2_8_shadow_integration.py` runs one new, complete, synthetic
modeling task twice and compares the complete acceptance identity. It proves
that the selected Phase 2-8 slices can exchange real identities and hashes; it
does not place eight unrelated fixtures in one test.

The only adapter is
`shadow_contracts/phase2_8_integration.py`. Phase 3 intentionally records owner
identity without artifact bytes or an authority coordinate, so the adapter
binds those three already-validated facts into a frozen
`phase2-8-shadow-artifact-binding-v1`. It has no I/O, runtime state, dispatch,
or authority. `shadow_contracts` remains excluded from production packaging.

## Reproducible data flow

1. A new temporary legacy-v9 SQLite fixture is migrated by the real Phase-2
   additive migration. `AuthorityRepository(write_shadow=True)` atomically
   records one command/event/receipt/outbox bundle and idempotently replays it.
   A read-only connection verifies the recorded workflow revision and envelope
   hashes.
2. The Phase-2 event and receipt in that atomic bundle bind the exact SHA-256 of
   one in-memory synthetic `results/canonical_results.json`; the
   `SHADOW_ADVANCE` command retains its source-policy normal `NoPayloadV1` form.
   Phase 3 compiles the current owner registry, registers that path, and supplies
   its registration/owner hashes to the thin adapter together with the Phase-2
   coordinate and artifact byte hash.
3. Phase 4 uses the resulting artifact-binding hash as the durable operation
   payload and the Phase-2 outbox message ID as its command identity. The chain
   records claim, dispatch checkpoint, active, reconciliation-required,
   recovered-active, and success receipts, all bound to one operation identity.
4. While active, Phase 5 evaluates ordinary `pause` for a `durable-solver`; the
   result is `continue`. It performs no signal or cancellation.
5. Phase 6 receives the same authority revision and a snapshot ID derived from
   the coordinate, artifact, operation receipt, and pause decision. Its frozen
   ready projection propagates that coordinate through every section and the
   Action Center. The JSON projection hash becomes downstream evidence.
6. Phase 7 writes only synthetic temporary role/context/manifest files. The
   exact quote contains the Phase-3/2 artifact-binding hash, while the context
   also binds the Phase-6 projection and Phase-2 authority receipt. Direct
   grounding and manifest-enabled three-role aggregation both PASS. A
   path-independent grounding/aggregate identity is compiled from the receipts.
7. Phase 8 compiles a two-page `reference-document-record-v1` from in-memory
   materialized facts. Its page text binds the authority, artifact, and aggregate
   hashes; JSON round-trip and record hash are reverified. External-share
   classification remains a fact with `authority_granted=false`.
8. Selected authority, artifact-registration, grounding, and reference-record
   hashes form a canonical `STAGED` data-egress manifest. Missing approval stays
   `DENIED/APPROVAL_MISSING`; an exact synthetic receipt returns `AUTHORIZED`.
   Both serialized decisions reverify and both keep
   `dispatch_performed=false`.

The final `phase2-8-shadow-integration-acceptance-v1` SHA-256 binds every stage
above and excludes temporary paths. Two independent runs in one test must
produce identical summaries and acceptance hashes; the validation command is
also repeated under multiple Python hash seeds.

## Acceptance conditions

- one project/workflow/subject, imported project/run generations, and committed
  revision are propagated throughout the chain;
- every downstream identity is computed from upstream stable bytes or hashes;
- authority persistence and replay, recovery receipts, UI JSON, grounding
  receipts, reference record, and both egress decisions are rechecked;
- no real PDF or artifact is opened, rendered, OCRed, uploaded, or dispatched;
- no model, Solver, provider, browser, network, production database, production
  approval identity, or real external service is used;
- production frontend/backend/CLI/Scheduler/aggregate routes gain no import or
  caller, and fresh `factory_core.cli` does not load the harness or Phase-8
  modules.

## Simplified-to-full gap matrix

The classifications below govern future scope; this acceptance implements none
of these gaps.

| Phase | Must complete before switching v1 | May enhance after cutover | Rejected over-design |
| --- | --- | --- | --- |
| 2 authority persistence | Production migration/rollback plan; verified source fence on real databases; unique application-writer boundary; supported read repository; transactional outbox delivery/recovery; operational backup and cutover evidence | richer migration observability and operator reports | inferred generations/owners from clocks, PIDs, neighboring rows, or file state; dual silent authorities |
| 3 artifact registration | revision-bound persistent registry; typed change/reopen decisions; checkpoint re-attestation; explicit migration when owner policy changes; Scheduler writer ownership | richer lineage queries and operator history | re-resolving and rewriting historical owner records; mtime/path-presence as authority |
| 4 durable operation | durable claim/lease store; transactional launch intent; idempotent outbox consumer; process-scope receipts; crash reconciliation; exclusion of the old launcher; Linux crash-window tests | more worker/provider kinds and operational dashboards | process launch or signal side effects inside the pure transition function; PID heuristics as identity |
| 5 pause policy | Execution Supervisor; authoritative owned-scope inventory; real signal/cancel receipts; durable solver cancellation reconciliation; platform behavior and cutover tests | configurable escalation timing and richer operator UX | kill-all/unowned process behavior; mixing process discovery or signaling into the policy matrix |
| 6 snapshot UI | authenticated revision-atomic endpoint; ACL/error mapping; production frontend integration; stale-revision transport; rollback and accessibility tests | presentation polish and additional read-only sections | collapsing auth/API/legacy errors into empty/ready; client-invented snapshot revisions |
| 7 grounding/aggregate | approved delivery-hard-gate ownership; durable effective verdict/receipt state; role retry and failure routing; release/outbox/current-pointer integration with rollback | grounding diagnostics, reviewer navigation, and analytics | fuzzy quote matching, silent evidence repair, or treating malformed output as PASS |
| 8 reference evidence | trusted PDF/PNG/text materializer; CAS byte verification; durable reference record/package ownership; provenance migration and failure routing | richer bibliography/OCR formats and reference search | validator path reads, implicit OCR/render, fuzzy hashes, or classification as sharing authority |
| 8 data egress | authenticated durable approval/revocation ledger; destination/account policy; secret scanning; idempotent dispatch outbox; provider receipts, reconciliation, audit retention, and API ownership | additional providers, policy UX, and reporting | treating `AUTHORIZED` as proof of dispatch; hidden uploads; approval inferred from classification |
| Joint Phase 2-8 | one authoritative transactional coordinate across persistence, artifact, operation, UI, judgment, reference, and egress; end-to-end failure routing, restart, rollback, and production isolation tests | cross-phase observability and acceptance dashboards | shipping this test harness as a production orchestrator or running it as a second authority beside v1 |

## Known limitations

The Phase-2 fixture necessarily retains imported `legacy_unknown` project/run
generation values because the approved additive migration never invents missing
legacy generations. The joint chain faithfully propagates those exact values
instead of upgrading them. Phase-7 aggregation writes grounding receipts only
inside the test temporary directory through its already-existing optional
manifest path. No receipt is durable outside the test, and no shadow result can
advance or authorize the current production workflow.
