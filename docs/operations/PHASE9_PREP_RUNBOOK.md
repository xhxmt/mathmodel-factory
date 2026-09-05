# Phase9-A preparation runbook

Status: this runbook defines the **fixed-offline implementation and immutable
package-evidence process**. Exact candidate identity and outcomes belong to the
generated package's manifest, command records, raw logs and derived summary;
the package remains a candidate until a separate independent audit passes.
Production remains `BLOCKED`; A2_0016 through A2_0020 are `NOT APPLIED` in
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
or cutover configuration. Every safely addressable attempt gets a unique
append-only command record and full raw log; a later success never overwrites a
failed attempt. The recorder becomes safe only after it has proved that the
audit root is a canonical ordinary directory, the canonical id-derived record
and log paths are unused children with safe parents, and the initial candidate
source inventory is complete. A failure before that boundary is explicitly
classified `NON_RECORDABLE_INVOCATION_VALIDATION` and creates no artifact: an
untrusted/aliased/missing root or unproved source cannot safely name or populate
an evidence record. After that boundary, a suite-kind, Python/runtime/dependency,
reporter/source-copy/environment/pipe/sandbox preparation failure returns 125
and writes a separate preflight-failure record plus canonical raw event and
source inventory. That record says `process_started=false` and does not
fabricate dependency, pytest, composite-stage or outcome evidence.
Summary/package verification keeps the attempt visible, while it cannot satisfy
a required final suite.

Committed run-generation and forensic-replay retries have a separate recovery
boundary. The service first validates only the lookup coordinate and then,
while holding the shared Authority commit lease, copies a stable database
main/WAL image into a private directory for a query-only lookup. It returns a
stored result only after reconstructing the canonical request, unique
idempotency binding, authorization/nonce consumption, generation succession,
receipts, terminal/event graph, source inventory and every referenced typed
business object. An exact recovery does not consult current authorization or
freshness, consume a nonce, write a row, or create WAL/SHM beside the source
database. The retained `replayed` compatibility field is part of the immutable
wire result: the committing call and every exact recovery both return
`replayed=false`, so callers cannot infer the service path from result bytes. A
key is reserved globally across workflows, and the services rebuild canonical
requests from creation receipts/replay business rows to retain that reservation
even if the composite-key idempotency row is missing or moved. A true miss
releases no safety gate: the service performs all live checks and repeats both
lookup and validation under `BEGIN IMMEDIATE` before a new commit. Unsafe
sidecars, aliases, incomplete graphs and same-key/different-request or
same-key/different-workflow lookups fail closed rather than falling through to
a new execution.

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

`full_repository` is an ordered composite suite, not a pytest-only alias. Its
machine-readable `composite_stages` entries bind each stage's symbolic argv,
cwd, source targets and exact npm-script name/body. The runner, summary builder
and portable package verifier all require that projection to equal the single
trusted definition; a missing stage/target or command/script drift fails closed.
In both source and fresh it runs the same three targets: all Python tests, a
production frontend build, then the documented `npm run test:phase6` Chromium
tests. The command must name canonical absolute Python, Node, npm, `node_modules`
and browser-runtime coordinates. Both dependency trees are mounted read-only
and recursively inventoried before and after execution; the browser executable
is also byte- and version-bound. The npm CLI entrypoint is resolved and hashed,
and the recorded Node executable explicitly invokes that exact CLI for both
frontend stages; merely probing an unrelated Node is not sufficient. The
production build uses Vite's explicit `--configLoader runner`, bypassing the
default bundled-config path that tries to materialize `.vite-temp` below the
read-only dependency mount. Its
outDir and every temporary browser build live below the invocation's writable
basetemp/TMPDIR, never in the read-only candidate. Missing locked development
dependencies or Chromium,
skipped/todo/cancelled/malformed browser TAP, a stage-record gap, or any nonzero
stage makes the suite non-passing. Even when npm exits zero, the child runner
turns an incomplete browser TAP result into stage exit 88; the parent verifier
independently repeats the semantic check. The runner additionally requires the candidate-bound exact
`vite build` and documented two-module `test:phase6` scripts. A zero-exit build
must produce a safe nonempty `index.html` plus `assets/` tree; its complete
path/byte/hash inventory is revalidated and compared exactly across source and
fresh. The requirement map must make the runner and `package.json` reachable as
implementations and both exact browser modules reachable as tests from the
requirements citing `full_repository`.

`tools/build_phase9_test_summary.py` reparses the raw logs, enforces the exact
suite/environment set and required targets/stages, checks every file reference
and source/dependency/browser inventory, accounts for all outcome categories and
warnings, and requires exact source/fresh outcomes. For `full_repository`, it
independently reconstructs every stage's argv/cwd/environment/exit/log slice,
uses only the Python slice for pytest statistics, validates the Node browser
summary plus its ordered test-node/outcome inventory, and compares the
normalized composite contract. Passed counts are
claimed only when the corresponding complete raw log, command record, trusted
pytest stream, and composite stream exist; unbound aggregate counts are not
evidence.

The package builder accepts only that reproducible summary and an honestly
`BLOCKED` production status. It reads candidate files from immutable Git
objects, excludes untracked/runtime/credential/database/archive content and
produces one-root, fixed-time, normalized-mode manifest/checksum closure.

## Formal entry prerequisites

### Isolated Step13 preparation and component execution

