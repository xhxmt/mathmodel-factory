# Phase 6 Verified Project Snapshot and Scoped-Grant Full Shadow

Status: current Phase 4-6 candidate contract, implemented and locally verified;
independent Pro review remains pending. It is non-authoritative,
default-disabled and read-only at the Web boundary. It does not authorize a
production cutover.

Phase 6 has two deliberately separate slices:

- **Phase 6A** is the frozen, pure seven-state projection in
  `web/frontend/src/lib/projectSnapshotUi.js`. It accepts an in-memory value,
  produces a deeply frozen view model and performs no I/O.
- **Phase 6B** is the verified durable full-shadow implementation in
  `factory_core/phase6_snapshot_grants.py`, with the ACL-first read adapter in
  `web/backend/phase6_api.py` and the default-off frontend integration. It
  persists evidence only in an independent caller-selected SQLite file.

Neither slice is a production workflow writer. Snapshot, grant, lifecycle,
evaluation, access-proof, runner and Web results fix `authoritative=false`,
`authority_transferred=false` and `dispatch_performed=false`. There is no
provider, process, Solver, outbox, Scheduler, workflow mutation,
grant-to-Web-ACL conversion or dispatch surface. The v1 Dashboard and current
Authority paths remain the default.

## Phase 6A: frozen projection contract

`buildProjectSnapshotViewModel()` preserves seven distinct states:

| State | Projection |
| --- | --- |
| `loading` | Status only |
| `ready` | One coordinate-bound set of sections and Action Center |
| `empty` | Status only; never reports a clear Action Center |
| `legacy_unavailable` | Status only |
| `auth_error` | Status only |
| `api_error` | Status only |
| `unknown` | Status only and fail-closed |

Only a valid `ready` input may expose business sections. Its
`snapshot_id`/`revision`, every section and every action must resolve to one
identical coordinate. Invalid or mixed coordinates, invalid section identity,
uncloneable content and unsafe future states suppress all business sections and
actions. The builder clones caller data before recursively freezing it.

Action IDs are stripped and must be non-blank and unique. A repeated normalized
ID fails the entire ready value closed as `unknown`, regardless of input order
or whether the duplicate payloads are equal; neither boundary silently chooses
a winner. A valid unique set is sorted by `expired`, `critical`, `warning`,
`guarded`, then `info`, followed by ID. Only a valid ready snapshot with zero
actions has `mode=clear`. The projection does not execute an action.

## Phase 6B: verified durable full shadow

### Source and snapshot identity

`build_authority_source_binding()` validates an exact
`authority-workflow-coordinate-v1`, its canonical SHA-256, a complete Authority
revision snapshot, the matching `snapshot-coordinate-v0`, and explicit
Phase 3 artifact, Phase 4 operation and Phase 5 supervisor state hashes.
Project, revision, contract pin and all generation coordinates must agree.

A `PARTIAL` source may be recorded for diagnosis but is ineligible to issue a
grant. A grant requires a `COMPLETE` source and a bound contract-pin set.
Snapshots are immutable canonical facts with a SHA-256 identity, monotonically
increasing sequence/revision rules and an explicit predecessor. The current
head advances with compare-and-swap semantics in the same transaction as its
fact and idempotency receipt. Same-key/different-bytes and same-revision/
different-bytes fail closed.

The source binding is constructed from caller-supplied immutable evidence. It
does not add a production Authority reader method and does not independently
query or mutate the Authority database.

### Scoped grants and lifecycle

The closed grant vocabulary is:

- `snapshot:view` for the complete verified snapshot;
- `section:view` for exactly one non-blank section key;
- `action-center:view` for the snapshot's Action Center projection.

Each grant binds the exact snapshot and Authority coordinate, workflow,
project, subject type/ID/generation, scope/key, issuer identity/generation and
issuer receipt. A grant can be issued only against the current eligible
snapshot and cannot outlive it. Grant chains use an explicit predecessor and
generation-safe current projection.

Lifecycle receipts are immutable and append-only. Revocation is irreversible;
expiry is materialized once at the exclusive logical-time boundary. Evaluation
checks subject, generation, exact scope, snapshot head, snapshot validity,
grant validity and current lifecycle. An allowed result is still only
`ALLOWED_SHADOW`; its access proof cannot transfer authority or dispatch. All
logical times are caller values—the domain does not consult wall time.

