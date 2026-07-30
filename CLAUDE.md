# CLAUDE.md

This file gives coding-agent guidance for this repository.

## What This Is

This checkout is a local Modeling Factory: a Python orchestrator with a frozen Bash step adapter that
takes a math-modeling competition problem and drives a multi-agent 16-step
workflow to produce a finished paper PDF and supporting artifacts.

The active domain is CUMCM / MCM / ICM style applied mathematical modeling, not
the original economics/sociology Paper Factory. Legacy social-science prompts
and Stata helpers remain as historical reference, but their execution path is retired. New modeling work
must follow `STEPS.md` and `modeling_guide.md`.

There are three relevant audiences for code in this repo:

1. The control plane: `factory_core/`, `launch_agents.sh`, the `run_paper.sh` compatibility launcher, CLI, and Web.
2. The prompts each agent reads: `prompts/step*.txt`, `STEPS.md`, `modeling_guide.md`, `method_library/`.
3. The launched agents, which write markdown, Python/solver code, LaTeX, figures, tables, and result files inside project directories under `ongoing/`.

`README.md` is the user-facing intro. `STEPS.md` is the canonical step contract.
`modeling_guide.md` is the project-local style and execution contract. If
`analysis_guide.md` is also present, it is legacy context and does not override
`modeling_guide.md`.

## Common Commands

User-facing CLI from repo root:

```bash
./launch_agents.sh new [--no-start] [--consult] <base> "/abs/path/to/problem.pdf"
./launch_agents.sh resume <base> [<base2> ...]
./launch_agents.sh <base1> [<base2> ...]
./launch_agents.sh run <base>
./launch_agents.sh pause <base>
./launch_agents.sh consult <base>
./launch_agents.sh status
./launch_agents.sh attach <base>
./launch_agents.sh trace <base> [--lines N] [--follow]
```

Direct runner invocations:

```bash
./run_paper.sh <project_dir>
./run_paper.sh --infer-step <project_dir>
./run_paper.sh --status <project_dir>
```

Inside a project directory:

```bash
../../solver_submit.sh --type python --max-time 600 models/m3_milp/03_solve.py
../../solver_submit.sh --status <jobid>
../../solver_submit.sh --wait <jobid>
../../compile_paper.sh "$(pwd)" <base_name>
python3 ../../scripts/verify_numbers.py "$(pwd)" <base_name>
```

`solver_submit.sh` supports `python`, `julia`, `matlab`, `R`, and `gurobi` when
the corresponding executable is installed. Use explicit `--max-time` for all
nontrivial jobs.

## Architecture

### Engine And Compatibility Launcher

`launch_agents.sh` is a compatibility CLI that forwards new engine projects to
`FactoryService` and uses the thin `run_paper.sh` compatibility launcher for
foreground execution. New projects initialize `.factory/state.db` and run
through the native Step 0-16 registry. Existing projects remain on the frozen
Legacy Runner until an explicit, conflict-free migration report is applied.

`factory_core/` owns revisioned state transitions, append-only events, retry
budgets, recovery decisions, pending actions, Step/backend registration,
application commands, and solver jobs. Native Steps implement
`prepare/execute/validate/recover` and do not invoke the frozen Bash runner.
The historical implementation lives at `factory_core/adapters/legacy_runner.sh`
for unmigrated and explicitly rolled-back projects only.

The Legacy Adapter still snapshots itself under `logs/runner_snapshots/` so an
active Step is insulated from edits. Do not add new scheduling, retry, recovery,
or state logic to the adapter.

### State And Artifact Authority

For new and migrated projects, `.factory/state.db` is the workflow-state source
of truth. A transaction appends an event and updates the snapshot with a
monotonic `revision`. `checkpoint.md`, heartbeat, marker, PID, and diagnostics
files are compatibility projections and must not be used to overwrite SQLite.

Artifacts defined by `STEPS.md` remain validation evidence. Recovery calls the
registered Step validator: valid artifacts promote the interrupted Step;
invalid artifacts retry it. File modification times do not determine the
authoritative state.

For unmigrated modeling projects only, `infer_step()` in the frozen Legacy
Adapter continues to infer progress from artifacts. `run_paper.sh --infer-step`
routes to SQLite or Legacy inference without changing its public output.

Modeling-mode is detected by the `problem/` directory after setup. The runner
then uses the modeling branch of `infer_step()` and the modeling Step 1-16
contracts.

### Human Consultation Window (opt-in)

Lets a human inject GPT Pro / Gemini Deep Think conclusions into the otherwise
autonomous pipeline. **Off by default** — enable per project with
`new --consult` (writes `consultation/enabled`) or env `CONSULT_ENABLE=1`. When
off, prompts and behavior are byte-identical, so unattended benchmark/ablation
runs are unaffected.

