# Run4 Technical Flow Bug Ledger

This ledger records workflow, API, and frontend defects observed during the downstream-only validation of `cumcm_2025_b_codex_luna_stability_20260817_run4`. It is separate from the paper's scientific issue ledger. Entries do not imply scientific quality approval.

## TF-RUN4-FE-001 — Pending content-freeze decision is not rendered

- Status: OPEN
- Observed: 2026-08-19
- Severity: HIGH (blocks the only legitimate human action needed to enter Final Audit)
- Frontend route: `https://tfisher.de/#/p/cumcm_2025_b_codex_luna_stability_20260817_run4?tab=selection`
- Frontend revision label: `REV 2438`
- Actual frontend result: the Human Gate panel says `暂无待处理人工节点` and `人工决策加载失败`.
- Expected result: render the pending `content_freeze` request and its two choices, `approve_content_freeze` and `reject_content_freeze`.
- Backend evidence at the same revision:
  - SQLite/status reports `status=awaiting_selection`, `active_stage=10`, `current_step=16`, `active_subtask=delivery`.
  - `pending_action.type=content_freeze_selection`, `pending_action.gate=content_freeze`, `request_id=10e6fb2b195fbcefe173e050`, `requested_revision=2438`.
  - `selection/content_freeze_options.json` exists, `available=true`, and contains both choices.
  - The request metadata retains six explicitly authorized stale-input drift records; `content_freeze_approved=false`, `quality_pass_fabricated=false`, and `delivery_allowed=false`.
- Impact: the backend is legitimately waiting for a human selection, while the frontend falsely presents no pending node and gives no actionable control. Users cannot distinguish a valid wait from a backend failure and cannot continue through the UI.
- Classification: frontend/API decision-loading or response-mapping defect. The screenshot alone does not identify whether the API request fails, authentication is omitted, or the frontend rejects the valid response schema; browser network/API logs are required to localize the failing layer.
- Acceptance criteria:
  1. At revision 2438-equivalent state, the Selection tab displays the `content_freeze` request and both choices.
  2. Refresh preserves the pending request and does not show `暂无待处理人工节点`.
  3. API/authentication errors are surfaced with an actionable status and do not get collapsed into an empty-state message.
  4. A submitted choice is bound to the displayed request id, requested revision, subject fingerprint, and options fingerprint.

## TF-RUN4-CTRL-002 — Controller restart invalidates an approved Human Gate

- Status: FIXED-RUNTIME-VERIFIED
- Observed: 2026-08-19
- Severity: HIGH (creates an approval loop and prevents Final Audit from starting)
- Actual result:
  - Generation 1 request `10e6fb2b195fbcefe173e050` was explicitly approved at revision 2438 and persisted at revision 2439.
  - The controller restart rewrote `.factory/technical_flow/run4_stale_solver_inputs.json` despite identical six drift fingerprints.
  - Its SHA-256 changed from `8b7200e3912d09c53b48cbaadce25935a474dee36860e83c78acd96f0f20e2c5` to `870ce576ee957f89a14ed0ff26bb53f3c77bbc1eb691d657a8dcfe87a539ab10` solely because volatile authorization metadata was regenerated.
  - The content-freeze subject fingerprint consequently changed from `70e1fc3298c072aae20a001a63eb56d0584bd23eb08c25c702765ca817599519` to `ae08c4aabe32f029c305593a4ed1731e69f01537098fae7261892321bfd37ffc`.
  - Revision 2441 entered `awaiting_selection` again with generation 2 request `26994957b18a3fbeebedbda6`.
