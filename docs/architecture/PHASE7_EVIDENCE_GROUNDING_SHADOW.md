# Phase 7 Evidence Grounding Shadow

Status: Phase 7A pure validation compatibility contract plus an explicitly
enabled, durable Phase 7B full-shadow runtime. The current v1 judge,
aggregation, audit, routing, and delivery path remains the sole production
authority. The durable runtime and its Phase 8 composition are specified in
[`PHASE7_8_DURABLE_FULL_SHADOW.md`](PHASE7_8_DURABLE_FULL_SHADOW.md).

## Selected contract

`scripts/evidence_grounding.py` produces `evidence-grounding-v1` reports for
`math`, `execution`, and `paper`. A hard role must use `judge-hard-role-v2`; a
paper role must use `judge-paper-role-v3`. Each role output retains the strict
two-part envelope: an exact `VERDICT: ...` first line followed only by one JSON
object whose role, schema, and verdict match the requested validator.

The validator reads only the explicitly supplied role output, manifest, and
context files. It binds the manifest role and `files` array, then verifies the
SHA-256 and raw byte size of the complete `context.txt` against
`manifest.context`.

For every active `included` or `truncated` file, the validator requires:

- a non-blank path without CR or LF;
- a unique 64-character lowercase hexadecimal `chunk_id`;
- a 64-character lowercase hexadecimal `included_sha256`;
- a non-boolean, non-negative integer `included_bytes`;
- a non-boolean integer `source_line_start` of at least one;
- a path unique among active chunks.

Context sections use the exact packet-builder delimiter
`\n----- FILE: <path> -----\n`. Duplicate, undeclared, and missing sections are
invalid. A declared section must match both `included_sha256` and
`included_bytes`. Content matching tries only the raw section, raw content
minus one terminal newline, and raw content minus two terminal newlines. The
recognized packet-builder omitted-files marker is removed only at the final
section boundary. No fuzzy content or quote matching is performed.

## Evidence receipts

Hard-role references come from `evidence[]`. Paper references come from every
`dimensions.*.evidence[]` plus `issues[]`. `ref_id` is non-blank and unique
across the role. `chunk_id` must identify an active packet chunk and `quote`
must be non-blank. A supplied `quote_sha256` must equal the system-computed
SHA-256 of the exact UTF-8 quote.

The quote must occur exactly once in the resolved chunk. A valid reference
receipt records its normalized `ref_id`, chunk ID, system-computed quote hash,
resolved path, source line range, and context line range. The compatibility
`line_start`/`line_end` fields carry the same source range.

Every input failure returns a stable structured invalid report instead of
escaping as an input exception. The CLI atomically writes the report to its
explicit `--output`: valid is exit 0, invalid is exit 1, and output-write
failure is exit 2. The library performs no writes.

## Aggregator compatibility and exclusions

`scripts/aggregate_judges.py` retains the selected Phase 7A optional integration:
without a manifest, grounding is not enforced and existing v1 aggregation is
unchanged; with a manifest, the role is grounded and a receipt is written by
that existing caller. Aggregate role parsing preserves exact quote bytes; only
non-quote strings are normalized. Phase 7A adds no caller or production routing.

Phase 7B adds a separate default-off SQLite store for exact three-role bytes,
revision-level Phase 3 occurrence and aggregate-state binding, Phase 6 current
proof binding, path-free durable receipts, effective verdict/current
projection, restart, concurrency, and exact replay. It is reachable only
through the explicitly enabled local Phase 7+8 sidecar and never calls a
provider, model, Solver, production workflow callback, outbox, or delivery
route. It does not change the Phase 7A report wire or make either slice
authoritative.
