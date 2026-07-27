# AGENTS.md

This file is the repository entry point for coding agents. Read it before
editing, then use the linked documents instead of inferring contracts from old
reports.

## Sources of truth

- `STEPS.md`: active modeling workflow and file gates.
- `modeling_guide.md`: modeling, solver, evidence, LaTeX, figure, and table rules.
- `CLAUDE.md`: detailed repository architecture and editing guidance.
- `.agent/rules/superpowers.md`: local planning and verification gate. A user may
  explicitly waive its execute-plan pause; do not infer a waiver.
- `web/README.md` and `web/docs/deployment/DEPLOYMENT.md`: current Web usage and
  production runbook. Other Web reports are historical snapshots unless they
  explicitly say otherwise.

When documents disagree with code or runtime state, verify the implementation
and update the current document. Do not silently preserve two current answers.

## Required working habits

- Inspect `git status --short --branch` and all relevant worktrees before edits.
  Preserve user changes and unrelated untracked files.
- Do not delete worktrees, branches, backups, logs, generated papers, or local
  credentials without explicit confirmation after reporting exact targets.
- Treat `run_paper.sh --infer-step <project_dir>` as the authoritative workflow
  status check. `checkpoint.md` and the presence of `complete/` are not proof of
  current-contract completion.
- Use `solver_submit.sh` for nontrivial solver work and explicit time limits.
- Prefer focused tests for the touched contract. Full-repository pytest can be
  noisy because historical test modules and optional Web/runtime dependencies
  coexist in this checkout.
- Finish with `git diff --check`, relevant tests/builds, and a severity review
  (`Blocker`, `Major`, `Minor`, `Nit`).

## Web control plane

- `web/backend/main.py` is the FastAPI application. `web/backend/app.py` is a
  compatibility launcher only.
- Authentication and project approvals are persisted in `web/auth.db` through
  `web/backend/auth_store.py`; passwords are bcrypt hashes.
- Registration creates a pending user. Administrators approve users and project
  requests. Non-admin users see and control only projects granted through the
  project ACL; administrators see all projects.
- Unauthenticated access is limited to the read-only paper showcase configured
  by `SHOWCASE_PROJECTS`.
- The dashboard groups repeated runs of the same contained problem statement by
  a canonical SHA-256 problem identity. `ongoing/` and `complete/` remain the
  storage/runtime truth; the UI grouping is not a data migration.
- Keep CLI and Web human-choice paths in parallel. Step 3 selection must remain
  possible through `scripts/selection_gate.py select-step3`.

## Secrets and generated state

- Production sensitive values come from GCP Secret Manager through
  `scripts/load_secrets.sh`. Never print values or value prefixes in docs,
  tests, logs, or diagnostics.
- `JWT_SECRET` and `ADMIN_PASSWORD` are required; weak defaults make the backend
  refuse startup. Do not document a default password or automatic JWT creation.
- `.env`, `web/.env`, their backups, `.claude/settings.local.json`, runtime
  directories, build output, logs, and generated papers must not be committed.
- Secret rotation and deletion are operational changes. Report exposure and
  dependencies first, then obtain explicit approval.

## Documentation ownership

- User entry: `README.md` and `DOCUMENTATION_INDEX.md`.
- Current Web operation: `web/README.md`, `web/QUICKSTART.md`,
  `web/USAGE_GUIDE.md`, and `web/docs/deployment/DEPLOYMENT.md`.
- Historical design/verification reports must carry a historical-snapshot notice
  and point to the current owner; they must not contain usable credentials.
- Update `CHANGELOG.md` when behavior, contracts, or operator procedures change.