- Expected result: when the authorization is unexpired, validates successfully, and authorizes the exact same drift fingerprints, controller restart must reuse the byte-identical receipt and SHA instead of rewriting it.
- Fix deployed: `authorize_stale_solver_inputs` now validates and reuses an existing exact authorization before attempting enumeration or receipt creation. A changed/expired/invalid receipt still fails closed and is regenerated through the existing enumeration path.
- Runtime verification: unchanged authorizations were reused byte-for-byte across controller restarts. New Human Gate generations were created only when the authorized drift set materially changed (from six to 37 inputs) or the bound snapshot changed; every generation was separately approved. After the generation 4 approval, restart proceeded to all three real Final Judge roles without creating another request.
- Safety: the fix did not reuse a Human Gate decision, approve content freeze, fabricate quality acceptance, or enable delivery.
- Acceptance criteria:
  1. Repeated controller starts retain authorization SHA `870ce576ee957f89a14ed0ff26bb53f3c77bbc1eb691d657a8dcfe87a539ab10` while the six drift identities remain unchanged and the receipt is unexpired.
  2. After a request is explicitly resolved, restart proceeds without a new generation when its bound subject and options fingerprints are unchanged.
  3. Any changed drift bytes or expired receipt still invalidate authorization and require a new auditable request.

## TF-RUN4-CORE-003 — Missing input in a historical solver receipt crashes Final Audit

- Status: FIXED-RUNTIME-VERIFIED
- Observed: 2026-08-19
- Severity: HIGH (kills the Step 16 worker before the real judge starts and leaves SQLite temporarily reporting `ACTIVE`)
- Trigger: `.factory/solver_receipts/local_python_20260817094721_a2a65cb0.submitted.json` references `data/intermediate/m2_spectra.npz`, which is no longer a regular project file.
- Actual result: `build_final_input_manifest` calls `solver_declared_input_coverage`, which raises an unstructured `ValueError`; the worker exits before Luna runs and the exception bypasses the normal terminal-state transition.
- Expected result: an absent declared input is represented as an exact, fingerprinted solver-input drift. Strict/default execution still fails closed. The run4 downstream-only controller may authorize that exact missing identity, preserve its receipt as evidence, and continue without pretending the absent file is present or deliverable.
- Fix deployed:
  - Missing files are distinguished from symlink, path-escape, directory, and other unsafe-path errors; unsafe paths remain non-authorizable.
  - A missing input produces `SolverInputDriftError(kind="missing")` with `current={"exists": false}` and receipt-bound fingerprint.
  - Exact technical authorization includes the historical receipt and authorization receipt as evidence but does not add the absent path to the final manifest.
  - Recreating different bytes invalidates the missing-input authorization and fails closed.
- Verification: the new missing-input test and the existing content-drift authorization test pass; the final affected suite reports `122 passed`, and `git diff --check` passes.
- Safety: this continuation is only for downstream bug validation. It does not establish reproducibility, scientific quality, content-freeze approval, or delivery permission; a genuine judge may report the missing input as a defect.

## TF-RUN4-SCHED-004 — Pre-judge crash consumes the only Step 16 attempt

- Status: FIXED-RUNTIME-VERIFIED
- Observed: 2026-08-19
- Severity: HIGH (recovery fails before executing the repaired stage)
- Actual result: the unhandled final-input validation crash started Step 16 and consumed attempt 1. After the dead runner was converted to `INTERRUPTED`, scheduler recovery selected the same delivery subtask but revision 2449 immediately failed with `PERMANENT_ATTEMPT_BUDGET_EXHAUSTED`; no judge had been dispatched.
- Expected result: a pre-judge infrastructure/manifest crash that is subsequently fixed must be resumable through a bounded, auditable technical-flow transition. It must not be reported as a scientific retry or successful stage execution.
- Fix deployed: the run4 controller recognizes only the exact revision shape `FAILED + Stage 10 delivery + last Step 15 + latest event PERMANENT_ATTEMPT_BUDGET_EXHAUSTED`, records `TECHNICAL_FLOW_ATTEMPT_BUDGET_RESET`, changes status to `INTERRUPTED`, and resets only the active delivery attempt to zero.
- Safety: the transition records `judge_dispatched=false`, `quality_pass_fabricated=false`, and `delivery_allowed=false`; all other failure and drift evidence remains intact.
- Additional evidence-quality fix: future dead-runner recovery events no longer hard-code the obsolete first drift error; they record a generic unhandled-worker-exit classification and refer to the immutable worker log.

## TF-RUN4-BUNDLE-005 — Solver runtime log is declared as an unroutable final input

