# Phase 2-4 Refactor Verification

Verified on 2026-07-30. This document records the implementation closeout for
the native Step lifecycle, shared application service, and repository/runtime
boundary work. It does not claim production Cloud Solver execution or judge
calibration against human awards.

## Acceptance Result

| Phase | Acceptance result | Evidence owner |
|---|---|---|
| 2 | Step 0-16 use native `prepare/execute/validate/recover` lifecycles; new projects use schema-v4 `native_v2`; the native registry has no Legacy Runner lifecycle | `factory_core/steps/`, `tests/test_native_orchestration.py` |
| 3 | CLI, Web project creation/control, solver policy, and solver jobs use `FactoryService`, SQLite events, project revision for control requests, and independent job revision for backend confirmation | `factory_core/service.py`, `factory_core/storage.py`, `apps/`, `web/backend/` |
| 4 | Core, apps, benchmarks, legacy adapters, deployment, and runtime data have documented boundaries; Python/Web/Cloud/frontend dependencies are locked; runtime start scripts do not install dependencies | `docs/architecture/`, `pyproject.toml`, `uv.lock`, requirements locks, `package-lock.json` |

The extension criterion is satisfied structurally: a new Step is a lifecycle
plus registry/catalog entry, a new model provider implements `ModelBackend`, and
a new solver transport implements `SolverBackend`. None requires a scheduler,
CLI/Web router, or `run_paper.sh` branch.

## State And Recovery Contract

- `.factory/state.db` is authoritative for new and explicitly migrated projects.
- Workflow control requests compare project `revision`; Worker-owned transitions
  additionally compare PID and lease in the same transaction. Solver backend
  confirmations compare their independent `job_revision` and append an event
  without depending on a stale project revision.
- Artifacts and compatibility files remain evidence/projections, not duplicate
  state authorities.
- Normal CLI/Web resume resolves satisfied evidence and records `RESUMED` then
  `WORKER_LAUNCHED`; stale requests neither write a decision nor launch a worker.
- `--no-start` and `--no-resume` retain explicit human-control paths. Step 3
  remains available through `scripts/selection_gate.py select-step3`.
- Recovery validates artifacts instead of comparing modification times.
- A Worker keeps `RUNNING` ownership between Steps. A live PID blocks all start
  and resume paths, and a replaced lease cannot commit after execution or
  validation.

## Verification Evidence

Commands completed from the repository root:

```text
uv lock --check
  Resolved 93 packages

uv run --isolated --locked --extra web --extra cloud --extra models --group dev pytest -q
  651 passed in 51.97s

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

- **Blocker:** the Step-boundary double-Worker window and stale-lease commit path
  are fixed with transactional PID/lease checks and a competition regression.
- **Major:** recovery stop states, rollback routing, and invalid Step 2 prepare
  behavior are fixed and covered across Engine, Service, Web, and migration
  boundaries. The earlier Web resume launch fix remains covered.
- **Minor:** recovered reopens now consume the normal reopen budget; Solver
  submission confirmation no longer loses external IDs when project control or
  cancellation races with the backend call.
- **Nit:** none open.

## Deliberate Boundaries

- Cloud Solver remains quarantined by default. The default service registers the
  local and quarantined Cloud Run backends through one shared builder. A real
  Cloud call additionally requires `CLOUD_SOLVER_URL`; the documented isolation
  blockers must still be closed before quarantine is removed.
- No live Cloud Solver job, Cloud Run deployment, secret mutation, project
  migration, or runner was executed during this closeout. The `tfisher.de` Web
  release is verified separately through the production runbook.
- Historical project data, backups, credentials, logs, and protected worktrees
  were not removed or modified.
- Judge-to-human calibration is an independent product/evaluation program and
  is not represented as complete by this refactor.
