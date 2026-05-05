# Local Paper Factory

This directory is a local-computer port of the paper factory. It keeps the paper prompts, the step runner, the paper-style resources, and the audit helpers, but removes the Slurm and watchdog dependency. It does not include paper projects, datasets, credentials, runtime logs, or generated PDFs.

## What It Includes

- `launch_agents.sh`: local launcher with `new`, `resume`, `pause`, `run`, `attach`, `trace`, and `status`
- `run_paper.sh`: 16-step paper runner adapted for local execution
- `prompts/`: the paper-step prompt templates
- `analysis_guide.md`: local conventions for Stata, figures, file layout, and LaTeX
- `stata_submit.sh` and `stata_wrapper.sh`: local Stata execution helpers
- `compile_paper.sh`: helper for `pdflatex` and `bibtex`
- `resources/`: `paper.sty`, `bibliography.bst`, `model_papers_style.json`, and `Abstract_examples.md`
- `scripts/`: `verify_numbers.py` and `cleanup_project_artifacts.py`

## Prerequisites

- `codex` CLI installed and authenticated
- `claude` CLI installed and authenticated
- `pdflatex` and `bibtex` on `PATH`
- local Stata available, either on `PATH` or via `STATA_BIN=/full/path/to/stata-mp`

## Quick Start

Clone the repository, enter the directory, and check that the launcher is executable:

```bash
git clone <repo-url> local_factory
cd local_factory
chmod +x launch_agents.sh run_paper.sh compile_paper.sh stata_submit.sh stata_wrapper.sh
./launch_agents.sh status
```

The launcher creates local runtime directories as needed:

- `ongoing/` for active projects
- `complete/` for delivered projects
- `papers/` for final PDFs
- `logs/` and `run_state/` for process state and logs

These runtime outputs are intentionally ignored by Git.

## Data Setup

The factory does not assume a shared filesystem. When creating a project, include the relevant local data paths directly in the research question or add them to the generated `project_brief.md` before starting Step 1.

For example:

```bash
./launch_agents.sh new --no-start example_project \
  "Study the relationship between X and Y. Use data in /Users/me/data/example."
```

Then edit `ongoing/example_project/project_brief.md` or `checkpoint.md` with any extra local setup notes before launching:

```bash
./launch_agents.sh resume example_project
```

## Common Commands

Create a project without starting it:

```bash
./launch_agents.sh new --no-start my_paper "Your research question here"
```

Create and start immediately:

```bash
./launch_agents.sh new my_paper "Your research question here"
```

Resume one or more existing projects:

```bash
./launch_agents.sh resume my_paper
./launch_agents.sh my_paper other_paper
```

Run a project in the foreground:

```bash
./launch_agents.sh run my_paper
```

Pause a running project:

```bash
./launch_agents.sh pause my_paper
```

Inspect status:

```bash
./launch_agents.sh status
```

Follow the local runner log:

```bash
./launch_agents.sh attach my_paper
```

## Notes

- The local launcher uses background processes and pid files instead of Slurm jobs.
- There is no local watchdog. If a run exits, inspect the log and resume it manually.
- Step 1 now includes a required `data_context.md` memo produced after wrangling and before Step 2.
- Step 2 runs six focused findings-package streams with critique/validation; Step 3 selects one package to carry forward rather than synthesizing across all six.
- The workflow now maintains an `audit_issue_ledger.md` inside each project once Step 4 runs. That ledger is the required cross-step record of blocking, major, and minor issues plus their resolution status.
- Step 8 is Gate 1: replication and number QA. Step 11 is Gate 2: estimand and claim-validity QA. A project should not pass Step 11 while the ledger still contains unresolved blocking issues.
- Finished projects are moved from `ongoing/` to `complete/`, and the final PDF is copied into `papers/`.