`maybe_consult <gate> <step> <title>` in the Legacy Adapter is the gate primitive.
Three gates: **preflight** (after Step 0 parsing, before Step 1), **step4**
(before full model construction), and **dynamic** (an agent writes
`consultation/REQUEST.md` and stops when it hits a hard call). A gate that is
not yet resolved writes `consultation/<gate>_request.md`, seeds a `## CONSULT
<gate> … STATUS: AWAITING` section in `human_review.md`, notifies, and **exits
the runner cleanly (`exit 0`)** — never a blocking wait, because the activity
monitor would treat a waiting process as a hang and a live process holds the
lock. The human pastes the answer under that section, flips `STATUS: READY`,
and `resume`s; the gate then proceeds (agents read `human_review.md` at highest
priority via the prompt preamble). `consult <base>` prints the pending request;
`status` shows `CONSULT(<step>)`. Telegram push is a best-effort hook, disabled
unless `CONSULT_TELEGRAM=1`; terminal notification always fires.

### Prompt Rendering

Prompts live in `prompts/step*.txt`. `render_prompt` prepends a common preamble:
read the project style guide, prefer `modeling_guide.md`, read
`human_review.md` if present, and do not reuse completed projects. It
substitutes:

- `__PROJECT_PATH__`
- `__RESEARCH_QUESTION__`
- `__BASE_NAME__`
- `__FACTORY__`
- Step-specific placeholders such as `__STREAM_ID__`

Optional researcher notes can be supplied through `web/notes.json` keyed by
base name and step.

### Agent Dispatch

Step functions call primitives such as:

- `run_codex`
- `run_claude_worker`
- `run_claude_then_codex`
- `run_codex_then_claude`
- `run_codex_parallel`
- `run_agy`

Hang detection watches trace-file freshness, but solver children count as real
work. The process whitelist includes Python, Julia, MATLAB, R, Gurobi, CPLEX,
SCIP, IPOPT, Octave, and legacy Stata names.

## Active Modeling Workflow

See `STEPS.md` for exact outputs and line/file gates. In short:

- Setup / Step 0: parse a competition problem into `problem/`.
- Step 1: background research, candidate methods, viability gate.
- Step 2: parallel modeling proposals, demo solves, critic verdicts.
- Step 3: method selection, with `human_review.md` override support.
- Step 4: full model construction, symbol table, assumption ledger, runnable code.
- Step 5: full solve through `solver_submit.sh`.
- Step 6: sensitivity and robustness.
- Step 7: model evaluation.
- Step 8: visualization polish.
- Step 9: full paper draft with `ABSTRACT_PLACEHOLDER`.
- Step 10: Gate 1 numerical and code consistency check.
- Step 11: constructive review.
- Step 12: revision and archive of the pre-revision draft.
- Step 13: provisional Gate 2 via isolated math, execution, and paper roles.
- Step 14: abstract replacement.
- Step 15: citation audit, table/prose polish, de-robotification; these edits invalidate the provisional judge fingerprint.
- Step 16: compile a fresh PDF, rerun Gate 2 on the post-Step-15 three-role text packets, bind the PASS, prompts, evaluator implementation, Step-13 model routing, and those exact PDF bytes to `judge_outputs/final_submission.sha256`, then write the `final_judge_v3` delivery contract, copy, package, cleanup, and move to `complete/`. The hash proves delivery consistency; the automated reviewer does not inspect rendered PDF pixels, so layout and visual quality require machine preflight or human review. Only an unchanged evaluator+packet+PDF fingerprint may reuse the final PASS; compilation failure stops delivery.

Gate 2 verdict tokens in modeling mode are:

- `VERDICT: PASS`
- `VERDICT: REOPEN_REVISION_TEXT`
- `VERDICT: REOPEN_REVISION_MODEL`

Math and execution use the hard three-valued state `PASS / FAIL / INDETERMINATE`. Paper six-dimension scores are conditional: they are comparable only when both hard roles PASS and every role output satisfies `judge-role-v1`. A hard FAIL, missing evidence, malformed output, or INDETERMINATE state must not be averaged into a score.

The runner allows one repair cycle. If the reopened or final-submission judge still does not PASS, normal delivery is blocked. Legacy Markdown scorecards are `LEGACY_UNVERIFIED` and are never comparison-ready under the current contract.

## Cross-Step State

Important project files include:

