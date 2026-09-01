# Phase9-PREP Runbook

Status: PREP only. This runbook does not authorize formal Phase 9 execution,
run-generation creation, provider calls, outbox dispatch, migration, delivery,
release, or production cutover.

## Outcome and phase names

The source architecture defines D.10 as Phase 9 and D.11 as Phase 10. This
runbook adds the following disambiguating aliases:

- **Phase9-A — Run4 Forensic Replay**: a new generation that starts at the
  Step 13 packet-v2 rebuild and proves closure of known downstream failures.
- **Phase10-B — Fresh Clean-room Acceptance**: a new project and generation
  that execute the complete Stage/Step workflow without override or reuse.

Older planning material called the clean-room run “Phase 9”. That numbering is
obsolete. This repository uses `Phase9-A` and `Phase10-B` so that evidence from
the two runs cannot be mixed.

The Phase 7+8 candidate at frozen commit
`fb58241077ce6874bdfe2df6c23322d431930d38` remains default-off,
non-authoritative, no-provider, no-production-outbox, and no-dispatch. Its Pro
review is still pending. Therefore only the preparation work in this document
is permitted in parallel with that review.

## What this preparation establishes

The preparation branch is isolated from both the main checkout and the frozen
Phase 7+8 worktree:

| Item | Frozen value |
| --- | --- |
| Prep worktree | `/home/tfisher/.codex/worktrees/phase9_prep_20260831/paper_factory` |
| Prep branch | `codex/phase9-prep-20260831` |
| Frozen review-candidate base commit | `fb58241077ce6874bdfe2df6c23322d431930d38` |
| Frozen review-candidate base tree | `b1e2e1d0f1232f4ad7ce19f074709db5525d3f3f` |
| Proposed generation | `run4-forensic-proposed-20260831-01` |
| Generation state | `NOT_CREATED` |
| Run mode | `FORENSIC_REPLAY` |
| Delivery capability | `DISABLED` |
| Initial target | `STEP13_PACKET_REBUILD` |

`scripts/phase9_prep_manifest.py` validates the checked-in preparation
manifest and the isolated Git worktree. A valid report is named
`PREP_MANIFEST_VALID`, never `PASS` or `PHASE9_AUTHORIZED`.

The validator is deliberately limited to:

- one bounded, strict UTF-8 JSON regular file with no duplicate or extra keys;
- exact safety literals, all capabilities false, and every runtime check still
  marked `DEFERRED`;
- the exact base-to-HEAD changed-path allowlist, ordinary and ignored untracked
  inventories, index special flags, and the frozen uninitialized gitlink;
- normalized, non-overlapping planned runtime paths outside all source and
  audit roots;
- a fixed set of read-only Git identity, ancestry, branch, and cleanliness
  commands with optional locks, system/global configuration, external diff,
  text conversion, pager, stdin, and unbounded output disabled; and
- one deterministic JSON report on stdout.

It does not import `factory_core`, open SQLite, inspect production databases,
create planned directories, read official inputs, start a worker or runtime
process, contact a network, call a model or Solver, create a generation, or
write a report file. It does start the fixed local Git commands listed above.
It also fails when `PHASE78_ENABLED` is true.

Run it only from the isolated worktree after the prep commit is clean:

```bash
/usr/bin/python3 -I -S -B \
  /home/tfisher/.codex/worktrees/phase9_prep_20260831/paper_factory/scripts/phase9_prep_manifest.py \
  /home/tfisher/.codex/worktrees/phase9_prep_20260831/paper_factory/docs/operations/PHASE9_PREP_MANIFEST.template.json \
  --repo /home/tfisher/.codex/worktrees/phase9_prep_20260831/paper_factory
```

Expected status:

```text
PREP_MANIFEST_VALID
formal_phase9_authorized=false
manifest_declared_run_generation_state=NOT_CREATED
validator_created_run_generation=false
next_gate=PRO_NORMAL_FLOW_PASS_AND_FORMAL_READ_ONLY_STATE_GATE
```

`-I -S -B` prevents `PYTHONPATH`, user/site customization, and bytecode writes
from running before this script. The JSON output is byte-stable for the same
manifest and Git state. Redirecting
stdout is an operator action outside this tool; the tool has no output/apply/run
option.