- Status: FIXED-RUNTIME-VERIFIED
- Observed: 2026-08-19
- Severity: HIGH (kills Step 16 final-manifest construction before the judge starts)
- Trigger: a solver submission receipt declares `logs/solver_jobs/local_python_20260818114753_3bf5a7b3.stderr.log` as an input.
- Actual result: solver coverage accepts the current matching file, but `submission_bundle_paths` correctly rejects it because runtime logs have neither artifact ownership nor an explicit delivery route.
- Expected result: stdout/stderr runtime logs must not be inserted into the final submission or judge bundle. Any exclusion must be explicit, hash-bound, append-only, and visible as evidence.
- Fix deployed: the run4 controller enumerates only errors with the exact unrouted-input prefix, accepts only regular non-symlink files under `logs/solver_jobs/` ending in `.log`, and writes existing-schema `solver_input_exclusion` receipts bound to each file SHA-256. Any other unrouted path still fails closed.
- Safety: no global ownership rule is relaxed; runtime log contents are not copied into the final bundle; exclusion receipts remain in the evidence manifest. The controller's attempt reset now also covers a freshly recovered dead worker whose pre-judge crash consumed the attempt.

## TF-RUN4-GOV-006 — Technical validation ledger is misclassified as a paper artifact

- Status: FIXED-RUNTIME-VERIFIED
- Observed: 2026-08-19
- Severity: HIGH (blocks Step 16 manifest construction before the real judge starts)
- Trigger: the downstream-only validation ledger was stored at project root as `technical_flow_bug_ledger.md`.
- Actual result: root-level Markdown files are intentionally covered by authored-artifact governance, so `submission_bundle_paths` rejected the ledger with `authored artifact ownership coverage failed` because it had no scientific owner stage or delivery route.
- Expected result: controller diagnostics and technical-flow audit evidence remain persistent and reviewable without being treated as paper content or submission members.
- Fix deployed: relocate the byte-preserved ledger to `.factory/technical_flow/technical_flow_bug_ledger.md`, alongside the run-scoped technical authorization evidence. The original root path is removed so it no longer enters authored-artifact discovery.
- Safety: no global ownership registry is relaxed, no submission member is added or removed except the already-invalid unowned diagnostic file, and no quality, Human Gate, judge, or delivery result is synthesized.

## TF-RUN4-AUDIT-007 — Technical-flow Final Audit stops before the live judge

- Status: FIXED-RUNTIME-VERIFIED
- Observed: 2026-08-19
- Severity: CRITICAL (prevents the requested Step 16 judge-path validation)
- Trigger: revision 2464 entered Step 16 after a valid generation 4 content-freeze approval while `audit_issue_ledger.md` intentionally retained OPEN/BLOCKING scientific issues.
- Actual result: `FinalAuditService` created snapshot `55c3901b2ca66837488f6ee45e9624b16e5052487aa46f8b88a89f0c14490a38`, then returned `CONTENT_NOT_READY / PERMANENT_DELIVERY_ACCEPTANCE` with `judge_completed=false`. No Luna output was created, even though the service instance was explicitly configured for downstream-only technical-flow validation.
- Expected result: normal production audits continue to fail closed on unresolved blocking issues. A run-scoped `technical_flow_validation=True` audit may retain those issues as negative evidence and still execute the real judges so the downstream scheduler, aggregation, and termination path can be tested.
- Fix deployed: unresolved blocking-ledger short-circuiting is bypassed only for `technical_flow_validation=True`; model stubs remain a hard stop in every mode. The technical-flow result still forces `delivery_allowed=false` and cannot create a deliverable snapshot authorization.
- Verification: the existing technical-flow test now injects an unresolved blocker and requires one real judge call; a new control test proves normal Final Audit still returns `PERMANENT_DELIVERY_ACCEPTANCE` without calling the judge.
- Additional runtime evidence: after the two superseded `.stub` files were archived, revision 2476 completed compilation and all nine final acceptance checks. Seven passed; `derived_artifacts` and `provenance` failed hard. The service again stopped before judge dispatch because final-acceptance failures were unconditional.
- Additional fix: technical-flow validation now retains `judge_outputs/final_paper_checks.json` and its hard failures in the snapshot but does not short-circuit before the live judge. Normal Final Audit remains unchanged and fails closed on any hard acceptance failure. The technical-flow test injects both an unresolved ledger and a hard `derived_artifacts` failure before requiring the judge call.
- Runtime verification: revision 2482 passed these pre-judge branches, created final snapshot `961e5d6bda5a14363aa65c318e90c92362e070bc3c91f0316dc82eeb1a323b6d`, ran all three isolated Luna roles, grounded and aggregated their outputs, and produced a decision route.

