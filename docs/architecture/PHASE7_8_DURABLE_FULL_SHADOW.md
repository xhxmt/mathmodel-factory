# Phase 7+8 Durable Full-Shadow Runtime

Status: implemented local full-shadow candidate, pending complete release
verification and separate deployment approval. This contract does not
authorize production cutover, provider dispatch, upload, or transfer of
workflow authority.

## Selected normal-flow contract

Phase 7B and Phase 8B run as one explicitly enabled, synchronous local
sidecar. A trusted local operator first prepares the exact Phase 7/PDF/Phase 8
facts and a durable approval preflight. The ordinary subject then references
only that preflight hash while submitting work through CLI or the
project-ACL-protected Web API. The service records the request, an explicit
local worker executes one claim, and Web can read the effective result.

The sidecar has a durable request/claim/lease/result ledger, but no background
thread and no hidden worker. Restart reconstructs pending and leased work from
the private spool and Phase 4 operation store. This is a synchronous local
sidecar contract, not a production outbox or apparently-asynchronous queue.

Every result carries `authoritative=false`, `authority_transferred=false`,
`dispatch_performed=false`, `provider_call_performed=false`, and
`outbox_dispatch_performed=false`. An `AUTHORIZED` Phase 8 pure decision is a
shadow fact only; it cannot call a provider or production outbox.

## Trust and operator preflight

`factory_core.phase78_operator` is the only operator preparation entry. It is
not exported through the ordinary `factory_core.cli phase78` surface, service,
Scheduler, worker, or Web router. It verifies the live Phase 3/6 heads,
persists the exact three-role Phase 7 result, materializes and re-reads the PDF
and CAS package, and writes an immutable Phase 8 trusted-preflight receipt.
The later worker must load that exact hash and recheck every bound field before
it can issue a shadow approval. A caller-supplied approval object or a Phase 6
`snapshot:view` proof can never mint or replace this receipt.

`PHASE78_TRUSTED_OPERATOR_ID` and
`PHASE78_TRUSTED_OPERATOR_GENERATION` are deployment labels, not credentials,
signatures, secrets, or proof of trust. Trust comes from restricting the local
command and its environment to the controlled OS account and from keeping all
store/CAS/spool/scratch parents private (`0700`) and persistent files private
(`0600`, with CAS immutable blobs made read-only after publication). An
untrusted caller must not be able to execute under that OS credential or
rewrite its environment. The issuer must be the configured operator identity
and must differ from the Phase 6 subject; the subject and generation must equal
the exact Phase 6 grant and the authenticated CLI/Web caller.

The preflight binds issuer, subject, both generations, the complete Authority
generation set, logical issue/not-before/expiry times, approval predecessor
CAS, Phase 3/6/7 heads, reference package/record, policy, staged-manifest hash,
and the exact artifact list. Operator preparation and ordinary work therefore
form two different trust steps even when they run on one host.

## Upstream currentness

Every Phase 7/8 commit binds and revalidates all of these facts:

- the complete canonical `AuthorityPhase3ArtifactState`, including workflow,
  through-revision, sorted occurrences, and aggregate state hash;
- one exact revision-level `ArtifactLedgerOccurrence`, including command,
  mutation, normalized path, semantic identity, and record/blocker/removal;
- the complete Phase 6 current access proof, including its Authority/source
  coordinate, snapshot, grant, lifecycle, evaluation, and proof hashes; and
- for Phase 8, the Phase 7 durable result, receipt, effective verdict, and
  current head.

The Authority revision and workflow, Phase 3 aggregate state hash, selected
occurrence, and Phase 6 source binding must agree. The selected occurrence may
have been created at an earlier revision than the aggregate head only when it
is still the unique exact current occurrence for its path at that head. It is
never accepted merely because a later artifact has the same semantic ID. The
stores recheck captured Phase 3/6/7 and work-generation heads immediately
before publishing a current pointer and again when loading current state. Thus
an A→B→A semantic cycle, a newer Authority or Phase 6 head, a replaced Phase
7 result, or a superseded worker generation retains history but makes the old
current result unavailable or denied.

Phase 7/8 rejects `legacy_unknown` project, run, runtime, or scheduler
generations. A legacy Authority database must first complete the normal
production migration that records concrete generations; operators must not
edit imported rows or synthesize generation labels merely to make the sidecar
eligible.