The manifest reader rechecks descriptor and path identity before returning;
the Git coordinate, index, dirty inventories, attributes, and gitlink state are
also reread before the report is emitted. No same-credential filesystem reader
can make an adversarial concurrent rename/write race globally atomic. The
controlled OS account remains part of the preparation trust boundary.

## Formal Phase9-A entry gate

`PREP_MANIFEST_VALID` is only evidence that the preparation boundary is sound.
Formal Phase9-A must remain blocked until a separate gate proves every item
below against immutable evidence:

1. The independent Pro review returns `NORMAL_FLOW_PASS` for the exact reviewed
   commit/tree. If the audit requires a fix, freeze and review the replacement
   commit/tree before continuing.
2. Phase 4–8 P0 exits are closed. This includes AR-007 delivery bypass, the
   Human Decision single-writer boundary, packet zero-dispatch/effective-verdict
   routing, command/read-set CAS, worker outbox and process-tree receipts,
   owner/checkpoint re-attestation, revision-atomic snapshots, generation/mode/
   delivery pins, and official-input/execution-context receipts.
3. Source, contract-pin set, official-input manifest and bytes, all generation
   identities, and the execution context are frozen and hash-bound.
4. Read-only state checks show zero active processes, zero pending outbox items,
   and zero unresolved migrations. Project, run, runtime, and scheduler
   generations are concrete; `legacy_unknown` is forbidden.
5. Use only the reviewed `Phase9RunGenerationService` / narrow
   `AuthorityOperations.create_or_rotate_run_generation` boundary to create or
   rotate a `run_generation`. It derives both initial project and run generation
   identities from canonical content, reads and rechecks the exact official
   input bytes and execution-context receipt, validates the controlled OS
   account, and commits predecessor/receipt/idempotency/current-pointer facts in
   one transaction. Direct SQL updates and invented generation labels remain
   forbidden. The API's existence is not authorization to call it against a
   non-test database.
6. Old generation history is read-only. The new generation fixes
   `modeling_consultation_contract=LEGACY_NOT_APPLICABLE`,
   `delivery_capability=DISABLED`, and begins at the Step 13 packet rebuild.

The formal state collector may reuse strict read-only Authority and snapshot
components, but must not use `factory_core.cli diagnostics` for forensic state:
that path can open the project database read-write, enable WAL, and upgrade the
schema. `factory_core.phase78_operator prepare` is also excluded because it
writes Phase 7/8 stores and CAS objects.

The reviewed collector and gate are `factory_core.phase9_entry` and
`scripts/phase9_entry_gate.py`. They open only explicit databases read-only,
hold one query-only transaction while joining the workflow/current generation/
creation receipt/migration/delivery/outbox/process facts, and require all nine
candidate-bound P0 receipts including AR-007. A `READY` gate result proves only
that those entry prerequisites are current; its authorization scope explicitly
keeps Phase9-A, provider/network, production outbox/delivery, release,
deployment, migration, and cutover false. Missing official input, execution
context, controlled-account authorization, runtime database, or receipt
produces `BLOCKED` and must not be filled from a template or test fixture.

## Phase9-A minimum acceptance matrix

The formal replay must bind each case to a test result and immutable receipt.

| Area | Required cases | Required result |
| --- | --- | --- |
| Forensic replay | `AC-RUN4-001`, `AC-RUN4-002` | New generation reaches a typed terminal; first rebuild uses three new role generations and no inherited delivery/Pro data. |
| Packet | `AC-PACKET-001`, `002`, `003` | Missing claims dispatch zero calls; complete packet hash is exact; later reruns follow dependency fingerprints only. |
| Verdict | `AC-VERDICT-001`, `003` | Raw, protocol, grounding, and effective layers coexist; contradictory aggregate data fails closed. |
| Snapshot | `AC-SNAP-001`, `002` | Every section has one revision coordinate; read failures are `ERROR`, not an invented empty state. |
| Outbox | `AC-OUT-001`, `002`, `004` | Pre-commit crash launches nothing; committed work reclaims once; uncertain dispatch is reconciled without automatic resend. |
| Supervisor | `AC-SUP-001`, `002`, `004` | Failed/pause/kill paths retain process-scope receipts and leave no live descendant. |
| Delivery fence | `AC-DEL-001`, `AC-DEL-002` | Technical and `ABLATE_NO_JUDGE` forensic modes keep delivery disabled and create no release or final-acceptance side effect. No-judge emits the typed nonzero `PERMANENT_ABLATION_NO_DELIVERY` marker/result, writes no `final_submission.sha256` or final-acceptance receipt, is never reusable, and takes precedence over an exact-snapshot delivery override; workflow-state and release readers reject it. This closes AR-007. |
| Entry/terminal inventory | formal state gate | Active process, pending outbox, and unresolved migration counts are zero at both boundaries. |

