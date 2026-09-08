# Workflow Contract Bundle v1

`factory_core.workflow_contract` compiles the current workflow constants into an
immutable `WorkflowContractBundle`. This M0.1 artifact is additive: no
authoritative scheduler, worker, solver, provider, API, Web route, database,
migration, or dispatch path imports or calls the compiler.  The disabled,
non-authoritative M0.2 `factory_core.shadow_scheduler` module is its sole
approved consumer and validates the bundle at its receipt boundary.

## Sources and scope

The bundle is compiled, not copied, from these current sources:

- `factory_core.stages.STAGE_CONTRACTS`: ordered Stage 1–10 and their subtasks;
- `factory_core.steps.catalog.STEP_CONTRACTS`: ordered Step 0–16 contracts,
  budgets, implementations, prompts, and model defaults;
- `factory_core.stages.GATE_POLICIES`: the fixed and dynamic Gate producer
  inventory, including consultation preflight/step4/dynamic, Step 3, Step 8.5,
  conditional Step 13, content freeze, delivery-freeze override, and the
  arbitrary Legacy marker family;
- `factory_core.contest.CONTEST_PHASES`: ordered contest phases and Human Gates;
- `factory_core.artifact_ownership.ARTIFACT_OWNERSHIP_REGISTRY`: ordered v1
  first-match owner/classification truth;
- `factory_core.dirty.semantic_flags`: the semantic dirty condition domain.

The bundle schema is `workflow-contract-bundle-v1`. It records the current
workflow state schema as a separate integer field (`workflow_state_schema_version:
9`); these are different version namespaces. Stable Stage, Step, subtask, Gate,
owner, and owner-rule IDs are derived from the source facts. An owner rule ID
also binds its exact pair-scoped priority authorizations.

The Stage catalog has an independent trust root. Validation accepts only the
current `factory-stage-catalog-v1` projection recompiled from
`STAGE_CONTRACTS`, `STEP_CONTRACTS`, and `CONTEST_PHASES`; it does not use the
supplied bundle to define its own expected behavior. The comparison binds
Stage/subtask order and IDs, key, source/checkpoint Step, kind, condition,
contest phase, owner, authority, and inherited Step budget. Every source Step
must resolve uniquely, schedule coordinates and subtask IDs are globally
unique, and subtask keys are unique inside a Stage. Step-backed subtasks bind
the same source/checkpoint Step. The Step 8.5 `reviewer_entry_gate` remains an
explicit source-Step-8, no-checkpoint completion gate; the separately typed
Stage-10 `content_freeze_guard` remains the source-defined Human Gate before
the Step-16 delivery subtask. The sole conditional subtask remains Stage 8
Step 13 `ANY` over the canonical semantic dirty operands; duplicate or unknown
operands fail closed.

The same independent authorization covers every remaining behavior-bearing
subgraph. Validation recompiles the fixed `factory-step-catalog-v2` Step
projection (including order, IDs, implementation, phase, owner, authority,
budgets, and default models), the complete Gate and ContestPhase projections,
the ordered owner rules and pair-scoped priority authorizations, and the dirty
classifier identity from current source facts. Explicit Stage↔Step phase,
owner, and budget checks plus bidirectional ContestPhase↔Step coverage checks
prevent two individually plausible projections from disagreeing. The unique
Stage 8 Step 13 condition operands must equal the source-derived classifier
semantic flags exactly. `prompt`, Gate `producer`, owner diagnostics, and the
owner-compiler implementation schema remain analysis-only and are deliberately
excluded from behavior authorization.

## Canonical bytes and hash

`factory_core.canonical` defines `factory-canonical-json-utf8-v1`:

- mapping keys must be strings and are sorted by Unicode code point;
- dataclass field names become mapping keys and Enum members use their values;
- `None`, booleans, integers, and strings become JSON null, booleans, integers,
  and strings;
- strings retain their exact Unicode scalar sequence and are encoded as UTF-8;
- lists and tuples retain order because their order is semantic;
- sets and frozensets become arrays sorted by each element's canonical bytes;
- floats, non-string mapping keys, invalid Unicode, bytes, and unsupported
  objects are rejected;
- JSON is emitted without insignificant whitespace. No Unicode normalization or
  locale-dependent conversion is performed.

`workflow_contract_bytes()` returns the canonical **semantic projection** and
`workflow_contract_sha256()` returns its lowercase SHA-256 hex. The projection
retains behavior-bearing order, budgets, conditions, dispatch kinds, owner
rules, the current owner resolution mode, and exact priority authorizations. It
excludes owner-analysis witnesses and diagnostic text, the owner-compiler
implementation schema, Step prompt source locations, and Gate producer source
locations. Changing only those review/implementation fields therefore cannot
fabricate workflow-contract drift. Bundle validation rejects an owner
resolution mode unsupported by the current resolver and rejects supplied
semantic behavior even when the supplied bundle has a self-consistent new
hash. A semantic hash identifies serialized behavior; it does not authorize a
new behavior under a fixed catalog version.

