# Phase 8 Reference Evidence and Data Egress Shadow

Status: direct-test-only shadow contract. The existing v1 runtime, UI, audit,
aggregation, delivery, and operator paths remain the only production authority.

## Scope

Phase 8 adds two independent, pure validation slices:

1. `factory_core.reference_evidence` compiles already-materialized reference
   facts into a deterministic `reference-document-record-v1`.
2. `factory_core.data_egress` stages a declared transfer request and evaluates
   an optional, exact approval receipt without performing a transfer.

Neither module is imported by the CLI, Scheduler, Web/API, frontend, judge
aggregator, or another production entry point. They have no feature flag and no
shadow writer. Importing either module has no runtime side effect.

## Canonical reference evidence

`validate_canonical_reference_evidence()` accepts only an in-memory
`canonical-reference-evidence-v1` mapping. The caller supplies:

- a content-addressed raw-PDF fact (`blob_ref=sha256:<digest>`, digest, and byte
  length);
- a successful, unencrypted inspection fact bound to the same PDF digest and a
  positive page count;
- exactly one ordered page fact per inspected page, with a non-blank page label,
  hashed PNG-render declaration, and exact hashed canonical text;
- one or more ordered chunks whose derived IDs bind the reference ID, contiguous
  ordinal, page range, and exact text hash;
- normalized bibliographic metadata and one provenance fact for every metadata
  field, each bound to the canonical value hash; and
- one supported record-level external-share classification.

The result is a frozen `ReferenceDocumentRecord`. Its nested facts are frozen,
its `as_dict()` representation is deterministic, and `record_sha256` binds the
entire record identity. The returned `external_share.authority_granted` is
always `false`: classification records a fact and never authorizes disclosure.

The validator does not accept a filesystem path and does not open a PDF, read a
blob, inspect PDF structure itself, render, OCR, fetch metadata, persist a
record, or publish a reference package. Hash and byte-length checks are limited
to exact text bytes present in the supplied in-memory facts; opaque PDF and PNG
facts are cross-bound declarations for a future trusted materializer to supply.

## Data-egress staging and approval binding

`evaluate_data_egress()` validates a `data-egress-request-v1`, normalizes its
artifacts into ascending artifact-ID order, and always constructs a canonical
`data-egress-staged-manifest-v1` in state `STAGED`. The manifest and its hash
bind:

- subject, provider, surface, account scope, retention, and purpose;
- policy identity; and
- every artifact ID, SHA-256, byte length, transfer form, and classification.

Without an approval, the ordinary result is a `data-egress-decision-v1` with
`status=DENIED`, `reason_code=APPROVAL_MISSING`, the complete staged manifest,
and `dispatch_performed=false`.

A `data-egress-approval-v1` can authorize only when it is affirmative and
exactly binds the staged-manifest hash, subject, current policy hash, purpose,
and the canonically ordered artifact ID/hash pairs. A well-formed mismatch stays
`DENIED`; an exact match returns `AUTHORIZED` with
`reason_code=EXACT_APPROVAL_MATCH`. Authorization is only a pure decision:
`dispatch_performed` remains `false` in every result.

`verify_data_egress_decision()` revalidates a JSON-round-tripped decision,
including request, policy, staged-manifest, approval, and decision hashes. It
reads no external state. `data_egress_policy_sha256()` exposes the stable
identity of the declaration-only `data-egress-policy-v1` policy.

## Explicit non-goals

This slice is not a PDF parser, CAS/blob store, OCR or render pipeline,
reference-package builder, secret scanner, authenticated approval ledger,
identity verifier, provider adapter, dispatch service, durable outbox, database
writer, or production cutover. It does not upload, transmit, fetch, schedule,
persist, mutate a real artifact, or contact a real service.

The fixed transfer-form and classification vocabularies are shadow wire values,
not a complete enterprise policy taxonomy. A valid reference record is not a
data-egress request, and an `AUTHORIZED` shadow decision is not proof that a
transfer occurred.

## Future integration prerequisites

Before any production caller is allowed, a later phase must separately define
and review:

- a trusted materialization boundary that proves opaque PDF/PNG bytes match the
  declared hashes and byte lengths;
- authenticated, durable approval identity and revocation semantics;
- provider/account policy, secret scanning, and destination-specific controls;
- a durable authorization/dispatch boundary with idempotency, receipts,
  reconciliation, and audit storage;
- explicit Scheduler/Web/API ownership and failure routing; and
- a cutover plan proving that no shadow result silently supersedes current v1
  authority.
