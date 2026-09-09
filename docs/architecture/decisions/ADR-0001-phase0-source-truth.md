# ADR-0001: Phase 0 source truth and v1 characterization

- Status: Accepted as a characterization baseline
- Date: 2026-08-20
- Baseline: `357947948f034325ea6202694c20bf435910d011`
- Scope: documentation, machine-readable characterization, and read-only tests

## Context

Run4 remediation needs a source-verified starting point before any workflow
contract or writer refactor. Architecture review reports were used as design
inputs, but their conclusions were not treated as runtime facts. Every decision
below was checked against the baseline source, schema upgrade code, and existing
tests.

This ADR does not change a runtime module, database, migration, process, model
provider, Solver, browser, or external service. It does not implement M0.1 or
copy, modify, test, or merge the separately owned AR-007 patch.

## Decision 1: the current workflow schema is v9

`factory_core/domain.py` defines `SCHEMA_VERSION = 9`.
`factory_core/storage.py` creates and upgrades `.factory/state.db` against that
constant. The v9 aggregate includes the project snapshot and append-only events,
contest policy, immutable Human Decision requests/instances, Stage checkpoint
current/history, owner-scoped dirty causes/flags/clear receipts, projection
failure/snapshot state, prompt-attempt input receipts, and Solver policy/job/
receipt state.

The prior active `ORCHESTRATION_ENGINE.md` statements “Schema v8” and “database
schema is version 7” contradicted the code. They are corrected to schema-v9.
Other version labels such as `quality_contract` schema v4 are independent
artifact protocols and are not workflow-schema conflicts.

The active hierarchy remains:

```text
8 contest-facing phases
  -> 10 persistent stage_v1 scheduler Stages
    -> Step 0-16 validation/evidence contracts (17 integer Steps)
       plus the non-integer Step 8.5 reviewer-entry artifact contract
```

Stage is the scheduling/checkpoint/retry/recovery boundary. Step remains the
validator, budget, artifact, evidence, and compatibility boundary. The Web phase
is projected from the current subtask's source Step rather than directly from a
Stage number.

## Decision 2: TransitionCoordinator is a target role, not a current monopoly

The normal `FactoryEngine` transition path and normal Solver mutations use
`factory_core.transitions.TransitionCoordinator`. The
`StageExecutionPipeline` returns an outcome and does not write workflow state.
That supports the intended boundary, but it does not prove writer exclusivity.

The source contains current direct Store mutation paths:

| Source | Mutation | Current effect |
|---|---|---|
| `web/backend/selection_service.py` | `record_decision` | compatibility selection writer can create/resolve a request, append a decision, increment revision, and append an event |
| `factory_core/service.py` | `supersede_pending_decision_request` | replaces a stale open request with a revisioned/evented generation |
| `factory_core/steps/prompt_step.py` | `bind_prompt_attempt_input` | binds immutable prompt input and increments revision before dispatch |
| `factory_core/service.py` | projection failure record/resolve | writes diagnostic side-table state |
| `factory_core/consultation_projection.py` | projection failure record/resolve | writes diagnostic side-table state |

Bootstrap/migration code also calls `initialize`, and archive relocation calls
`prepare_for_move`. `TransitionCoordinator` itself records projector failure
diagnostics. These surfaces have different semantics but all refute the blanket
claim that only one application module currently writes the project database.

The `TransitionCoordinator` class docstring says it “alone commits workflow
state”; the call graph above disproves that statement at this baseline. Because
editing runtime modules is outside Phase 0, this ADR and the active architecture
documents carry the correction rather than changing that Python file.

[`../application_writer_allowlist_v1.json`](../application_writer_allowlist_v1.json)
is the executable inventory and future static-gate specification. The Phase 0
test is deliberately only a naming-heuristic characterization gate: it compares
direct `SQLiteStateStore(...)` receivers and variables named `store` or ending
in `_store`. It does not resolve types, assignment aliases, `self`/attribute
receivers, or receivers across functions, so renamed, aliased, or attribute-held
Stores can escape this current scan. The listed baseline callsites were also
checked by grep and source review, but the heuristic does not prove future
completeness. The future gate must be receiver-aware, allow the Store
implementation, retain narrow bootstrap/migration exceptions, exclude the
separate `AuthStore` control plane, and fail CI on any unexplained application
mutation.

