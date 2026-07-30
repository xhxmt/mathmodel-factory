# Worktree Consolidation Snapshot - 2026-07-30

> Historical snapshot. Current architecture and runtime contracts remain owned
> by `factory_core/`, `STEPS.md`, `CLAUDE.md`, and the active deployment
> runbook. This record explains why old worktree histories were merged without
> restoring their obsolete implementations over `main`.

## Consolidation Decisions

| Source | Recovered value | Main-line disposition |
|---|---|---|
| `fix/cumcm2025a-p4-p5-model` | Corrected P4/P5 solver, regression tests, bounded experimental results, and a large-request Cloud client fix | Solver material is preserved in `benchmarks/cumcm2025a_p4_p5/`. The Cloud request fix is reimplemented against current IAM authentication in `scripts/gcp_solver_client.sh`. Generated paper artifacts remain ignored. |
| `fix-contract-runner-eval` | Delivery manifest, complete-project audit, workflow-state, model-dispatch, and structural/LLM evaluation separation | Function and test entrypoints are already covered by newer main-line implementations. Its Bash runner changes predate the versioned Python engine and are not restored. |
| `autoresearch-score-opt` | Nine calibration experiments covering blind pair judging, adjudication, resumable pairs, axis reliability, official problem context, and recorded proxy results | Later commits `3d8e610` and `e56be6a` implement a strict superset with schema identity, freshness, objective evidence, and runtime reliability gates. The experimental history is retained through a strategy merge; its stale reports do not replace current reports. |

## Merge Policy

Dirty source lanes are first committed on their own branches so their exact
working state remains recoverable. Main then records those branch heads with an
`ours` strategy merge after the useful content has been curated. This preserves
ancestry while preventing old Bash orchestration, stale evaluation schemas, or
damaged worktree filesystem state from replacing current code.

No worktree, branch, generated paper, backup, result, or credential is deleted
as part of this consolidation. Physical worktree removal remains a separate
operation requiring explicit approval after exact targets are reported.

## Verification

- Current locked repository suite: `636 passed in 50.78s`.
- Recovered P4/P5 project suite in its pinned numeric environment: `14 passed`.
- Focused Cloud routing, calibration, delivery, and workflow-state suite:
  `53 passed`.
- All three source branch heads are ancestors of `main`; strategy merges retain
  history while the main tree keeps the curated implementation above.
