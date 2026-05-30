# CLAUDE.md

This file gives coding-agent guidance for this repository.

## What This Is

This checkout is a local Modeling Factory: a bash/Python orchestrator that
takes a math-modeling competition problem and drives a multi-agent 16-step
workflow to produce a finished paper PDF and supporting artifacts.

The active domain is CUMCM / MCM / ICM style applied mathematical modeling, not
the original economics/sociology Paper Factory. Legacy social-science prompts
and Stata helpers remain in the tree for compatibility, but new modeling work
must follow `STEPS.md` and `modeling_guide.md`.

There are three relevant audiences for code in this repo:

1. The shell that launches and supervises agents: `launch_agents.sh`, `run_paper.sh`.
2. The prompts each agent reads: `prompts/step*.txt`, `STEPS.md`, `modeling_guide.md`, `method_library/`.
3. The launched agents, which write markdown, Python/solver code, LaTeX, figures, tables, and result files inside project directories under `ongoing/`.

`README.md` is the user-facing intro. `STEPS.md` is the canonical step contract.
`modeling_guide.md` is the project-local style and execution contract. If
`analysis_guide.md` is also present, it is legacy context and does not override
`modeling_guide.md`.

## Common Commands

User-facing CLI from repo root:

```bash
./launch_agents.sh new [--no-start] <base> "/abs/path/to/problem.pdf"
./launch_agents.sh resume <base> [<base2> ...]
./launch_agents.sh <base1> [<base2> ...]
./launch_agents.sh run <base>
./launch_agents.sh pause <base>
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

### Two-Layer Launcher

`launch_agents.sh` creates projects, manages pids/locks, and shells out to
`run_paper.sh`. It writes:

- `run_state/process_registry`
- `ongoing/<base>/.runner.pid`
- `ongoing/<base>/.paused`
- `ongoing/<base>/.killed`

`run_paper.sh` is the per-project workflow driver. On startup it executes a
snapshot of itself under `logs/runner_snapshots/` via `RUN_PAPER_SNAPSHOT=1`;
edits to `run_paper.sh` affect newly launched runners, not already-running
snapshot copies.

### File-State Is Authoritative

`infer_step()` in `run_paper.sh` determines progress from actual files, not
from `checkpoint.md`. Editing `checkpoint.md` alone does not redo or skip work;
delete or regenerate the output artifacts that define the step.

Modeling-mode is detected by the `problem/` directory after setup. The runner
then uses the modeling branch of `infer_step()` and the modeling Step 1-16
contracts.

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
- Step 13: Gate 2 judge simulation via `judge_evaluation.md` verdict.
- Step 14: abstract replacement.
- Step 15: citation audit, table/prose polish, de-robotification.
- Step 16: compile, copy PDF to `papers/`, cleanup, move project to `complete/`.

Gate 2 verdict tokens in modeling mode are:

- `VERDICT: PASS`
- `VERDICT: REOPEN_REVISION_TEXT`
- `VERDICT: REOPEN_REVISION_MODEL`

The runner allows one reopen cycle and then proceeds by policy to avoid loops.

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
- `judge_evaluation.md`: Gate 2 control file.

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

## Editing Notes

- Runtime directories (`ongoing/`, `complete/`, `papers/`, `logs/`, `run_state/`) are gitignored.
- Do not commit `.env`, credentials, generated logs, PDFs, or benchmark downloads.
- Editing `run_paper.sh` is safe while runners are active because active jobs use snapshots.
- Do not change `STEPS.md`, `modeling_guide.md`, or active prompts casually; they are agent contracts.
- Keep legacy prompt files unless deliberately removing legacy mode. Many old social-science prompt files are no longer called by the modeling dispatcher but remain useful reference material.
