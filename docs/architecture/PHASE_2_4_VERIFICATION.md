# Phase 2-4 Refactor Verification

Verified on 2026-07-30. This document records the implementation closeout for
the native Step lifecycle, shared application service, and repository/runtime
boundary work. It does not claim production Cloud Solver execution or judge
calibration against human awards.

## Acceptance Result

| Phase | Acceptance result | Evidence owner |
|---|---|---|
| 2 | Step 0-16 use native `prepare/execute/validate/recover` lifecycles; new projects use schema-v3 `native_v2`; the native registry has no Legacy Runner lifecycle | `factory_core/steps/`, `tests/test_native_orchestration.py` |
| 3 | CLI, Web project creation/control, solver policy, and solver jobs use `FactoryService`, project revision, and SQLite events | `factory_core/service.py`, `apps/`, `web/backend/` |
| 4 | Core, apps, benchmarks, legacy adapters, deployment, and runtime data have documented boundaries; Python/Web/Cloud/frontend dependencies are locked; runtime start scripts do not install dependencies | `docs/architecture/`, `pyproject.toml`, `uv.lock`, requirements locks, `package-lock.json` |

The extension criterion is satisfied structurally: a new Step is a lifecycle
plus registry/catalog entry, a new model provider implements `ModelBackend`, and
a new solver transport implements `SolverBackend`. None requires a scheduler,
CLI/Web router, or `run_paper.sh` branch.

## State And Recovery Contract

- `.factory/state.db` is authoritative for new and explicitly migrated projects.
- Every state change compares `revision`, appends an immutable event, and updates
  the snapshot in one SQLite transaction.
- Artifacts and compatibility files remain evidence/projections, not duplicate
  state authorities.
- Normal CLI/Web resume resolves satisfied evidence and records `RESUMED` then
  `WORKER_LAUNCHED`; stale requests neither write a decision nor launch a worker.
- `--no-start` and `--no-resume` retain explicit human-control paths. Step 3
  remains available through `scripts/selection_gate.py select-step3`.
- Recovery validates artifacts instead of comparing modification times.

## Verification Evidence

Commands completed from the repository root:

```text
uv lock --check
  Resolved 93 packages

uv run --isolated --locked --extra web --extra cloud --extra models --group dev pytest -q
  635 passed in 49.38s

fresh frontend copy: npm ci --ignore-scripts
  added 68 packages, audited 69 packages
fresh frontend copy: npm audit --audit-level=moderate
  found 0 vulnerabilities
fresh frontend copy: npm run build
  Vite 8.1.5, 152 modules transformed, built successfully without warnings
```

The final closeout also checks Python compilation, launcher shell syntax,
registry invariants, dependency locks, and `git diff --check` after this record
is written.

## Severity Review

- **Blocker:** none found.
- **Major:** the Web resume path previously returned `ready` without launching a
  worker. It is fixed and covered for ordinary resume, Step 3 selection,
  consultation resolution, stale revision, event order, and illegal terminal
  starts.
- **Minor:** none open in the implemented Phase 2-4 scope.
- **Nit:** none open.

## Deliberate Boundaries

- Cloud Solver remains quarantined by default. The default service registers the
  local backend; a real Cloud transport must be explicitly injected and the
  documented isolation blockers closed before quarantine is removed.
- No live Cloud Solver job, Cloud Run deployment, secret mutation, project
  migration, or runner was executed during this closeout. The `tfisher.de` Web
  release is verified separately through the production runbook.
- Historical project data, backups, credentials, logs, and protected worktrees
  were not removed or modified.
- Judge-to-human calibration is an independent product/evaluation program and
  is not represented as complete by this refactor.
