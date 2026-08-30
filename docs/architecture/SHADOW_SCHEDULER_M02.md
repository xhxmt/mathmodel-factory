# M0.2 Stage-v1 shadow scheduler

Status: implemented as a disabled, non-authoritative, read-only M0.2 slice.

This document describes `factory_core/shadow_scheduler.py`.  The production
writer remains `FactoryEngine` plus `TransitionCoordinator` and
`SQLiteStateStore`.  No production module imports the shadow module, and the
frozen Legacy adapter does not import it.  M0.2 adds no database table,
migration, runtime flag read, provider call, or cutover behavior.

## Boundary

`StageV1ReadinessAdapter` accepts one caller-supplied
`stage-v1-recorded-read-snapshot-v1` mapping.  The mapping must explicitly
contain the project revision, run/runtime/scheduler generations, workflow and
atomic Stage cursor, checkpoint heads, dirty refs, pending actions, active
invocations, terminal/delivery capability, domain readiness, immutable refs,
and availability/gap facts.  Missing facts are errors; an unavailable required
fact produces a fail-closed `WAIT` plan rather than a guessed default.

The adapter never accepts a store or project path.  It does not scan artifacts,
open SQLite, read the clock or random source, invoke a model/Solver/provider,
or dispatch a process.  The supplied workflow bundle is passed through
`validate_workflow_contract_bundle()` and the fixed M0.2 condition validator
before any readiness value, plan, or receipt is constructed.  The only
non-`ALWAYS` Stage subtask must be Stage 8
`conditional_math_preflight`/Step 13, with operator `ANY` and operands exactly
equal to the classifier semantic dirty flags in canonical order; all other
subtasks must be `ALWAYS` with no operands.  The adapter derives its ordered
catalog from that trusted bundle and rejects cursor/checkpoint coordinates
outside it.

The workflow-bundle validator is the shared behavior trust boundary for
adapter, core, and receipt construction. It requires the supported Stage and
Step catalog versions and recompiles Stage, Step, Gate, ContestPhase,
classifier, and owner-rule behavior from source constants. Source/checkpoint
mappings, phases, owners, inherited budgets, conditions, ordering, IDs,
per-Stage keys, `(stage_id, key, source_step_id)` coordinates, Gate bindings,
phase membership, classifier schemas/domains, owner patterns, and exact
priority authorizations are checked before a readiness value exists. Step 13
condition operands equal the verified classifier semantic flags exactly, so a
supplied classifier cannot remove `MATH_DIRTY` from the planning domain.
Immediately before constructing an active `step:N` route, the core also asserts
that `N` belongs to the already validated Step ID set. Analysis-only Step
prompt locators, Gate producers, owner diagnostics, and owner-compiler
implementation schema remain independently visible without becoming behavior.

That same boundary first validates the exact immutable DTO/container graph.
Lists are not accepted where the contract requires tuples, subclasses cannot
carry hidden mutable state, and wrong nested DTO, primitive, optional-string,
integer/boolean, enum, or UTF-8 values fail with a stable field-path
`WorkflowContractValidationError`. The validator, adapter, core, and receipt
constructor consequently expose one exception family before readiness,
planning, or parity construction; Python attribute/iteration failures and
canonicalizer errors are not part of the public contract.

Behavior-bearing string values are exact, case-sensitive contract values rather
than open text.  Workflow status is derived from `WorkflowStatus`; dirty flags
are derived from `DirtyFlag`; active invocation type/state pairs, domain state,
fact availability, and delivery capability have explicit M0.2 vocabularies.
The adapter rejects unsupported, empty, case-changed, or whitespace-changed
values before constructing `ReadinessInput`.  `SchedulerCore.plan(bundle,
readiness_input)` independently validates the bundle and re-derives the exact
catalog instead of treating the DTO catalog as a trust root.  Its shared
adapter/core validator rechecks schema and contract identities, runtime and
scheduler generations, cursor/attempt/atomic-step links, checkpoint
coordinate/step/revision/hash/uniqueness, dirty owners, all behavior target
coordinates, immutable runtime types/ranges, and terminal/domain consistency.
Checkpoint receipt and immutable-ref digests must be 64 lowercase hexadecimal
SHA-256 values.  A technical terminal remains separate from contest delivery
permission; only an explicitly completed, delivery-eligible terminal may
declare `delivery_allowed=true`.