## Phase 7 exact-byte grounding

`Phase7GroundingStore` persists the exact bytes and lengths of the `math`,
`execution`, and `paper` role output, manifest, and context packets. It
recomputes each path-free grounding report, the three role receipts, the
aggregate policy identity, and the effective verdict. Whitespace and terminal
newlines are identity-bearing; quote bytes are never stripped.

The immutable receipt, effective verdict, current projection, and idempotency
result commit in one SQLite transaction. Same key and same bytes replay;
different bytes conflict. A missing or invalid role, a non-record occurrence,
or grounding failure writes a new `INDETERMINATE` current result instead of
leaving an earlier `PASS` current. Replay does not depend on the original
absolute source directory.

## Phase 8 PDF, CAS, approval, and decision

During trusted preparation, the reference materializer reads one stable
regular PDF under the configured project root and verifies its Phase 3
occurrence digest and byte length. It records the exact raw PDF, page PNGs,
extracted page text, chunks, canonical reference package, and materialization
receipt in the private CAS. The receipt binds the parser/render/extraction
implementation, version, and options. Missing, corrupt, encrypted, or textless
input returns a stable path-free structured unavailable result rather than an
internal exception response.

CAS publication is immutable and fsynced before the Phase 8 SQLite
transaction. Every blob is read back and rehashed before the binding receipt,
current pointer, and idempotency result commit. A crash may leave an unreachable
immutable blob, but current state cannot point to an unverified blob. Restart
revalidates every digest and length. The ordinary worker replays persisted CAS
components; moving or deleting the original PDF after a successful preflight
does not change its identity or make replay depend on a newly installed parser.

The local Poppler commands (`pdfinfo`, `pdftoppm`, and `pdftotext`) are required
and version-bound in the persisted package. Pillow is optional: when present
its exact version is recorded; otherwise the bounded built-in
`stdlib-png-ihdr-v1` validator records that implementation instead. Bootstrap
reports the selected PNG validator and does not pretend an optional decoder is
a missing required dependency.

The durable approval is derived only from the exact trusted preflight and
binds issuer and subject identities and generations, logical
issue/not-before/expiry times, scope, policy hash, reference package, staged
manifest, and the exact artifact list. Issue, successor, revoke, and expiry
events are append-only and idempotent. A Phase 6 read proof is never treated as
egress authority. Only a current trusted local approval can yield a shadow
`AUTHORIZED` decision; revoke, expiry, successor, policy drift, or any upstream
head drift makes the effective current view `DENIED` while preserving the
historical receipt and decision. Status reads use service-owned current time,
not a request-supplied evaluation time, so expiry remains effective after
restart.

## Work, deadlines, and recovery

The local work ledger uses a private `0700` spool, `0600` canonical jobs, and
an independent Phase 4 durable operation store. Submit, claim, lease reclaim,
local-worker checkpoint, complete, fail, cancel, and uncertain reconciliation
are durable. Same-key concurrent submission converges to one logical job;
same-key different bytes conflict. There is no scheduler enqueue without a
durable job and no process that starts automatically.

Cancellation first publishes an immutable private receipt that binds the exact
job, operation, claim generation, owner, epoch, nonce, request key, classified
reason, and logical time. Public CLI/Web views expose only its stable reason,
time, and receipt hash. Restart replay cannot rewrite the original reason, and
only a durable `SUCCEEDED` work state may project a Phase 8 decision as the
effective pipeline outcome; cancelled, failed, pending, or active work cannot
be made successful by a historical `AUTHORIZED` decision.

One `TotalDeadline` instance spans current-head reads, SQLite busy waits, file
read, PDF tools, CAS, Phase 7, Phase 8, and the single deterministic replay of
already-committed work; no adapter resets the budget. Deadline, user
cancellation, shutdown, and superseded generation have distinct stable codes.
A timeout after an inner commit reports an uncertain outcome; the caller
reuses the same idempotency key to query or replay instead of generating a
second result. The exact claim generation, owner, epoch and nonce plus current
Phase 3/6/7 heads are fenced before every Phase 7/8 publication. A cancelled or
superseded worker that returns late may leave immutable history or an orphan
CAS blob, but cannot publish current or complete the newer work generation.

## Entry points and default-off isolation

