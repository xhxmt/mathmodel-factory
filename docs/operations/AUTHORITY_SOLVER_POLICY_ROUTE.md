# Authority solver-policy route

This is the first application command routed to Authority. It supports solver
configuration and its query through the existing `FactoryService` and CLI.
It does not switch the modeling scheduler, launch a worker, or enable delivery.
Existing projects without Authority, and installations in `V1_ONLY`, retain
their existing behavior. Do not enable this pilot on an executing project.

## Required installation and owner

Use the existing explicit-path, verified-backup Authority operator workflow to
install through `A2_0021_NATIVE_WRITE_FENCE` (production schema version 9).
Published A2_0001–A2_0020 statements are unchanged. The new migration checksum is
`3f942805c29604ac253a804db6d4389bcb1258e8eeb0869d8c97bd2765fa7820`.
Test migration, replay and restore on a stopped project copy before deployment.
Close every database connection before a restore, including connections held by
diagnostic scripts; a stopped scheduler alone does not release those handles.

The durable writer must be explicitly configured as `factory-service`, with
its current writer epoch enabled, before recording `CANARY` or
`AUTHORITY_PRIMARY`. An arbitrary writer name, disabled writer, incomplete
migration or inconsistent schema is an error; none falls back to a legacy
write. Existing control authorization and switch-epoch CAS still apply.

The current Foundation switch requires an enabled consumer identity. This is
only a durable fence: this route does not start a consumer or implement a
provider callback. Each committed configuration includes a pending
`authority.solver-policy.changed` notification. Its delivery state remains
`PENDING`; no dispatch or delivery receipt is fabricated.

## Command and read behavior

`solver policy --mode ... --expected-revision ...` uses
`CONFIGURE_SOLVER_POLICY`. The command binds the normalized actual policy,
project/workflow generations, source-compiled contract pins and expected
Authority revision. The fenced writer atomically appends its command, event,
receipt, notification intent and idempotency record, and advances Authority
once. A changed request with a stale revision fails; an exact request replay
returns the original result without overwriting a later configuration.
Authority writes require an explicit expected revision, obtained from the policy
query. Omitting it is rejected before persistence so a retry cannot silently
become a new command. V1_ONLY keeps its existing optional-revision behavior.
The public writer also enforces the `factory-service` owner, independently of
the application route; another enabled durable writer cannot submit this command.

`solver policy` reads the most recent committed configuration through verified
immutable command evidence. The result retains the existing policy,
quarantine and enabled fields, and adds `authority=authority` and the current
Authority `revision`. Before the first configuration command, its policy is
the frozen legacy policy imported by the migration. The original workflow
database rows and compatibility files are not used as competing writable
projections. The result's `updated_revision` belongs to the configuration
event; `revision` is the captured Authority workflow revision.

The generic M0.3 Shadow command/CAS policy still rejects this new command.
This application route uses the production writer's transaction-bound revision
and writer fences and the dedicated typed solver-policy contract. It does not
reinterpret a Shadow test receipt as production authorization.

## Legacy write isolation

A2_0021 installs INSERT/UPDATE/DELETE guards on all 19 schema-v9 native tables.
They evaluate the persisted mode inside the SQLite write transaction, including
for old processes, prepared statements and direct SQL. While mode is CANARY or
PRIMARY, native writes fail with `AUTHORITY_LEGACY_WRITE_DISABLED`.
`SQLiteStateStore` translates this into `InvalidTransition` before compatibility
projections or subsequent execution can occur. Read-only inspection remains
available. Exact guard DDL is excluded from the frozen legacy source identity;
altered or renamed DDL is not, and the production schema verifier also detects
missing guards.

Pause/resume, native checkpoints, human decisions and Solver execution have not
yet been ported to Authority command handlers. They must remain blocked in this
pilot instead of silently updating the old workflow. The Web's general project
state is not yet an Authority scheduler view. This route is therefore suitable
for isolated integration validation, not a complete production workflow cutover.

An explicit fallback to V1_ONLY disables Authority actors and allows the frozen
native route again; it does not copy new Authority configuration back into the
old project. Authority history stays available for reconciliation. Use the
existing verified restore procedure when an exact pre-migration rollback is
required. Do not merge old and new revisions or synthesize completed Steps.
