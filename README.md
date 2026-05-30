# Modeling Factory

This repository is a local multi-agent workflow for math-modeling competitions
such as CUMCM, MCM/ICM, and similar applied modeling contests. It is adapted
from the original local Paper Factory, but the active workflow now targets
competition problem parsing, method selection, mathematical modeling, solver
execution, robustness checks, paper drafting, judging, and final packaging.

The original social-science assets are kept for reference and compatibility,
but the canonical modeling contract is `STEPS.md` plus `modeling_guide.md`.

## What It Includes

- `launch_agents.sh`: local launcher with `new`, `resume`, `pause`, `run`, `attach`, `trace`, and `status`
- `run_paper.sh`: 16-step Modeling Factory runner with file-state based resume
- `STEPS.md`: canonical math-modeling workflow contract
- `modeling_guide.md`: project layout, solver, LaTeX, figure, table, and reproducibility conventions
- `prompts/step*.txt`: agent prompt templates for each workflow step
- `method_library/`: registered modeling methods and runnable seed templates
- `solver_submit.sh` and `solver_wrapper.sh`: async local solver execution helpers
- `compile_paper.sh`: LaTeX helper that selects `xelatex` for Chinese / CUMCM style papers
- `scripts/`: helper scripts for Antigravity routing, MinerU parsing, number checks, and cleanup

Legacy files such as `analysis_guide.md`, `stata_submit.sh`, and
`stata_wrapper.sh` are retained only for cross-mode compatibility. New modeling
projects should follow `modeling_guide.md` and use `solver_submit.sh`.

## Prerequisites

- `codex` CLI installed and authenticated
- `claude` CLI installed and authenticated, if using Claude fallback routes
- Python 3 with the project-local `.venv` dependencies used by this checkout
- LaTeX tooling: `xelatex`, `pdflatex`, and `bibtex`
- At least one practical solver stack for project code, usually Python with
  `numpy`, `scipy`, `pandas`, and `matplotlib`
- Optional: `MINERU_TOKEN` in `.env` for PDF problem parsing through MinerU
- Optional: Julia, MATLAB/Octave, R, Gurobi, or other solvers if a project uses them

Secrets and local configuration belong in `.env`, which is gitignored.

## Quick Start

Clone the repository, enter the directory, and check the launcher:

```bash
git clone <repo-url> mathmodel-factory
cd mathmodel-factory
chmod +x launch_agents.sh run_paper.sh compile_paper.sh solver_submit.sh solver_wrapper.sh
./launch_agents.sh status
```

The launcher creates runtime directories as needed:

- `ongoing/` for active projects
- `complete/` for delivered projects
- `papers/` for final PDFs and submission bundles
- `logs/` and `run_state/` for process state and logs

These runtime outputs are intentionally ignored by Git.

## Creating A Modeling Project

For competition use, seed a project with an absolute path to a problem PDF or
Markdown file. That triggers modeling-mode setup, including `problem/` parsing.

```bash
./launch_agents.sh new --no-start test_cumcm2024b \
  "/absolute/path/to/problem.pdf"
```

Then resume the workflow:

```bash
./launch_agents.sh resume test_cumcm2024b
```

Run in the foreground when debugging:

```bash
./launch_agents.sh run test_cumcm2024b
```

Inspect status:

```bash
./launch_agents.sh status
```

Follow the runner log:

```bash
./launch_agents.sh attach test_cumcm2024b
```

## Solver Usage Inside A Project

Agents and humans should run nontrivial solves through `solver_submit.sh` from
inside `ongoing/<base>/` or `complete/<base>/`:

```bash
../../solver_submit.sh --type python --max-time 600 models/m3_milp/03_solve.py
../../solver_submit.sh --status <jobid>
../../solver_submit.sh --wait <jobid>
```

Supported types include `python`, `julia`, `matlab`, `R`, and `gurobi`, subject
to local installation.

Compile the paper manually when needed:

```bash
../../compile_paper.sh "$(pwd)" <base_name>
```

## Workflow Summary

The active modeling workflow is setup plus Steps 1-16:

- Setup / Step 0: problem parsing into `problem/`
- Step 1: background research and method pre-selection
- Step 2: parallel modeling proposals with demo solves
- Step 3: method selection, with `human_review.md` override support
- Step 4: full model construction
- Step 5: full solve
- Step 6: sensitivity and robustness
- Step 7: model evaluation
- Step 8: visualization polish
- Step 9: paper draft
- Step 10: Gate 1 numerical and code consistency check
- Step 11: constructive review
- Step 12: revision
- Step 13: Gate 2 judge simulation
- Step 14: abstract
- Step 15: citation/table/prose polish
- Step 16: compile, package, cleanup, and move to `complete/`

See `STEPS.md` for the exact file contract used by
`run_paper.sh --infer-step`.

## Notes

- File state is authoritative. `run_paper.sh --infer-step <project_dir>` checks
  actual artifacts, and checkpoint text is corrected to match.
- `modeling_guide.md` wins over legacy `analysis_guide.md` whenever both exist.
- Finished projects are moved from `ongoing/` to `complete/`.
- Final PDFs are copied to `papers/`; submission zips may be produced manually
  until Step 16 packaging is fully automated in the runner.