Workflow status also has an exhaustive planning disposition.  Only `ready`,
`running`, and `retrying` may enter ordinary catalog scheduling.  The
selection/consultation waits, `paused`, `failed`, `archiving`, and
`interrupted` produce `WAIT` when no higher-priority recorded fact applies;
`completed` and `killed` require explicit terminal facts.  Unavailable,
terminal, pending-action, active-invocation, and blocking or explicit domain
facts retain precedence, so a recorded recovery/domain action is not replaced
by generic status handling.

All returned DTOs are frozen dataclasses containing only frozen dataclasses,
tuples, enums, integers, booleans, strings, or `None`.  Lists and mappings in
the input are defensively copied into sorted tuples, so later caller mutation
does not alter an existing receipt.

## Pure planning

`SchedulerCore.plan(WorkflowContractBundle, ReadinessInput)` returns one immutable
`SchedulerDecision` containing:

- the exact validated readiness input;
- `stage-v1-readiness-result-v1`;
- `shadow-transition-plan-v1`.

The only plan actions are `STAY`, `ADVANCE`, `WAIT`, `DISPATCH`, `REOPEN`, and
`TERMINATE`.  M0.2 emits `WAIT` for unavailable required facts, pending actions,
and recorded active invocations; respects explicit recorded domain facts such
as semantic reopen; selects an active cursor or the first uncompleted catalog
entry; uses the bound skip route when conditional Step 13 has no semantic dirty
ref; and terminates on an explicit recorded terminal or complete checkpoint
set.  Both `proposed_mutations` and `performed_side_effects` are always empty,
and `authoritative` is always false.

This is a shadow description of a next action.  It does not call
`FactoryEngine._select_stage_task()`, because that v1 method performs live
stale-receipt checks, filesystem manifest capture, and an authoritative cursor
transition.

## Identity layers

Every readiness input/result, transition plan, and parity receipt contains the
M0.1 identities in separate fields:

- workflow semantic: 55,178 bytes,
  `2e2f3b9cb48788db5e7a28d0f0343518c3d1cd5969a57e045ef2fc946df5455d`;
- workflow analysis: 137,810 bytes,
  `a9d7aa2a134075ba53b9f2c09b94c8182d209a683639609a27caa4c1e535c92b`.

Each M0.2 value has an analysis/full canonical identity.  Readiness and plan
also expose a semantic identity.  The semantic projection includes all
behavior fields and any immutable ref explicitly marked `behavior_binding`,
but excludes analysis-only evidence digests, gap prose/IDs, checkpoint receipt
digests, domain evidence refs, and the workflow analysis identity.  Therefore
diagnostic/source-evidence changes remain visible in the full identity without
fabricating a scheduling change.

Canonical encoding remains `factory-canonical-json-utf8-v1`.  Values contain no
timestamp, UUID, absolute temporary path, unordered mutable collection, or
Python hash-seed input.

## Parity receipt

`stage-v1-shadow-parity-receipt-v2` permits only:

- `MATCH`;
- `EXPECTED_CORRECTION`;
- `UNEXPLAINED_DIFFERENCE`;
- `V2_ERROR`;
- `V1_UNREPRESENTABLE`.

The independently serialized parity value is
`stage-v1-shadow-parity-value-v2`; it binds action, coordinate, and execution
route.  Thus Step 13 execute (`step:13`) and skip
(`stage-subtask:conditional_math_preflight_skip`) can no longer compare as a
match merely because their action and target agree.  An expected correction
must exactly bind its issue ID, fixture ID, both route-bearing values, and
nonempty evidence refs.  A different fixture, action, coordinate, or route
cannot use the correction.  Before comparison, receipt construction
revalidates the embedded input, recomputes readiness and plan links/hashes,
requires `authoritative=false` and empty mutation/side-effect tuples, and
recomputes the deterministic trusted-bundle decision.  Unexplained differences
remain explicit and fail the normal parity gate.

The Phase0 characterization corpus did not record a complete next-action
snapshot for six of eight cases.  M0.2 does not invent one: those cases retain
their checked-in gap IDs and produce `V1_UNREPRESENTABLE`.  The normal terminal
and semantic-reopen cases produce `MATCH`.  Supplemental shadow fixtures cover
normal progression, current checkpoint heads, Human Gate pending, both Step 13
routes, recovery, Solver-current, technical non-delivery, and completion.  The
machine-readable matrix is
`tests/fixtures/scheduler_shadow_v1/parity_cases.json`.

## Cutover boundary

M0.2 does not resolve the classifier semantic-versus-implementation identity
split identified by the M0.1 review.  That remains a hard prerequisite before
M0.3 persistence exposure or any later cutover.  No M0.3 work is part of this
slice.