`workflow_contract_analysis_bytes()` and
`workflow_contract_analysis_sha256()` retain the complete immutable compiled
bundle, including diagnostics and source locators, for reproducible review.
Both identities are pure and recomputable. Their golden identities are in
`tests/fixtures/workflow_contract_v1/bundle_identity.json`; mapping insertion
order is irrelevant, while Stage/Step/list reordering changes the hash.

Before reading any supplied field or computing either identity, the public
validator checks the exact runtime DTO graph. Every contract node must be its
exact frozen dataclass type (subclasses are rejected), every immutable sequence
must be a tuple, integer fields reject booleans, and primitive/optional string
fields must have their declared type and strict UTF-8 representation. A wrong
nested value therefore raises `WorkflowContractValidationError` with its
stable field path at the validator, readiness adapter, scheduler core, and
receipt boundaries; raw attribute, iteration, or canonicalization errors do
not escape. This structural check does not authorize behavior and does not
compare analysis-only values to source constants: valid prompt/producer,
diagnostic, and compiler-schema changes retain their documented identity split.

## Owner compatibility compiler

`factory_core.owner_compiler` compiles every registry rule in source order and
resolves all matching rules while preserving the production v1 first match as
the compatibility result. Production and compiler matching share
`artifact_pattern_matches()` from `factory_core.artifact_ownership`; there is no
second fnmatch/globstar implementation.

Diagnostics are explicit:

- `NO_OWNER`: no rule matches a requested path;
- `OVERLAP`: multiple matching rules resolve to the same owner;
- `MULTIPLE_MATCH`: different owners match without an exact authorization;
- `SHADOWED` / `UNREACHABLE`: an earlier rule covers a later rule;
- `INTENTIONAL_PRIORITY`: one exact winner-pattern/owner to
  loser-pattern/owner pair has nonempty rationale and issue ID;
- `UNANALYZABLE`: the conservative static analyzer cannot prove the relation.

Authorization is never winner-wide. A resolution with one authorized loser and
one unauthorized loser retains the authorized pair evidence and also emits
`MULTIPLE_MATCH`; strict validation rejects it. Compatibility compilation
currently reports 29 `INTENTIONAL_PRIORITY`, 115 unapproved
`MULTIPLE_MATCH`, 60 same-owner `OVERLAP`, and the known same-owner
`paper/**/*.tex` over `paper/*.tex` `SHADOWED`/`UNREACHABLE` redundancy.
These are language-level compatibility diagnostics; they do not alter v1
first-match selection. Strict compilation rejects the multiple-owner
intersections and known redundancy until a separately authorized registry
migration resolves them.

Static overlap for supported literal/`*`/`?` patterns is not sampled. The
compiler intersects both finite glob automata for every shared globstar variant
and carries a five-state normalized-path shape. The search visits each product
state once, with an explicit upper bound of
`5 × (len(pattern_a)+1) × (len(pattern_b)+1)` per variant pair; the returned
shortest/lexicographic witness is then checked by the shared production matcher.
Character classes, backslash pattern literals, and normalization-sensitive
empty/slash/dot components are explicitly `UNANALYZABLE`, never silently
treated as disjoint.

The Gate inventory distinguishes exact names from families. Native `dynamic`
consultation has no fabricated Stage/Step: its binding is the active Stage with
a Stage-1 fallback. The frozen Legacy adapter accepts any non-whitespace value
from `.awaiting_consultation` via `GATE:([^\s]+)`; the bundle records this as
`gate-family:legacy_dynamic`, with no Stage/Step and an explicit
`UNANALYZABLE` compatibility diagnostic. A conservative AST inventory test
scans every Native `PendingAction` producer plus the Legacy adapter. Default
producers belong to this frozen bundle; separately enabled native extensions
must declare their own versioned inventory and participate in the same exact
producer-parity check. The opt-in joint-modeling extension declares
`joint-modeling-gates-v1` in `factory_core.joint_modeling.JOINT_GATE_POLICIES`:
candidate consultation owns Stage 2 / Step 3 and risk consultation owns Stage 4 /
Step 5. These policies also drive their PendingAction metadata. They do not
rewrite the frozen M0.2/M0.3 bundle or grant those shadow contracts joint-modeling
authority; unknown native producers and unregistered joint gates still fail.

## Purity and later integration

Construction, validation, semantic/analysis canonical serialization, hashing,
compilation, and resolution read only arguments and imported immutable
constants. They do not
read or write files or databases, inspect time/environment/random state, open a
network connection, spawn a process, or dispatch work.

M0.2 consumes the bundle only through the pure, returned-receipt boundary
documented in `SHADOW_SCHEDULER_M02.md`; it adds no persistence or production
integration. M0.3 may expose a persisted identity through API/UI only after a
separate review. Later integrations must consume this bundle rather than
recreate Stage, Step, Gate, owner, or classifier tables. Before M0.3
persistence/cutover, the existing dirty-classifier source hash must still be
split into separately named semantic and implementation identities; this P1
hard gate remains open.
