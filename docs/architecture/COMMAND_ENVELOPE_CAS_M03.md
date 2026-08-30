# CommandEnvelope and Read-Set CAS M0.3

Status: pure, non-authoritative shadow validation. It does not call
`_owned_transition`, commit a command, write a receipt/outbox, retry work, or
authorize production execution.

## Closed wire values

`CommandEnvelopeV1` binds a registered command type, command ID, explicit
project/revision/generation and runtime/scheduler/run generation, actor,
explicit entity/subject sum types, explicit payload/no-payload value, complete
structured read set, and `ContractPinSetV1`. Empty strings, zero, `None` or
missing fields cannot stand in for `NoEntityScopeV1`, `BoundEntityScopeV1`,
`NoSubjectScopeV1` or `BoundSubjectScopeV1`.

All public values require exact dataclass/tuple/scalar types; subclasses,
`bool` as integer, unregistered exact-type Enum forgeries, uninitialized DTOs,
non-canonical ordering, duplicates, invalid UTF-8 and non-lowercase SHA-256 are
rejected as `CommandEnvelopeValidationError`. Serializers validate the complete
runtime structure before canonicalization and translate canonicalization
failures to the domain error.

`ReadSetV1.entries` is the full sorted unique `(fact_type, fact_key)` sequence.
Its hash is recomputed from entries. `CurrentFactsV1` carries explicitly
recorded current values and availability; the validator never fetches them.
`PAGED`, `REDACTED`, `ERROR` or unavailable required facts fail closed.

Each `FactRequirementV1` also carries a source-authorized scope binding:
`NONE`, `ENTITY`, `SUBJECT`, or `ENTITY_AND_SUBJECT`. Before rebuilding the
read set, CAS compares every entity-bound fact generation with the bound
entity scope and every subject-bound fingerprint with the bound subject
scope. A coherently rehashed forged read set therefore returns the dedicated
`ENTITY_GENERATION_MISMATCH` or `SUBJECT_FINGERPRINT_MISMATCH` code rather
than being hidden inside a generic read-set mismatch.

## Source-authorized policy and decision

`CommandScopePolicyV1` is rebuilt from a fixed command policy projection. It
defines command support, exact entity/subject scope, required fact keys,
payload schema, and whether dirty-owner facts require both persisted-owner
policy pins. A supplied policy or pin self-hash is not authority.

`current_facts_from_snapshot()` uses one fixed source mapping, never a caller
mapping. `compile_snapshot_fact_projection_v1()` rebuilds the six rules and
`validate_snapshot_fact_projection_v1()` rejects even a unique, coherent
mapping drift: project state, event head, active Stage cursor, active pending
action, Solver-receipt dirty owner and bound Solver job project to their six
explicit CAS fact keys. Duplicate or missing available source facts fail at
projection.
The bridge validates the recoverable Snapshot pins against the trusted
Workflow V2 bundle, derives entity/subject sum types only from the mapped
facts, preserves typed unavailability, and then runs `validate_current_facts`.
Four synthetic COMPLETE routes reach shadow acceptance; the real legacy
schema-v9 Snapshot still fails before acceptance because its generations and
recorded pins are unavailable.

The strengthened event/Snapshot trust root is carried explicitly by
`command-current-facts-v3` and `command-cas-decision-v3`. The fixed event-head
projection consumes `events-source-authorized-v3`; this changes a
Snapshot-derived EVENT_HEAD value and therefore its real proposal read set.
`command-envelope-v1` and `command-read-set-v1` retain their versions because
their intrinsic wire validation did not change. Old CurrentFacts/CAS decision
schema strings are rejected rather than silently reinterpreted.

CAS recomputes the actual payload hash, rebuilds the current read set, compares
project/revision/generations/scopes, and compares semantic, operational and
runtime pins. Persisted-owner pins are compared only for a source policy that
reads dirty-owner facts. Well-formed stale inputs return stable sorted typed
rejections; malformed inputs throw the domain validation error.

A positive result is named `accepted_for_shadow_validation`, not authorized or
committed. Every decision fixes `authoritative=False`,
`proposed_mutations=()`, and `performed_side_effects=()`. A legacy `PARTIAL`
Snapshot cannot be accepted; the positive golden uses a synthetic complete
fixture only.

The current v1 runtime remains the only authoritative competition path. These
pure DTO/validators remain default-off and disconnected from the project DB,
Web API/UI and scheduler; this contract does not start Phase 2–10, migration,
outbox, durable receipt or cutover work.