## TF-RUN4-PREFLIGHT-008 — Superseded placeholder backups remain in the active models tree

- Status: FIXED-RUNTIME-VERIFIED
- Observed: 2026-08-19
- Severity: HIGH (the generic `.stub` hard gate stops Step 16 before judge dispatch)
- Trigger: `models/m1_fringe/02_model.py.stub` and `models/m1_fringe/04_postprocess.py.stub` remained after the corresponding implemented `.py` files were created.
- Actual result: both `.stub` files still contain the original Step 5 `NotImplementedError` placeholders. `FinalAuditService._has_stub` correctly treats any `.stub` under `models/` as content not ready, but its generic failure record does not identify the paths; revision 2472 therefore repeated `CONTENT_NOT_READY` with empty evidence.
- Evidence: the archived SHA-256 values are `e4a22cd04c164088670d4d7f2adf9728629fbdc335292e1c00c78bf39d131013` and `5fa0fda32258caccfaf01f10d3816d447247ef5f30628c1697036dd6d67f20c2`. The active implementations have distinct SHA-256 values `c1253ec7842fd706b5e5be419dedcf7d59c28390186fb5e5a386f8ca1e996b8a` and `f7399e87261ba00ac56e435e76bd0d6142e9213ac1f4cda164728fbfc3a6d154`.
- Fix deployed: move the superseded placeholders byte-for-byte to `.factory/technical_flow/archived_stubs/models/m1_fringe/`, outside the active model tree, and preserve their hashes in this ledger.
- Safety: the live `.py` implementations are untouched, the global stub hard gate remains enabled, and any future `.stub` in `models/` will still fail closed.

## TF-RUN4-JUDGE-009 — Valid role verdicts are downgraded by quote grounding

- Status: OPEN
- Observed: 2026-08-19
- Severity: HIGH (changes the aggregate verdict after all three real judges finish)
- Actual result:
  - The paper judge wrote `VERDICT: PASS`; the math judge wrote `VERDICT: PASS`; the execution judge wrote `VERDICT: INDETERMINATE`.
  - All three roles ran as isolated `gpt-5.6-luna` judges with `model_reasoning_effort=xhigh`.
  - Grounding downgraded the paper role to `INDETERMINATE` because refs `paper-model-2`, `paper-writing-1`, `paper-results-1`, and `paper-results-2` reported `QUOTE_NOT_FOUND`.
  - Grounding downgraded the math role to `INDETERMINATE` because refs `math-e2` and `math-e6` reported `QUOTE_NOT_FOUND`.
  - `judge_outputs/aggregate.json` therefore records all three roles as `INDETERMINATE` and the legacy aggregate verdict as `INDETERMINATE_REVIEW`, even though the raw paper and math protocols were `PASS`.
- Expected result: quote validation should use the same canonical text representation supplied to the role and should identify the exact rejected quote and chunk normalization mismatch. A grounding failure may invalidate evidence, but it must remain distinguishable from the judge's raw verdict and must not make the aggregate provenance ambiguous.
- Evidence: `judge_outputs/{paper,math,execution}.md`, `judge_outputs/{paper,math,execution}.grounding.json`, and `judge_outputs/aggregate.json`, generated between 08:05 and 08:20 UTC at snapshot `961e5d6bda5a14363aa65c318e90c92362e070bc3c91f0316dc82eeb1a323b6d`.
- Acceptance criteria:
  1. Each role's raw verdict, protocol-validation result, grounding result, and effective verdict are separate explicit fields.
  2. Grounding uses the manifest-bound canonical chunk bytes or a documented normalization applied identically to prompt generation and validation.
  3. A failed quote reports the ref id, chunk id, normalized candidate, and a bounded nearest-match diagnostic.