### Store and ownership boundary

The Phase 6 store requires an absolute path whose parent already exists, is a
real non-symlink directory and is available through `/proc/self/fd`. A new
database is exclusively created with `O_EXCL`, `O_NOFOLLOW`, `O_CLOEXEC` and
mode `0600`. Existing stores must be single-link regular files with exact mode,
header, ownership binding, schema inventory, canonical rows and no WAL/SHM/
journal sidecar before any read-write connection is opened.

Creator, cleanup and connection descriptors use the shared explicit
ownership-transfer protocol. Parent and database inodes stay anchored through
verification and transactions. `BaseException`, descriptor exhaustion,
connection/commit interruption and cleanup faults retain the original failure,
close each descriptor at most once and clean only the uncommitted inode after a
descriptor-bound quarantine check. A completed commit is retained for restart
verification.

The standalone runner is also default-off. With `enabled=false` it returns
before constructing or inspecting a path, opening SQLite or reading any source.

## Authenticated Web read path

The endpoint is:

```text
GET /api/projects/{base_name}/phase6-snapshot?expected_revision=<revision>
```

Its fixed order is authentication, the existing project ACL, backend feature
gate, absolute-path resolution, lazy Phase 6 core import, verified store read,
then response normalization. Therefore an unauthenticated or unauthorized
caller cannot use the Phase 6 gate, database path or store failures as a project
existence oracle. The endpoint is registered while disabled, but returns the
generic disabled 404 only after ACL and never imports the Phase 6 core or opens
the database.

`expected_revision` is deliberately received as an untrusted raw query value
and parsed only inside the ACL-approved endpoint body. An authenticated caller
without project access therefore receives the same project denial for a
missing, negative or non-integer revision. An authorized caller receives the
bounded parameter error for an invalid value and a stale 409 only for a valid
nonnegative revision. FastAPI query validation cannot run ahead of this order.

Web access is granted exclusively by the existing `web/auth.db` project ACL.
Phase 6 scoped grants are shadow evidence and do not grant Web access. The Web
adapter returns only the current verified snapshot's section metadata/content
hashes; it does not issue, revoke or evaluate grants and currently supplies an
empty action list. Loader or validation errors are mapped to a closed allowlist
of stable public codes and messages. Unknown backend codes, adapter exceptions,
transport failures and local errors all collapse to one generic unavailable
response; paths, SQL, credentials, stack text and raw exception messages are
never UI content. Internal diagnostics use a controlled redacted logger only.

Before producing a ready `phase6-project-snapshot-web-v1` response, the backend
validates the internal project/workflow/source-binding/Authority coordinate.
The bounded public projection retains project, snapshot SHA-256, server
revision, sections and actions on one coordinate and repeats all three false
safety bits; it does not expose the full Authority/source record. Stale reads
return `409 PHASE6_SNAPSHOT_STALE` with the current revision. Missing or
ineligible state maps to the unavailable view; tamper or inconsistent state
fails closed rather than being repaired.

## Dual activation and rollback

Activation intentionally requires two independent flags:

| Plane | Setting | Accepted enabled value | Default |
| --- | --- | --- | --- |
| Backend | `PHASE6_SNAPSHOT_ENABLED` | case-insensitive `1`, `true`, `yes` or `on` | `false` |
| Frontend build | `VITE_PHASE6_FULL_SHADOW_ENABLED` | the exact string `true` | disabled |
| Frontend request budget | `VITE_PHASE6_SNAPSHOT_DEADLINE_MS` | integer `1`–`300000` milliseconds | `15000` |

When the frontend flag is off, the tab is absent and the Phase 6 component is
excluded from the default production bundle. When the backend flag is off, the
store module is not imported and no database path is touched. Both flags and a
pre-provisioned verified standalone database are required for an end-to-end
view. Setting either flag false preserves the corresponding v1 surface; a full
rollback sets both false, rebuilds the frontend and restarts the backend. The
standalone database is left intact for audit/recovery and is never deleted by
rollback.

