# Run4 downstream technical-flow validation

Date: 2026-08-19 (UTC)

Project: `cumcm_2025_b_codex_luna_stability_20260817_run4`

Scope: validate the Step 13–16 scheduler, Human Gate, Final Judge, grounding, aggregation, decision-routing, and terminal-state paths. This run does not assert paper scientific quality and does not authorize delivery.

## Terminal state

- SQLite revision: `2484`
- State: `failed`, Stage 10 / Step 16 / `delivery`, no runner, no pending action
- Terminal event: `PERMANENT_TECHNICAL_FLOW_REWIND_SUPPRESSED`
- Final snapshot: `961e5d6bda5a14363aa65c318e90c92362e070bc3c91f0316dc82eeb1a323b6d`
- Final Audit: `judge_completed=true`, `decision=PACKET_REBUILD`, `resume_after_step=13`, `delivery_allowed=false`
- Interpretation: all requested downstream components ran. The genuine Final Audit requested a Step 13 packet rebuild; the run-scoped technical controller retained that result and terminated instead of rewinding to Step 0–12.

## Runtime policy verified

- Remote Codex service tier: non-Fast (`CODEX_SERVICE_TIER` absent; controller audit records `non_fast=true`)
- Solver backend: `local`
- `CODEX_ONLY=1`
- Judge policy: `enforce`
- Cloud Run: disabled
- Step 16 judges: three isolated `gpt-5.6-luna`, `model_reasoning_effort=xhigh`
- No quality PASS or delivery authorization was synthesized.

## Final Judge outputs

| Role | Raw verdict | Effective aggregate status | Key result |
| --- | --- | --- | --- |
| Paper | PASS | INDETERMINATE | Four cited quotes failed grounding with `QUOTE_NOT_FOUND`. |
| Math | PASS | INDETERMINATE | Two cited quotes failed grounding; the role also reported a non-fatal polarization-q aggregation risk. |
| Execution | INDETERMINATE | INDETERMINATE | Packet coverage was incomplete for the canonical Q2/Q3 result and final-table chain. |

The policy router mapped the incomplete execution packet to `PACKET_REBUILD`. The visual gate was PASS but did not override the packet decision.

## Workflow defects

The detailed evidence and acceptance criteria are in `technical_flow_bug_ledger.md`.

- Fixed and runtime-verified: TF-RUN4-CTRL-002, CORE-003, SCHED-004, BUNDLE-005, GOV-006, AUDIT-007, PREFLIGHT-008, and TEST-012.
- Open: TF-RUN4-FE-001, JUDGE-009, PACKET-010, and STATUS-011.
- Frontend mismatch: the Selection page failed to render a valid pending content-freeze request. The final durable state also exposes the intentionally suppressed rewind only as a generic failed state unless clients inspect the event/audit evidence.

## Independent verification

- Focused changed-contract suite: `122 passed in 3.02s`
- Worktree-path verifier regression: `2 passed in 0.12s`
- Frontend runtime helper suite with the repository's existing locked dependencies: `27 passed in 5.21s`
- Complete pytest suite with the repository Python environment and locked frontend dependencies: `1120 passed, 4 warnings in 105.46s`
- Warnings: four existing Pydantic `UnsupportedFieldAttributeWarning` instances for `base_name` and `download` aliases in showcase ACL tests.
- Frontend production build: Vite `169 modules transformed`, build succeeded in `1.34s`
- `git diff --check`: PASS

## Evidence hashes

- Controller: `6ca7e86033ef5971c225e8feafe174868c819d35ce67702806d03facc00e2797`
- Technical bug ledger: `91cecbe750d300cfddd0ae1ee4a078d8fbac8825f35258615a9f2dcef31dc70d` (hash before this report was added; ledger content itself is unchanged by report creation)
- Paper judge: `f907ab2eb9db83e921c12cb3855593f055f81d9104664151bc3ee3acac0a9fb7`
- Math judge: `7030b6d027d8ff420fb947899af231ab58db11168623843a00dfeb49c4412c54`
- Execution judge: `2f83a48e767017909296c34759e3d187bec4ec09668c1526735b752d7554ff69`
- Aggregate: `e194895d7d0f9e4a268294e637b7e8c53fb8b98a03df5cd71182e9dc14ad1c63`
- Decision route: `7fb01c6d486f8a308a4e0c7bb36111fb98da679185bd64773c18ce645e93f583`
- Latest Final Audit: `6a2b78e2aa007d3ecad0b79aa58458f07c79eaf9c5121261654210ecba4c4504`

## Repository state

- Worktree is detached at `4d2eb32e07eb104326d6b1f0f90e581b46ad4a99`, equal to `origin/main`, local `codex/stage-scheduler-simplification`, and `origin/codex/stage-scheduler-simplification` at inspection time.
- The validation/fix changes are uncommitted working-tree modifications.
- Nothing from this validation was committed, pushed, merged again, deployed, or used to modify production configuration or data.