`scripts/phase9_runtime.py prepare --project-copy <absolute-copy> --records
<new-absolute-directory>` runs the real objective-evidence/packet builders and
preflight without model calls. `review` runs the applicable existing JudgeStep
path; `--mode NORMAL_STEP13` is math-only and `--mode FORENSIC_THREE_ROLE` runs
the prepared three-role review. The default requested model/effort is
`gpt-6-astra` / `medium`, without model fallback. Explicit per-call and total
timeouts bound the existing role and infrastructure retry budgets.

The records retain source bytes/identity, packet fingerprints, each transport
attempt, raw output/log bytes, requested configuration, and terminal status.
An independent provider response model identity is `unavailable` when the CLI
does not expose it. These records are `ISOLATED_COMPONENT_RUN`, not formal
Authority receipts; `COMPONENT_PASS` and `PREPARED` do not establish Phase9-A
completion. Step14–16 and delivery are never invoked by this entry point.

### Authorized runtime coordination (candidate implementation)

`scripts/phase9_authorized_runtime.py` is default-off through `PHASE9_ENABLED`.
It uses the existing Phase9 database/source/input configuration. Its commands
are deliberately separate from the normal Step13 math precheck:

1. `plan --request <entry-coordinate-request> --records <new-directory>` rebuilds
   objective evidence and all complete role packets with zero provider calls.
   The durable runtime requires `authority-phase9-forensic-replay-request-v3`;
   existing v2 finalizer evidence keeps its original coordinate semantics.
   It writes the exact dispatch target and packet-v2 bytes. Generation creation
   remains the existing `scripts/phase9_run_generation.py` operator workflow.
2. The authorized operator supplies a private (0600), account-owned
   `authority-phase9-dispatch-grant-v1` for that target. The scope permits only
   role provider calls and the fixed local process-scope probes. Entry and
   finalizer-start grants do not grant those capabilities. The coordinator does
   not issue this external authorization or migration/production approval.
3. `execute --request <request> --entry <READY-entry> --target <target>
   --grant <grant> --records <new-directory>` consumes the distinct grant in
   A2_0020 before dispatch. Each attempt first commits its command, invocation,
   attempt and scope, then records the actual OS launch and completion. Role
   The target also binds the resolved native Codex ELF bytes (the JS launcher
   is bypassed), resolution chain, configuration-file hashes and routing
   environment hash. Approval must verify these exact provider identities;
   a program merely named codex is not independently certified by its name.
   Actual argv/cwd and kernel executable/command-line observations bind each
   intent and launch. Credentials are never printed; response model identity
   remains unavailable unless independently visible. Role execution uses the repository's two-attempt transport / three-round
   infrastructure retry budget, a pinned model/effort and an overall deadline.
   Providers require a private PID namespace and read-only host source, inputs
   and Authority state; only the current output files and a private child of
   the explicit `TMPDIR` are writable. There is no unsandboxed
   fallback. Failed/kill/pause probes use fixed local processes, not models.
4. `collect --runtime-id <id>` reads the immutable lifecycle. Unobserved or
   uncertain attempts prohibit automatic redispatch; nonzero/timeout results
   after launch are UNCERTAIN because local process closure cannot prove
   remote cancellation. Only proven pre-launch failures may be retried; they remain active in
   entry-state collection. An execution `COMPLETED` means role/process work
   finished, not a forensic PASS or permission for Steps14–16.
5. After execution, obtain a fresh READY entry and completion request at the
   same candidate, generation, input and Authority-state coordinate. The stable
   runtime target excludes entry acquisition timestamps and the later receipt
   serialization timestamp; it retains all execution/input identities. The
   original grant has a 300-second acquisition TTL and a separate bounded run
   deadline. It is never renewed by editing a timestamp. `export --runtime-id
   <id> --request <fresh-request> --entry <fresh-entry> --records <execution-records>`
   joins actual output bytes and OS observations and calls
   `record_formal_phase9_runtime_receipt` for roles and process scopes. It
   uses the native judge's persisted accepted output (including final-response
   fallback), and validates packet/verdict/snapshot controls before receipt writes.
6. `finalize --request <fresh-request> --records <new-scratch-directory>` runs
   the fixed real acceptance probes, obtains the independent one-use finalizer
   start capability through the existing evidence producer, and invokes the
   forensic finalizer. Its terminal still requires the independent formal
   review prescribed below. No migration, production outbox or delivery path
   is invoked by this CLI.

A2_0020 is additive and production schema version 8. It retains published
A2_0010–A2_0019 SQL bytes and upgrades the runtime-record guard to accept the
new verified dispatch graph as well as the legacy completion graph. A
receipt-record capability has its own short TTL immediately before recording;
it is not the authority under which a long provider call executes. Interrupted
export artifacts and failed attempts are preserved. Repeating export compares
existing bytes, bindings and runtime records exactly, then writes only missing
stages. A fresh real entry/request is required after its acquisition window
expires. V3 receipt coordinates exclude that acquisition metadata, while the
full request and entry remain separately verified. If old entry control files
already exist, use a new evidence root, preserving the original root. This
never re-dispatches a provider. Any differing existing bytes or rows block
recovery; no deletion or timestamp editing is a recovery mechanism. This implementation requires current-candidate tests
and independent review; fixture processes and their synthetic provider text
are never formal runtime evidence.

After the new candidate ZIP passes an independent audit, formal entry still
requires all of the following real material:

1. exact reviewed commit/tree/parent and full source-byte inventory;
2. a real Authority database, verified backup, stable migration owner and
   approved ordered A2_0016-A2_0020 application journal;
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