The bundle gate is verified against real Vite production manifests, emitted
chunks and browser resource requests—not source-text matching alone. The
default build contains no Phase 6 module, route, chunk, endpoint string or
network request; the enabled build retains the lazy panel and its complete
verified read flow. Mounted-browser checks cover `activeElement`, disabled
actions, roving Arrow/Home/End focus, Tab boundaries, Enter/Space activation,
ARIA busy/live/alert state and safe retry. One wall-clock deadline covers
response headers, response-body parsing and the single permitted stale retry;
it is not reset between attempts. An AbortController and deadline Promise race
settle even a custom transport that ignores the signal. Current timeout maps to
the safe unavailable state, user/reset/navigation/superseded cancellation
settles loading without overwriting another generation, and late old work
cannot replace the newest request.
The workspace imports only the generic
`virtual:optional-workspace-snapshot` build interface. Vite resolves that ID
to a path-free, loader-free disabled module unless the exact frontend flag is
enabled; only the enabled implementation contains the Phase 6 tab key and
dynamic component import. A runtime-only `v-if` is not an accepted isolation
boundary.

Detailed local and production procedures are maintained in `web/README.md`,
`web/QUICKSTART.md` and `web/docs/deployment/DEPLOYMENT.md`.

## D001-D018 requirement-to-implementation map

| ID | Candidate acceptance requirement | Implementation and reproducible test owner |
| --- | --- | --- |
| D001 | Canonical verified snapshot identity | `AuthoritySourceBinding`, `VerifiedShadowSnapshot`; `tests/test_phase6_snapshot_grants.py` source/snapshot identity cases |
| D002 | Exact Authority/source coordinate agreement | `build_authority_source_binding()`; coordinate crossover and integer-domain cases |
| D003 | Bind Phase 3, 4 and 5 state identities | Source binding's three required SHA-256 fields; exact-binding case |
| D004 | Independent durable store and exact integrity profile | `Phase6SnapshotGrantStore`; fresh/reopen, foreign-store, schema-mode and tamper cases |
| D005 | Atomic snapshot fact/current/receipt CAS | `append_snapshot()`; chain, conflict, fault-stage and post-commit recovery cases |
| D006 | Exact idempotency replay and cross-domain conflict | Phase 6 idempotency ledger; replay and conflict cases |
| D007 | Concurrency, restart and recovery | immediate transactions and immutable reconstruction; concurrent snapshot/grant/revoke-expire and reopen cases |
| D008 | Closed subject/generation/scope grant | `ShadowScopedGrant`, `GrantScope`, `issue_grant()`; subject/scope and lineage cases |
| D009 | Durable revoke/expiry/successor lifecycle | `GrantLifecycleReceipt`, `revoke_grant()` and successor grant chain; expiry/revoke/reopen cases |
| D010 | Current-only evaluation and non-authoritative proof | `evaluate_grant()`, `GrantEvaluationReceipt`, `ShadowAccessProof`; stale/expired/revoked evaluation cases |
| D011 | Default-off before all side effects | `run_phase6_snapshot_grants_shadow()` and backend/front gates; default-off/import-isolation cases |
| D012 | Descriptor-safe creation and transactions | shared `OwnedDescriptor` protocol; EMFILE/ENFILE, `KeyboardInterrupt`/`SystemExit`, cleanup, leak and double-close cases |
| D013 | Authentication and project-ACL-first endpoint, including invalid raw revision values | `web/backend/phase6_api.py`; real HTTP auth/ACL/revision ordering cases |
| D014 | Seven distinct deeply frozen UI states | `projectSnapshotUi.js`; seven-state, clone/freeze and fail-closed cases |
| D015 | One revision-atomic Web coordinate | backend normalizer and `phase6SnapshotProjection.js`; mixed section/action/revision/project cases |
| D016 | Strict dual default-off integration and v1 rollback | backend setting, exact frontend gate, Vite manifests/chunks and real browser resource-load tests |
| D017 | One total request deadline, bounded stale recovery and accessible interaction | request coordinator deadline/abort/generation fence plus fake-clock headers/body/retry tests and mounted-browser timeout, ARIA, focus, Tab, roving arrows/Home/End and Enter/Space tests |
| D018 | Current documentation and self-contained audit evidence | this contract, current Web runbook and candidate audit-package manifest/log/reproduction checks |

The table describes the implemented candidate and its verification owners; it
is not a claim that an earlier historical archive completed Phase 6 or that the
candidate has been deployed.