## Decision 3: control-plane and project-workflow authority are not substitutes

| Property | `web/auth.db` | project `.factory/state.db` |
|---|---|---|
| Trust owner | Web administrator/control plane | project workflow engine and validated human-decision path |
| Scope | users, registrations, project requests, `project_acl`, `showcase_acl`, delivery overrides, admin audit log | one project aggregate: scheduler, events, decisions, dirty/checkpoint state, Solver state, receipts |
| Access meaning | who may see/control a project or receive a scoped governance exception | what happened in the project and what work/decision is current |
| Lifecycle | installation-level; grants can be changed independently; overrides may expire, be revoked, or be consumed | project-level; revisioned and moves with the project between `ongoing/` and `complete/`; decision history is append-only |
| Must not do | select Step 3, approve content freeze, advance a cursor, or replace a decision receipt | authenticate a user, grant an ACL, grant showcase visibility, or authorize a delivery override |

`delivery_overrides` has two scopes. `continue_after_gate2` is not snapshot
bound; `deliver_snapshot` requires an exact 64-hex snapshot and is consumable.
The provider reads only `web/auth.db` and fails closed. A project-authored
`gate2_delivery_override.json` has no authorization power.

Likewise, schema-v9 `workflow_decision_requests` and
`workflow_decision_instances` bind per-project gate, generation, revision,
subject/options fingerprints, decision identity, and immutable receipt. They do
not grant a Web identity or ACL. Delivery remains a conjunction of the relevant
control-plane grant/override and current project evidence; neither database may
be treated as a replacement for the other.

## Decision 4: v1 behavior is indexed without inventing fixtures

[`../../../tests/fixtures/v1_characterization/index.json`](../../../tests/fixtures/v1_characterization/index.json)
indexes eight required categories: normal, dirty, semantic reopen, Human Gate,
recovery, packet rebuild, technical terminal, and Solver receipt. Each stable
fixture ID resolves to an existing source path and pytest node, records only the
asserted v1 expectation, names the relevant invariants and machine fields, and
lists missing evidence with a `GAP-V1-*` ID.

The cases are descriptors for disposable pytest `tmp_path` projects, not copied
Run4 artifacts. No real Run4 SQLite database is read. In particular, the source
proves a two-stage content-addressed Solver receipt and its SQLite lifecycle,
but the repository has neither a checked-in ten-job receipt corpus nor a literal
machine-readable “10/10” rubric. `V1-SOLVER-10OF10-001` records those gaps rather
than fabricating a ten-of-ten result.

The technical-terminal case characterizes the standalone technical-flow test:
the real Judge is called, the audit record can be `PASS`, delivery remains false,
no final-submission hash is created, and the terminal error is
`PERMANENT_TECHNICAL_FLOW_NO_DELIVERY`. The technical-flow plus no-judge-ablation
combination remains outside this task under the separate AR-007 ownership.

## Test-first evidence

Before adding the allowlist, corpus, or correcting the documents, this command
was run in the clean baseline worktree:

```bash
pytest -q tests/test_phase0_architecture_baseline.py
```

The expected red result was `3 failed in 0.10s`:

1. `ORCHESTRATION_ENGINE.md` had no schema-v9 marker;
2. `application_writer_allowlist_v1.json` did not exist;
3. `tests/fixtures/v1_characterization/index.json` did not exist.

The final test reads `SCHEMA_VERSION` through Python AST, verifies active-doc
schema markers, compares the naming-heuristic AST characterization with the
current writer inventory, and asserts the machine-readable assurance limits
above. It also validates fixture IDs, paths, pytest node names, expectations,
contracts, machine fields, and gap records. It imports no production entrypoint
and creates no workflow database.

## Consequences and follow-up gaps

- Active documents can no longer silently call the workflow schema v7/v8.
- A new or removed bypass-sensitive Store call requires an explicit inventory
  decision.
- The v1 corpus is repeatable and honest about synthetic versus missing proof.
- Writer refactoring, owner/compiler work, schema changes, migrations, outbox,
  snapshots, Scheduler v2, UI changes, model collaboration, Solver changes, and
  delivery changes remain future work.
- The corpus gaps are evidence backlog, not permission to synthesize production
  artifacts or run a real project during Phase 0.