Exit additionally requires:

- an explicit `terminal_reason`, requested resume target, effective
  verdict, delivery-disabled decision, and one revision-atomic snapshot
  coordinate;
- parity classified only as `MATCH` or an issue/fixture-bound
  `EXPECTED_CORRECTION`, with zero unknown or unexplained differences; and
- packet, role, process, transaction, and restart fault injection that is
  recoverable without stale-plan replay or duplicate external calls.

This can prove closure of known downstream Run4 failures. It cannot prove the
modeling-collaboration layer, scientific quality, production delivery, or
clean-room operation.

## Evidence skeleton

`docs/operations/PHASE9_FORENSIC_EVIDENCE.template.json` is a non-receipt
skeleton. It intentionally says `NOT_COLLECTED`, `NOT_CREATED`, and `NOT_RUN`.
After formal authorization, copy it to the isolated external evidence root and
populate it only from immutable receipts. Do not edit the checked-in template
to simulate a pass.

The skeleton reserves fields for:

- source/tree/fingerprint, contract pins, official-input manifest and execution
  context;
- project/workflow/generation coordinates and entry/terminal quiescence;
- atomic run-generation creation, old-generation read-only, process-tree,
  outbox-reconciliation, and delivery-fence receipts;
- entry and terminal state-inventory receipts binding the active-process,
  pending-outbox, and unresolved-migration counts at each boundary;
- packet generation/hash, required-claim coverage, preflight and dispatch
  count;
- math/execution/paper role generations, exact raw byte hashes and lengths,
  dependency fingerprints, protocol/grounding/effective verdicts, and call
  receipts;
- aggregate policy, terminal reason, resume target, snapshot coordinate and
  delivery fence;
- per-case test-result and immutable-receipt hashes for the minimum acceptance
  set, fault-injection receipts, and explained parity.

Raw official inputs, databases, CAS, logs, provider output, credentials, and a
full audit ZIP are runtime evidence, not repository source. They must stay out
of this branch and out of the slim Pro review package.

## Forbidden actions during Pro review

- Do not create or direct-update a run generation.
- Do not open a workflow/Authority database with a read-write adapter.
- Do not create the planned database, CAS, spool, log, evidence, or project-copy
  directories.
- Do not run Step 13, a worker, Solver, model, provider, migration, outbox
  consumer, release publisher, archive move, or production cutover.
- Do not change, clean, reset, stage, commit, or otherwise repurpose the frozen
  Phase 7+8 worktree and its untracked audit evidence.
- Do not reuse old role output, consultation/Pro history, delivery decision,
  grant, or receipt in a new generation.
- Do not upload the full audit candidate to Pro; the existing slim review
  package remains the only Pro input.

## Phase10-B boundary

Fresh clean-room acceptance begins only after Phase9-A passes and the clean-room
P1 set is closed. It requires a clean commit/worktree, pinned dependencies and
external-service configuration, a new project and generation, verified frozen
official inputs, and a real no-override run through all 10 Stages, Step 0–16,
Step 8.5, the modeling-collaboration layer, Supervisor, Solver receipts, Human
Gates, packet, effective verdict, delivery decision, and immutable atomic
release. Exit must show no orphan process, wrong owner, mixed revision, stale
response, or old-generation contamination. Enabling the production flag still
requires a separate product-owner signature after the clean-room evidence is
complete.

Phase10-B is not started by this branch or by a `PREP_MANIFEST_VALID` report.

## Rollback

No runtime state is created by this preparation, so rollback is simply to stop
using the local prep branch. Preserve the branch, worktree, reports, and frozen
Phase 7+8 evidence until the user explicitly approves removal. No automated
cleanup is part of this runbook.