`PHASE78_ENABLED` is false by default. Disabled CLI, Web, service, Scheduler,
and worker paths return before request-file reads, path validation, database or
CAS access, heavy Phase 7/8 imports, threads, or process creation. Existing
CLI help, Web routes, Scheduler, service, and Solver worker behavior remain
unchanged.

When explicitly enabled, all of these absolute paths are required:

- `PHASE78_AUTHORITY_DB_FILE`
- `PHASE78_PHASE6_DB_FILE`
- `PHASE78_PHASE7_DB_FILE`
- `PHASE78_PHASE8_DB_FILE`
- `PHASE78_WORK_DB_FILE`
- `PHASE78_WORK_SPOOL`
- `PHASE78_PROJECT_ROOT`
- `PHASE78_CAS_ROOT`
- `PHASE78_SCRATCH_ROOT`

`PHASE78_AUTHORITY_SOURCE_FENCE_SHA256` is also required. Optional bounded
settings are `PHASE78_DEADLINE_MS` (default 30000, range 1–300000) and
`PHASE78_LEASE_SECONDS` (default 30, range 1–86400).

The operator-only preparation additionally requires the protected deployment
labels `PHASE78_TRUSTED_OPERATOR_ID` and
`PHASE78_TRUSTED_OPERATOR_GENERATION`. They must come from the controlled
process environment and must not be accepted from request JSON.

The trusted preparation surface is deliberately separate:

```text
python3 -m factory_core.phase78_operator --operator OPERATOR prepare PROJECT REQUEST.json
```

It returns a `trusted_preflight_sha256`; the ordinary request must reference
that exact hash. Preparation does not enqueue or dispatch work.

The ordinary CLI surface is:

```text
python3 -m factory_core.cli phase78 [--actor ID] submit PROJECT REQUEST.json
python3 -m factory_core.cli phase78 [--actor ID] run-one PROJECT REQUEST.json
python3 -m factory_core.cli phase78 [--actor ID] status PROJECT IDEMPOTENCY_KEY
python3 -m factory_core.cli phase78 [--actor ID] cancel PROJECT CANCEL.json
python3 -m factory_core.cli phase78 [--actor ID] revoke PROJECT APPROVAL REQUEST.json
```

Enabled Web routes are project-authenticated and ACL-first. Authentication and
project ACL run before feature/config checks, request-body parsing, core import,
or resource access. Web exposes submit, explicit `run-one`, status read,
user-cancel, and issuer-only revoke adapters; it does not expose operator
preflight and adds no
Phase 7/8 frontend bundle. Unknown internal errors are logged by type and
returned as one sanitized public error.

Turning `PHASE78_ENABLED` back off and restarting the process removes the Web
router and returns CLI/service/Scheduler/worker before resource access. Rollback
preserves all private stores, CAS objects, spool entries and historical
receipts for audit; it never deletes or rewrites them.

## Verification contracts

The existing `phase46-bootstrap-exact-count-v1` contract remains unchanged at
`100/274/146/29/108 = 657`, with every non-pass category zero. Phase 7+8 uses
the separate versioned `bootstrap_phase78.sh` contract with exact unit,
runtime, adapters, PDF/CAS, and enabled-E2E groups. Its JUnit and independent
outcome ledgers are run-ID and contract-hash bound; missing, extra, duplicate,
stale, truncated, skipped, xfailed, xpassed, or count-drifted results fail
closed.

The release gate is `./bootstrap_phase78.sh`; its current exact group/file/count
contract is printed by
`python3 -m scripts.phase78_test_contract describe`. Exact Phase 7/8 counts are
owned by that versioned machine-readable contract, not copied into prose. The
final candidate inventory and deterministic builder must exclude SQLite
databases and WAL/SHM/journal files, CAS temporary objects, absolute paths,
caches, dependencies, and audit output before file content is read. Source and
fresh no-`.git` extraction must both run the complete repository and both
bootstrap contracts.

## Known non-blocking boundary

The inherited same-credential namespace race between a final quarantine
identity check and unlink is not a normal user or operator flow. This slice
does not widen the Phase 4/5 cleanup architecture for that adversarial timing;
private directory permissions and existing regression coverage remain the
documented information-level boundary. The same-credential assumption also
means that an attacker already able to execute as the trusted operator OS
account is outside this local trust model; the two operator environment labels
do not mitigate such compromise.
