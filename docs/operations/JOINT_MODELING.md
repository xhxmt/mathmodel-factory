# Optional Claude Fable + GPT Pro modeling

This is an active implementation contract. The feature is **off by default**
and must be selected independently for each native project. Deployment,
existing artifacts, the ordinary `--consult` option, and model configuration
defaults do not enable it.

## Enable and run

In Web, create/request the project with “仅创建，不自动开始”. Once created,
open its overview, explicitly enable joint modeling, then start it. The switch
requires a stopped READY/PAUSED native project, no pending human request, and
no Step 2 execution history. An already-started candidate run cannot change
its modeling mode; use a new project run when a different mode is needed.

CLI from the factory checkout (replace the project and revision):

```sh
python -m factory_core.cli joint-modeling ongoing/example status
python -m factory_core.cli joint-modeling ongoing/example enable --expected-revision 1
python -m factory_core.cli joint-modeling ongoing/example consultation
```

`disable` uses the same revision and lifecycle checks. Configuration is a
`JOINT_MODELING_CONFIGURED` ledger event; no global flag or schema migration is
needed. The existing CLI `resolve` JSON path accepts consultation answers and
the existing `scripts/selection_gate.py select-step3` path remains supported.

## Workflow and evidence

1. Step 2 uses fresh Claude calls pinned to `claude-fable-5-1` for proposals and
   critiques. Missing executable, incompatible `CODEX_ONLY`, rejected model,
   invalid output, or reported alternate model fails rather than falling back.
   At least two candidates must satisfy the current validated-candidate contract.
2. Step 3 opens `joint_modeling_candidates`. Web exports one UTF-8 text file
   containing the prompt and complete textual manifest (problem Markdown/JSON/
   text, research brief, candidate specs, demos and critiques). This is not a
   transfer of every original binary attachment. The user supplies that file in
   a new ChatGPT conversation, manually selects Pro, and pastes its full JSON.
3. The answer echoes request ID/generation, subject hash, package hash and nonce.
   Every finding names a candidate and an exact quote in a manifest file. The
   user explicitly confirms the new conversation, absence of old context,
   complete materials, and unchanged answer. Invalid/stale replies do not
   resolve the request. Model identity is `HUMAN_ASSERTED_UNVERIFIED`; the system
   does not claim that it authenticated a particular ChatGPT model or tier.
4. Claude synthesizes every finding once as ACCEPT, PARTIAL or REJECT, including
   rationale, a grounded Pro quote, proposed change and need for human judgment.
   This call has no tools. Its captured reply, Pro receipt and current candidates
   are bound to the existing Step 3 human selection. There is no automatic
   selection on timeout. Native selection retains deterministic projections.
5. Before Step 5, `joint-risk-v1` requires another Pro review when the selected
   primary is outside Pro recommendations, an auxiliary model is merged, a
   finding requests follow-up, synthesis requests human judgment, a HIGH finding
   is not accepted, or `model.md` differs from the chosen candidate specification
   after outer whitespace trimming. The last comparison is deliberately
   conservative and will also trigger for many expanded full specifications.
   The risk package binds the complete model and its current contracts. After
   reading the reply, the user must explicitly approve that exact specification
   before solving. A second review does not automatically rewrite the model.

The manual CLI resolution body includes `gate`, `answer` (the original JSON
text), `request_id`, `generation`, `subject_fingerprint`, `options_fingerprint`,
and an `attestations` object. Its four boolean keys are `new_conversation_used`,
`no_old_project_context`, `exact_upload_manifest_used`, and
`copied_without_editing`. The risk gate additionally requires
`selected_model_spec_approved`. The consultation view provides the exact current
identity and required keys. All must be explicit `true`.

Immutable evidence lives under `.factory/joint_modeling/` and existing decision
receipts; `.factory/state.db` owns accepted decisions and lifecycle state.
General `human_review.md` and judge preambles receive an advisory receipt notice
instead of raw Pro opinions. Modeling steps receive the bound synthesis.
Advice alone never grants final audit, release or delivery approval.

## Refresh, runtime and recovery

Web can regenerate a stale pending request from current materials. Old request
generations remain historical, and old replies cannot resolve the new request.
A changed candidate must first have valid pinned Claude execution evidence;
refresh does not invent a receipt. Changing materials after an accepted review
requires the existing authorized workflow reopen/recovery path.

The executor uses `CLAUDE_CLI_PATH`, PATH, or `~/.local/bin/claude`. It requires
the installed CLI/account to support the exact configured model ID. No paid
provider call is needed to deploy the code or to keep the feature disabled.
Requests are sent on stdin, bounded by the existing attempt/deadline contract.
Execution receipts preserve CLI-reported model IDs; this is routing evidence,
not independent provider authentication.

Each call reserves a per-purpose inflight record. Known completion releases it.
If a process dies before a completion receipt, later attempts fail with
`PERMANENT_MODELING_DISPATCH_UNCERTAIN` and the original call ID. An operator
must first verify that the old runner/child process is stopped and inspect that
call's `intent.json`, log and any `result.json`. Preserve the evidence; after
confirming there is no in-flight call, move only the matching inflight record
to an operator recovery archive before using normal resume/recovery. Never
clear all reservations or infer completion from an absent runner PID.

## Verification scope

Focused tests cover default-off behavior, project isolation, revision conflicts,
model routing and captured output, stale/tampered answers, exact quotes,
synthesis coverage, manual selection, conditional risk approval, shared CLI/
SQLite decisions, ACL checks and native Stage pause/resume. Frontend build and
browser interaction checks use mocked providers. Live model availability and
scientific quality require a separately initiated, opt-in project run.
