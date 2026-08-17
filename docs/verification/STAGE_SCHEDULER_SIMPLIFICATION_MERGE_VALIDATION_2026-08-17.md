# Stage Scheduler Simplification Merge Validation

Date: 2026-08-17 (Asia/Hong_Kong)

## Final judgment

ChatGPT Pro issued the explicit judgment `MERGEABLE_TO_ORIGIN_MAIN` for the exact identities below. Independent repository validation also found no remaining code-level merge blocker.

- Conversation: <https://chatgpt.com/g/g-p-69761d3eed688191a65fbd15539baf81/c/6a8180b9-1ee0-83ea-a8ff-5baa1892a333>
- Branch: `codex/stage-scheduler-simplification`
- Source baseline: `104c2fcb9856bc2025aea968c5d1abd2ae1573ff`
- Fetched target `origin/main`: `7389ccaba0295e3499668c87f6d3358438391e1b`
- Source bundle: `paper_factory_104c2fcb_pro_fix_bundle.zip`
- Source bundle size: 752375 bytes
- Source bundle SHA-256: `2be6df22eae8d03a587bcd6bad28e3ef946a34061b497458955be05fe3392b94`
- Accepted patch: `modeling_factory_merge_blocker_remediation_round2_corrected_104c2fcb.patch`
- Accepted patch size: 195194 bytes
- Accepted patch SHA-256: `5b973cd4c648fb43268d52a55ff46bc4393329e6a80689c981aa56b54722653e`
- Review ZIP size: 168495 bytes
- Review ZIP SHA-256: `e8e33df439045dcd77117e6098cdc91a237ac64c05b5f0083d7f33cd4d88d2d1`

`origin/main` was the merge base. The branch was 13 commits ahead and 0 behind at validation time. A read-only `git merge-tree` inspection found no textual conflict markers.

## Delivery history

The initial Pro delivery was rejected because it contained 75 generated `.pyc` files, had circular imports, and produced 18 focused-test failures after diagnostic-only import corrections.

The first corrected replacement was rejected after complete-repository validation exposed three functional compatibility regressions:

1. Legacy runtime status incorrectly stopped accepting the frozen no-SQLite `READY` format.
2. Direct `PromptStep` lifecycle execution raised `StateNotInitialized` before dispatch.
3. The legacy/no-SQLite Web consultation path incorrectly required an engine request ID.

The accepted replacement preserves explicit engine/legacy authority boundaries while retaining the seven remediation areas from the original engineering task.

## Independent validation

The accepted patch was applied with `git apply --whitespace=error-all` to a fresh detached worktree at the exact source baseline. Four new files were marked intent-to-add only inside that isolated worktree so a complete diff could be regenerated. The regenerated `git diff --full-index --binary HEAD` was byte-identical to the delivered patch and had the same SHA-256.

Results on the exact patched source:

- Compile: `python -m compileall -q factory_core cloud web/backend` passed.
- Required six-file focused suite: 189 passed.
- Prior regression nodes plus Consultation authority and engine pre-dispatch receipt-order tests: 8 passed.
- Core CI selection in the isolated worktree: 938 passed, 1 failed, 9 deselected.
- Core CI selection after applying the identical accepted patch to the original checkout path: 939 passed, 9 deselected.
- Web backend/API suite: 64 passed.
- Frontend production build: passed with Vite 8.1.5; 169 modules transformed.
- LaTeX suite: 9 passed, 15 deselected.
- Cloud suite: 74 passed.
- `git diff --check`: passed.

The isolated-worktree Core failure was `tests/test_verify_numbers_refactor.py::test_cli_output_byte_identical`. Its diff consisted solely of the isolated worktree absolute path versus the golden original-checkout path. The same node first passed independently in the unmodified original checkout at the same commit; after the accepted patch was applied to the original checkout, the full Core selection passed 939/939. Production code and the golden file were not changed to hide the environment-only mismatch.

The repository defines no separate lint or typecheck command in `pyproject.toml` or `web/frontend/package.json`; the configured frontend production build was executed.

## Reviewed invariants

- Engine-controlled Prompt dispatch persists an immutable `factory-effective-prompt-v1` input receipt under SQLite revision CAS before invoking the dispatcher. Stage completion rejects a missing or mismatched receipt.
- Standalone direct-lifecycle compatibility is restricted to revision 0 without a workflow database, emits no durable receipt identity, reports `prompt_input_receipt_durable=false`, and fails closed if SQLite authority appears.
- Engine Consultation answers are request-scoped and committed through SQLite CAS before deterministic shared projection. Mutable `READY` text cannot satisfy engine gates.
- Frozen Legacy `READY` handling remains confined to no-engine/file-authoritative compatibility paths.
- Consultation request, review, solver input, and receipt paths fail closed on unsafe symlink substitution.
- Dirty-classifier rebase and prompt-input receipts are append-only and auditable.
- TeX conditional state capable of changing rendered mathematics produces `MATH_DIRTY` and reopens the responsible Stage.
- Every solver-declared project input must enter final/submission identity or have an immutable hash-bound exclusion receipt; missing ownership and content drift block Finalization.

## Patch scope

The patch changes 23 source/test files, with 4032 insertions and 172 deletions. It contains no Git binary records, credentials, cache files, databases, logs, runtime state, browser state, generated papers, or build output.

The implementation adds or strengthens:

- request-scoped Consultation staging, immutable decision subject binding, deterministic projection recovery, and legacy/engine separation;
- durable effective-prompt receipts and recovery/checkpoint guards;
- dirty-classifier rebase receipts and multi-owner obligation reconstruction;
- TeX conditional/control-state math-dirty classification;
- solver-declared input coverage in Finalization and submission packaging;
- fault-oriented, concurrency, compatibility, migration, path-safety, and append-only regression tests.

## Remaining limits

This validation covers source merge safety. It does not claim GitHub Actions execution, production deployment, real-user operation, a real competition project end-to-end run, database migration in a live environment, or production release validation. A clean-room real-project Final Audit and atomic-release exercise remain production-enablement checks, not blockers for this source merge judgment.

## Repository state at handoff

The accepted patch and this report are local working-tree changes only. No commit, push, pull request, deployment, production configuration change, or real database migration was performed. Pre-existing untracked audit/transcript files in the main worktree were preserved.