- `checkpoint.md`: status display only; not authoritative.
- `problem/*.md`: parsed problem, constraints, data inventory, candidate methods.
- `viable_streams.md`, `m<N>_spec.md`, `m<N>_critique.md`: Step 2 stream state.
- `method_decision.md`, `chosen_method.md`: selected primary/auxiliary method.
- `model.md`, `symbol_table.md`, `assumption_ledger.md`: modeling state.
- `solve_log.md`, `results/**`: numerical evidence.
- `sensitivity_report.md`, `evaluation.md`, `visualization_log.md`: downstream evidence.
- `audit_issue_ledger.md`: issue status tracker. Blocking issues must not be silently dropped.
- `judge_evaluation.md`: current `judge-aggregate-v1` Gate 2 control file.
- `judge_packets/**`, `judge_outputs/**`: isolated evidence manifests, strict role outputs, aggregate JSON, and final-submission fingerprint. Each manifest carries `judge-packet-completeness-v1`; required evidence that is missing, truncated, or omitted forces the role to `INDETERMINATE`, while non-critical truncation must remain visible in `limitations`.

Protected assumptions or issues must not be deleted or downgraded without a
clear evidence-backed reason.

## Solver Execution Model

Use `solver_submit.sh`, not ad hoc background jobs, for nontrivial runs:

```bash
../../solver_submit.sh --type python --max-time 1800 models/m3_milp/05_sensitivity.py
```

It writes metadata under `run_state/solver_jobs/<jobid>.meta`, stdout next to
the script as `<script>.log`, and stderr under the script directory's `logs/`.
Agents should move or reference logs as needed in project evidence files.

Legacy `stata_submit.sh` is retained only for historical projects.

## Figures, Tables, And LaTeX

Follow `modeling_guide.md`:

- Figures: academic palette, self-contained captions, PDF plus PNG when useful.
- Tables: `booktabs`, right-aligned numeric columns, compact labels.
- Symbols: every variable and parameter used in the model must appear in `symbol_table.md` and the paper's symbol table.
- LaTeX: CUMCM/MCM-style sections, with abstract filled only at Step 14.
- Compilation: use `compile_paper.sh`; it selects `xelatex` for `ctex`, `cumcmthesis`, `mcmthesis`, or `xeCJK`.

## Web Control Plane Contract

The stable ASGI entry is `apps.web.backend.main:app`. During the repository
boundary transition, `web/backend/main.py` contains the FastAPI implementation;
`web/backend/app.py` is only a compatibility launcher/re-export.

Authentication and approvals are persisted in SQLite at `web/auth.db` through
`web/backend/auth_store.py`. Passwords are bcrypt hashes. Registration creates
a pending user; administrators approve users and project requests. A non-admin
user sees and manages only projects granted through `project_acl`, while an
administrator can manage all projects. Unauthenticated visitors can only use
the read-only showcase configured by `SHOWCASE_PROJECTS`.

Engine project creation, lifecycle controls, and solver policy call
`FactoryService` directly. FastAPI owns authentication, ACL checks, and HTTP
error mapping; it does not route these commands through shell subprocesses.

The Web project list exposes `problem_key`, `problem_title`, `storage_scope`,
`archived`, and the workflow `revision`. Equivalent contained problem statements share a canonical
SHA-256 identity so the frontend can group multiple runs into one problem
archive. This grouping does not move or rename project directories:
`ongoing/` and `complete/` remain authoritative storage.

Production secrets are loaded from GCP Secret Manager by
`scripts/load_secrets.sh`. `JWT_SECRET` (at least 32 characters) and a strong
`ADMIN_PASSWORD` are mandatory; startup rejects missing or weak values. Never
document a default password, automatic JWT generation, secret value, or secret
prefix. `web/README.md` owns current usage and
`web/docs/deployment/DEPLOYMENT.md` owns current production operations.

## Editing Notes

- Runtime directories (`ongoing/`, `complete/`, `papers/`, `logs/`, `run_state/`) and project `.factory/` databases are gitignored.
- Do not commit `.env`, credentials, generated logs, PDFs, or benchmark downloads.
- Inspect every worktree before cleanup. Do not remove a worktree, branch,
  backup, gitlink, log, or local credential file until its exact contents and
  recovery value have been reported and the user has explicitly approved the
  destructive action.
- `run_paper.sh` must remain a thin compatibility launcher. Existing Step shell changes belong in the frozen adapter; new scheduling behavior belongs in `factory_core/`.
- Root `pyproject.toml`/`uv.lock`, hash-locked Web/Cloud exports, and frontend `package-lock.json` own dependency resolution. Runtime start scripts must not install packages.
- Do not change `STEPS.md`, `modeling_guide.md`, or active prompts casually; they are agent contracts.
- Keep historical prompt/data files unless deletion is explicitly approved. They are not an executable compatibility promise.