## TF-RUN4-PACKET-010 — Execution packet omits the canonical Q2/Q3 result chain

- Status: OPEN
- Observed: 2026-08-19
- Severity: HIGH (forces `PACKET_REBUILD` even though the execution judge itself ran successfully)
- Actual result:
  - The execution role completed normally but returned `VERDICT: INDETERMINATE` because its manifest was `INCOMPLETE` and `eligible=false`.
  - Missing claim coverage includes `Q2_SIC_SHARED_THICKNESS_AND_RELIABILITY`, `Q3_MULTIBEAM_DIAGNOSIS_AND_SI_RESULT`, `Q3_CONDITIONAL_SIC_CORRECTION`, and all four final Q2/Q3 result-table claims.
  - The packet excluded the canonical `results/problem2/values.json` and `results/problem3/values.json` chain, while the allowed evidence also records a failed derived-artifact generator and receipt provenance conflicts (`SUBMITTED_CODE_OR_INPUTS_CHANGED`).
  - `judge_outputs/decision_route.json` correctly maps this unmet coverage to `PACKET_REBUILD`; Final Audit persists `judge_completed=true`, `delivery_allowed=false`, `resume_after_step=13`, and `decision=PACKET_REBUILD`.
- Expected result: packet construction should either include every required canonical claim source and its provenance or fail before judge dispatch with a structured list of missing claim routes. It should not spend all three model calls only to discover statically knowable manifest incompleteness.
- Acceptance criteria:
  1. Required claim coverage is validated before invoking judges.
  2. Canonical Q2/Q3 result values, table derivations, and receipt provenance are included together or explicitly marked unavailable.
  3. A packet rebuild produces a new manifest fingerprint and re-runs only roles affected by changed packet inputs.

## TF-RUN4-STATUS-011 — Suppressed rewind is exposed as an undifferentiated failure

- Status: OPEN
- Observed: 2026-08-19
- Severity: MEDIUM (the technical-validation objective completes, but operators see only a generic failure)
- Actual result: revision 2484 correctly records `PERMANENT_TECHNICAL_FLOW_REWIND_SUPPRESSED`, `judge_completed=true`, `decision=PACKET_REBUILD`, `resume_after_step=13`, and `delivery_allowed=false`; however, the durable project state is only `status=failed`, Stage 10 / Step 16, with no pending action and no runner.
- Expected result: status/API projections should expose the terminal reason, completed judge state, requested rewind target, and the fact that rewind was intentionally suppressed. The UI must distinguish this audited technical-validation terminal from a crashed worker or an unhandled Step 16 failure.
- Acceptance criteria:
  1. The project summary exposes `reason_code=PERMANENT_TECHNICAL_FLOW_REWIND_SUPPRESSED` and `requested_resume_after_step=13`.
  2. The UI displays “终审已完成；按技术验证策略禁止回退” rather than a generic “失败”.
  3. The final snapshot id, aggregate decision, three role outcomes, and delivery-disabled state are linked from the terminal status.

## TF-RUN4-TEST-012 — Byte-identical verifier test disagrees with worktree path normalization

- Status: FIXED-RUNTIME-VERIFIED
- Observed: 2026-08-19
- Severity: LOW (test-only failure in the required isolated-worktree validation environment)
- Actual result: `scripts/verify_numbers.py` intentionally canonicalizes a `.worktrees/<name>/...` paper path to the main repository path so legacy CLI output remains byte-identical across worktrees, but `test_cli_output_byte_identical` substituted the unnormalized fixture path into its golden output.
- Fix deployed: the test now applies the same platform-aware `.worktrees` display-path normalization before replacing `{FIXTURE}`; production verifier behavior is unchanged.
- Verification: the focused verifier module reports `2 passed`; the final complete suite reports `1120 passed` with four pre-existing Pydantic alias warnings, and the frontend production build succeeds with Vite.
