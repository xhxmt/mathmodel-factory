# Phase 7 Evidence Grounding Shadow

Status: pure validation and hardening slice for ordinary judge evidence
grounding. The current v1 judge, aggregation, audit, routing, and delivery path
remains the sole production authority.

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

`scripts/aggregate_judges.py` already had the selected optional integration:
without a manifest, grounding is not enforced and existing v1 aggregation is
unchanged; with a manifest, the role is grounded and a receipt is written by
that existing caller. Aggregate role parsing preserves exact quote bytes; only
non-quote strings are normalized. Phase 7 adds no caller or production routing.

This slice does not implement delivery hard gates, multi-layer effective
verdict state, release publication, outbox/current-pointer writes, Scheduler,
Web/API, frontend, feature flags, provider/model/Solver calls, or cutover. It
does not use network, SQLite, production configuration, or production data.
