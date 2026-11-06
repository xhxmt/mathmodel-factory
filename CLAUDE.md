# CLAUDE.md

This file gives coding-agent guidance for this repository.

## What This Is

This checkout is a local Modeling Factory: a Python orchestrator with a frozen
Bash compatibility adapter that takes a math-modeling competition problem
through 10 persistent scheduler Stages while retaining 17 Step contracts
(Step 0–16), producing a finished paper PDF and supporting artifacts.

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
python3 -m factory_core.cli audit <project_dir>
python3 -m factory_core.cli preflight [<project_dir>] [--steps 0-16] [--offline]
python3 scripts/replay_run_history.py            # liveness replay of tests/fixtures/run_history
```

Inside a project directory:

```bash
../../solver_submit.sh --type python --max-time 600 \
  --input data/final/instance.json \
  --output results/problem1/values.json \
  --seed 20260804 \
  models/m3_milp/03_solve.py
../../solver_submit.sh --status <jobid>
../../solver_submit.sh --status <jobid> --json
../../solver_submit.sh --wait <jobid>
../../compile_paper.sh "$(pwd)" <base_name>
python3 ../../scripts/verify_numbers.py "$(pwd)" <base_name>
```

`solver_submit.sh` supports `python`, `julia`, `matlab`, `R`, and `gurobi` when
the corresponding executable is installed. Use explicit `--max-time` and
repeated `--input` / `--output` / `--seed` declarations for all nontrivial jobs.
Only `--status <jobid> --json` with `solver-job-evidence-v2` and
`receipt_ready=true` proves the submitted code/input identity and current
declared-output hashes; it does not prove optimality.

## Architecture

### Engine And Compatibility Launcher

`launch_agents.sh` and `run_paper.sh` forward to FactoryService and FactoryEngine.
The only active workflow is `engine` / `native_v2` / `stage_v1`, backed by
`.factory/state.db`. Ten persistent Stages schedule the existing Step 0–16
validation contracts. Existing `step_v2` Native projects require explicit
`factory migrate scheduler-activate`; Legacy migration and rollback are retired.

`factory_core/` owns revisioned transitions, events, retry budgets, recovery,
pending actions, native Stage/Step registration, audit, delivery and solver jobs.
The full pre-split source and experimental development contracts live separately
in `~/paper_new`. Do not copy experimental tables into a Native database.

New projects persist a 74-hourNew projects persist a 74-hour contest policy in schema-v9 SQLite. Steps 0–15
are capped at T−6h content freeze; Step 16 owns the six-hour terminal reserve
and is capped at the final deadline. T−2h is delivery freeze: any audit-driven
substantive reopen requires a separate human override. Retry sleeps are also
budget checked. Historical/migrated projects without a contest-policy row stay
unbounded for compatibility; do not synthesize an expired deadline for them.

Schema v9 represents every Human Gate occurrence as an immutable request
(`request_id`, gate, generation, subject/options fingerprints) and one optional
append-only decision instance. A rejected Approval remains historical evidence,
but content-freeze rejection clears the pending action, invalidates downstream
checkpoints, records `WORK_REOPENED`, and returns to Stage 9. The next
generation is created only when repaired work reaches the Gate again. Never
infer approval from the existence of a decision row or a selected option
string. Current Approval decisions also require a verified immutable receipt;
missing, symlinked, hash-mismatched, or identity-mismatched evidence fails
closed.

Experimental Authority/Phase sources and historical runners are preserved in
`~/paper_new`; see `docs/architecture/NATIVE_MAINLINE.md`. They are not imported
by this mainline, and their databases are rejected without automatic downgrade.

### State And Artifact Authority

For new and migrated projects, `.factory/state.db` is the workflow-state source
of truth. A transaction appends an event and updates the snapshot with a
monotonic `revision`. `checkpoint.md`, heartbeat, marker, PID, and diagnostics
files are compatibility projections and must not be used to overwrite SQLite.

`TransitionCoordinator` is the target application-writer boundary, not an
achieved unique-writer guarantee. At the current baseline, compatibility
decision recording, request supersede, prompt-attempt input binding, projection
failure bookkeeping, bootstrap/migration, and archive relocation still include
direct `SQLiteStateStore` writes. Do not add another bypass or claim exclusivity;
use the characterized inventory and future gate specification in
`docs/architecture/application_writer_allowlist_v1.json`.

Artifacts defined by `STEPS.md` remain validation evidence. Recovery calls the
registered Step validator: valid artifacts promote the interrupted Step;
invalid artifacts retry it. File modification times do not determine the
authoritative state.

Artifact authority is three-layered. Authored problem/model contracts,
structured decisions, canonical results, `paper.tex`, and the issue ledger are
business truth. Solver/audit receipts, hashes, fingerprints, and final
acceptance are immutable machine evidence. Checkpoint, method summaries, solve
logs, verification summaries, and Web status are rebuildable projections.
Step-3 and content-freeze decisions are read from SQLite first; their JSON and
Markdown forms are projections. For current Native projects,
`chosen_method.md` is generated from the immutable SQLite Step-3 decision and
`method_decision.md` carries the same machine-verifiable identity header. Step 3
validation and Step 4 prepare both verify the receipt, current candidate
fingerprints, and every projection identity field; `human_review.md` cannot
override them. Consultation decisions likewise store the exact answer in
SQLite, deterministically rebuild their `human_review.md` section, inject the
verified answer into the effective model prompt, and bind that prompt plus Web
researcher notes by SHA-256 in the Step result/checkpoint evidence.

Artifact responsibility is centralized in
`factory_core/artifact_ownership.py`. Dirty classification, semantic reopen,
Finalization recovery, Judge missing-evidence routing, Web diagnostics, and
final/submission manifests must consume that registry rather than add local
path-owner conditionals. Scheduler/control-mode rollback shares one guard that
rejects active attempts, unresolved dirty/projection state, pending Finalization
snapshots, unfinished human decisions, and any current manifest drift from
`stage_cursor_input`. Dirty state is keyed by `(flag, owner_stage)` so one
domain cannot overwrite another Stage's active cause. Final/submission
collection fails closed when a tracked authored file has neither registry
ownership nor an explicit active-LaTeX or declared-deliverable route.

Native failure events preserve execution and validation metadata such as the
failed check, role, backend, report, and missing artifact paths. A model process
that exits zero without producing the required artifact is
`TRANSIENT_ARTIFACT_MISSING`, not an unclassified success. Permanently rejected
model routes are quarantined in the worker and a healthy configured fallback is
tried; a missing API credential or unsupported model must not be invoked again
on every workflow retry. When every candidate fails, the dispatcher reports the
most explanatory one (a provider signal such as an exhausted balance in its log
first, a fallback that could not launch last) and lists all of them in
`candidate_attempts`.

An incremental-audit check that ends without a verdict (timeout, signal, a log
ending in an uncaught traceback, or a last line naming an exhausted host
resource such as a full disk) is `INDETERMINATE` / `TRANSIENT_INCREMENTAL_AUDIT`,
never a finding, and leaves its ledger row as the last verdict recorded it; a
determinate finding in a sibling check still wins. Error classes in
`engine.NON_CONTENT_ERROR_CLASSES` never consume a Step's content attempts:
the engine derives their count from the event stream (no new replayed state
field) and stops after `INFRASTRUCTURE_RETRY_BUDGET` retries with a distinct
terminal class (`PERMANENT_AUDIT_INFRASTRUCTURE`).

`run_paper.sh --infer-step` reads Native SQLite state only. Projects without
Native state remain historical artifacts and cannot execute here.

### Audit And Delivery Boundary

Step 4, Step 5/6, and Step 10 run cacheable `model`, `results`, and `paper`
profiles. Their records live under
`.factory/audits/profiles/<profile>/<snapshot>/`, synchronize deterministic
failures into `audit_issue_ledger.md`, and always set `delivery_allowed=false`.
Step 15 is the `CONTENT_READY` boundary. The `final` profile owns release
acceptance checks, compilation, visual inspection, isolated judge execution,
decision routing, snapshot fingerprints, and judgment receipts. Final records
are stored under `.factory/audits/<snapshot>/`; current `judge_outputs/` files
remain compatibility projections.

`python3 -m factory_core.cli audit <project>` runs that subsystem independently
in analysis-only mode. It may write audit/Judge evidence, but must not create
`final_submission.sha256`, an override receipt, or a final acceptance receipt,
publish into `papers/`, package, clean, archive, or mutate SQLite workflow state.
Contest-policy and approval-fingerprint reads use a private main/WAL snapshot,
without upgrading the source schema, changing journal mode, or writing source
WAL/SHM files. Unsupported or incomplete approval state blocks analysis.
Analysis results use `.factory/audits/analysis_latest.json`; rerunning analysis
for an accepted snapshot does not replace its acceptance-authority `latest.json`.
Step 16 is a compatibility adapter: Native explicitly selects acceptance mode;
The CLI supports `audit --accept-delivery` for explicit acceptance. Only after that boundary may a
Native `PASS` or explicit `OVERRIDDEN` result be published and packaged.
Experimental databases are rejected by the Native boundary.
Final analysis ordering is compile → full paper/provenance checks → visual/page
gate → packets/fingerprint → enforce-mode three-role Judge → snapshot recheck →
judgment receipt; acceptance adds the final-submission marker and final acceptance receipt.
Audit failures return
structured repair hints to the engine; the audit subsystem does not directly
rewind workflow state.

Delivery override authority lives only in `web/auth.db`. The scopes are
`continue_after_gate2` and `deliver_snapshot`; the latter must bind the exact
64-character final snapshot and is consumed when the final acceptance is
recorded. A project-local `gate2_delivery_override.json` never authorizes
anything. This is an operational boundary for the single-operator deployment,
not cryptographic isolation from another process running as the same Unix UID.
The same control database owns identity and `project_acl`, but none of those
records replaces project-local workflow decisions in `.factory/state.db` or
advances its scheduler. Conversely, a project decision never grants Web access
or an override. The two databases have different trust scopes and lifecycles.

Step 16 publishes immutable releases under
`papers/releases/<base>/<snapshot>/` and atomically replaces only
`papers/<base>/current.json` after all bytes and receipts verify. Flat
`papers/<base>_paper.pdf` and `_submission.zip` files are compatibility aliases,
not release authority. Native adapters use the same
Final Audit and release publisher.

### Human Consultation Window (opt-in)

Lets a human inject GPT Pro / Gemini Deep Think conclusions into the otherwise
autonomous pipeline. **Off by default** — enable per project with
`new --consult` (writes `consultation/enabled`) or env `CONSULT_ENABLE=1`. When
off, prompts and behavior are byte-identical, so unattended benchmark/ablation
runs are unaffected.

Native consultation gates persist immutable decision requests in SQLite and
project request/answer views into `consultation/` and `human_review.md`.
Use `launch_agents.sh consult NAME` to inspect a pending request, then the Web
or CLI decision path to resolve it. Verified decisions are embedded in prompts;
projection files alone cannot authorize workflow advancement.

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

- Setup / Step 0: parse a competition problem into `problem/`, generate the
  validated `problem-plan-v1` dependency DAG, and run hierarchical method
  retrieval across the curated and authorized HMML registries.
- Step 1: background research, candidate methods, viability gate.
- Step 2: parallel modeling proposals, demo solves, critic verdicts.
- Step 3: Human Gate 1 selects PRIMARY/AUXILIARY from validated demo solves; SQLite is authoritative and `human_review.md` is a projection.
- Step 4: full model construction, symbol table, assumption ledger, runnable code, then the `model` audit profile.
- Step 5: full solve through `solver_submit.sh`, then the Step-5 `results` audit checkpoint.
- Step 6: sensitivity and robustness, then the Step-6 `results` audit checkpoint.
- Step 7: model evaluation.
- Step 8: visualization polish.
- Step 9: full paper draft with `ABSTRACT_PLACEHOLDER`.
- Step 10: `paper` profile for numerical, code, result, and deliverable consistency; symbol findings are warnings.
- Step 11: constructive review.
- Step 12: revision and archive of the pre-revision draft.
- Step 13: isolated math-only precheck; `PRECHECK_PASS` allows progress but never delivery.
- Step 14: abstract replacement.
- Step 15: citation audit, table/prose polish, de-robotification; these edits make the Step-13 precheck non-final and produce the `CONTENT_READY` boundary.
- Step 16: require Human Gate 2 (`content_freeze`) before execution, then explicitly request acceptance for the independent final-audit result . On a cache miss the audit subsystem compiles a fresh PDF, reruns Gate 2 on the post-Step-15 packets, and binds the decision to the evaluator and exact PDF bytes. Only a Native accepted result may then be copied, packaged, cleaned, and moved to `complete/`; default audit alone performs none of those delivery mutations or acceptance writes.

Step 13 precheck verdict tokens are `PRECHECK_PASS`,
`REOPEN_REVISION_MODEL`, and `INDETERMINATE_REVIEW`. Final-audit Gate 2 verdict
tokens are:

- `VERDICT: PASS`
- `VERDICT: REOPEN_REVISION_TEXT`
- `VERDICT: REOPEN_REVISION_MODEL`

Math and execution use the hard three-valued state `PASS / FAIL / INDETERMINATE`. Paper six-dimension scores are conditional: they are comparable only when both hard roles PASS and every role output satisfies `judge-role-v1`. A hard FAIL, missing evidence, malformed output, or INDETERMINATE state must not be averaged into a score.

Only a substantive math/execution `FAIL` consumes the scientific reopen budget.
Malformed output, quote grounding failure, unavailable judge routing, and an
otherwise indeterminate review retry only the affected audit role and stop as
`PERMANENT_JUDGE_INFRASTRUCTURE` when exhausted. A packet that
names a genuinely absent upstream artifact reopens that artifact's earliest
owning Step; packet truncation or judge uncertainty does not default to Step 4.
All role packets must be eligible before the first model call, including direct
prepared/precheck entry points. If an artifact's registered recovery boundary
is not earlier than the active Step, report `PERMANENT_RECOVERY_TARGET` with the
missing paths instead of emitting an invalid scheduler transition.

The runner allows one repair cycle. If the reopened or final-submission judge still does not PASS, normal delivery is blocked. Legacy Markdown scorecards are `LEGACY_UNVERIFIED` and are never comparison-ready under the current contract.

## Cross-Step State

Important project files include:

- `checkpoint.md`: status display only; not authoritative.
- `problem/*.md`: parsed problem, constraints, data inventory, candidate methods.
- `problem/problem_plan.json`: authored problem-specific scientific DAG;
  deterministic validation and audit fingerprints bind it, while SQLite
  Stage/Step state remains the workflow authority.
- `viable_streams.md`, `m<N>_spec.md`, `m<N>_critique.md`: Step 2 stream state.
- `method_decision.md`, `chosen_method.md`: selected primary/auxiliary method.
- `model.md`, `symbol_table.md`, `assumption_ledger.md`: modeling state.
- `solve_log.md`, `results/**`: numerical evidence.
- `sensitivity_report.md`, `evaluation.md`, `visualization_log.md`: downstream evidence.
- `audit_issue_ledger.md`: issue status tracker. `AUDIT-*` rows are maintained by stage profiles; blocking issues must not be silently dropped.
- `.factory/audits/profiles/**`: non-delivery `model` / `results` / `paper` snapshots and attempts.
- `.factory/audits/latest.json`: current `profile=final` audit record used by delivery.
- `judge_outputs/final_paper_checks.json`: hash-bound final paper/provenance check report.
- `judge_outputs/final_acceptance_receipt.json`: binds the approved snapshot to PDF, checks, visual gate, decision route, judgment or override receipt, and the exact `submission-bundle-manifest-v2` identity.
- `logs/compilation/latex_inputs.json`: compiler-recorder proof that project-local TeX inputs equal the declared `LatexCompileContract` dependency graph.
- `.factory/finalization/submission_bundle_manifest.json`: exact, path-safe ZIP member list with size and SHA-256; unreferenced `paper/` drafts are excluded.
- `judge_evaluation.md`: Step-13 `PRECHECK_PASS` control file until the final audit replaces it with the full aggregate verdict.
- `judge_packets/**`, `judge_outputs/**`: isolated evidence manifests, strict role outputs, aggregate JSON, and final-submission fingerprint. Each manifest carries `judge-packet-completeness-v1`; required evidence that is missing, truncated, or omitted forces the role to `INDETERMINATE`, while non-critical truncation must remain visible in `limitations`.

Protected assumptions or issues must not be deleted or downgraded without a
clear evidence-backed reason.

## Solver Execution Model

Use `solver_submit.sh`, not ad hoc background jobs, for nontrivial runs:

```bash
../../solver_submit.sh --type python --max-time 1800 \
  --input results/canonical_results.json \
  --output results/sensitivity/summary.json \
  --seed 20260804 \
  models/m3_milp/05_sensitivity.py
```

The wrapper exposes `FACTORY_SOLVER_JOB_ID` to the solver. Query public evidence
through `--status <jobid> --json`; do not inspect SQLite or mutable Legacy
metadata as proof. Submission receipts bind runtime/code/inputs/argv digest/
seeds, completion receipts bind terminal status and declared output hashes;
native jobs also bind both receipt hashes into workflow events.
Stdout remains next to the script as `<script>.log`, and stderr under `logs/`.

Historical Stata and Legacy entry points are preserved in `~/paper_new`.

## Figures, Tables, And LaTeX

Follow `modeling_guide.md`:

- Figures: academic palette, self-contained captions, PDF plus PNG when useful.
- Tables: `booktabs`, right-aligned numeric columns, compact labels.
- Symbols: every variable and parameter used in the model must appear in `symbol_table.md` and the paper's symbol table.
- LaTeX: CUMCM/MCM-style sections, with abstract filled only at Step 14.
- Compilation: use `compile_paper.sh`; it selects `xelatex` for `ctex`, `cumcmthesis`, `mcmthesis`, or `xeCJK`, sanitizes TeX search paths, runs without shell escape, verifies all three recorder files, and emits `bibliography-build-receipt-v1`. BibTeX/Biber failures, stale `.bbl` reuse, unresolved citations, project symlinks, and non-runtime external reads are fatal.

## Web Control Plane Contract

The stable ASGI entry is `apps.web.backend.main:app`. During the repository
boundary transition, `web/backend/main.py` contains the FastAPI implementation;
`web/backend/app.py` is only a compatibility launcher/re-export.

Authentication and approvals are persisted in SQLite at `web/auth.db` through
`web/backend/auth_store.py`. Passwords are bcrypt hashes. Registration creates
a pending user; administrators approve users and project requests. A non-admin
user sees and manages only projects granted through `project_acl`, while an
administrator can manage all projects. Read-only paper visibility is a separate
`showcase_acl`: visitors use the `guest` audience, and active users inherit that
public set plus their personal grants. `SHOWCASE_PROJECTS` seeds the guest
audience only when an existing database is first upgraded to this contract.

Engine project creation, lifecycle controls, and solver policy call
`FactoryService` directly. FastAPI owns authentication, ACL checks, and HTTP
error mapping; it does not route these commands through shell subprocesses.

The Web project list exposes `problem_key`, `problem_title`, `storage_scope`,
`archived`, and the workflow `revision`. Equivalent contained problem statements share a canonical
SHA-256 identity so the frontend can group multiple runs into one problem
archive. This grouping does not move or rename project directories:
`ongoing/` and `complete/` remain authoritative storage.

The authenticated project workspace uses `contest-dashboard-v1` from
`GET /api/projects/{base}/contest-dashboard` for the eight-phase view, timing
forecast, persistent action center, evidence cockpit, and delivery-readiness
checks. It must show Legacy projects as unconfigured instead of inventing a
contest clock. Human Gate submissions remain revision-checked and SQLite
append-only. `GET /api/projects/{base}/submission` resolves and verifies the
atomic current release before serving a ZIP; it must never trust a flat alias
or an arbitrary project path. Both endpoints enforce the same `project_acl` as
other internal project details.

`GET /api/projects/{base}/modeling-directions` and
`GET /api/projects/{base}/problem-plan` expose `node-output-v1` typed
`ContentBlock` payloads. The frontend registry renders method cards, notices,
key/value summaries, artifact links, and DAGs without accepting backend HTML.

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
