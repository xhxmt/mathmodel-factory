# Modeling Guide

This document is the canonical conventions guide for math-modeling
competition projects inside the modeling-factory workflow. It plays the
same role that `analysis_guide.md` plays for the original Paper Factory:
every step prompt is expected to read it before writing code or prose.

## Solver Execution

This factory does not use Slurm. It provides `solver_submit.sh` to
launch arbitrary solver scripts as local background processes with a
jobid-based interface. From a project directory:

```bash
SOLVER_SUBMIT="../../solver_submit.sh"

# Python (the default for most modeling work):
JOBID=$("$SOLVER_SUBMIT" --type python scripts/m1_solve.py)

# With a wall-clock cap (recommended for any non-trivial job):
JOBID=$("$SOLVER_SUBMIT" --type python --max-time 600 scripts/m1_solve.py)

# Other types:
"$SOLVER_SUBMIT" --type julia   scripts/m2_simulate.jl
"$SOLVER_SUBMIT" --type matlab  scripts/m3_pde.m
"$SOLVER_SUBMIT" --type R       scripts/m4_regression.R
"$SOLVER_SUBMIT" --type gurobi  models/m1_opt/instance.lp
```

Check status:

```bash
"$SOLVER_SUBMIT" --status "$JOBID"
# → RUNNING | COMPLETED | FAILED | TIMEOUT | EXITED | UNKNOWN
```

Block until terminal:

```bash
"$SOLVER_SUBMIT" --wait "$JOBID"
# exits 0 if COMPLETED, nonzero otherwise
```

Important rules:

- Always use `solver_submit.sh`. Do not call `nohup python ... &` directly.
  Direct `nohup` jobs are invisible to the runner's hang detection and
  to `--status` / `--wait`.
- Always pass `--max-time` for jobs that could plausibly hang
  (optimization loops, ML training, simulation). A modeling competition
  has a hard total budget — every hung job steals from another model.
- The solver's stdout/stderr appear in the project as
  `<script_stem>.log` and `logs/<script_stem>_stderr.log`. After the
  job finishes, move the stdout log into `logs/` (alongside the
  stderr) so the project root stays clean.
- Soft modeling outcomes (infeasible LP, MATLAB caught error) are NOT
  classified as FAILED by `--status` if the process returns 0. Read
  the log to interpret them.

### Working While Jobs Run

Do not block on one solver job when more work could be in flight.
Submit, continue writing the next script or reading earlier results,
poll periodically. The step is not finished until every job that
contributes to the deliverables is COMPLETED (or its failure has been
explicitly diagnosed and worked around).

### Parallel Solver Jobs

When multiple scripts are independent, submit them in parallel:

```bash
SOLVER_SUBMIT="../../solver_submit.sh"
JOB1=$("$SOLVER_SUBMIT" --type python --max-time 300 scripts/m1_baseline.py)
JOB2=$("$SOLVER_SUBMIT" --type python --max-time 300 scripts/m1_robust.py)
JOB3=$("$SOLVER_SUBMIT" --type python --max-time 600 scripts/m1_sensitivity.py)
echo "Jobs: $JOB1 $JOB2 $JOB3"
```

Each sibling script must load its inputs independently from
`data/intermediate/` or `data/final/`. Do not assume shared in-memory
state across parallel processes.

## LaTeX Compilation

Use the local compile helper:

```bash
../../compile_paper.sh "$(pwd)" your_base_name
```

It runs `pdflatex`, `bibtex` when needed, then two more `pdflatex`
passes. For competitions that require specific document classes:

- US contest (MCM/ICM): use `mcmthesis` or the contest's published
  template. Keep page count under the contest limit (currently 25 pages
  for MCM, including summary sheet).
- Chinese national contest (CUMCM): use the official CUMCM template.
  Section ordering is prescribed; do not improvise.
- Other competitions: follow the published template exactly. Style
  deviations are scored against you.

## Project File Layout

A modeling-factory project is organized as:

```
ongoing/<base>/
├── checkpoint.md                ← research question, last step, timestamp
├── modeling_guide.md            ← this file (copied at project init)
├── problem/
│   ├── problem.pdf              ← original problem statement (raw)
│   ├── problem_brief.md         ← restated and decomposed (Step 0)
│   ├── terminology_table.md     ← ambiguous-term disambiguation (Step 0)
│   ├── data_inventory.md        ← provided + missing data + sources (Step 0)
│   ├── feasibility_constraints.md  ← time budget, format, page limit (Step 0)
│   └── candidate_methods.md     ← shortlisted method-library entries (Step 0)
├── data/
│   ├── raw/                     ← source artifacts, never modified
│   ├── intermediate/            ← rebuildable staged products
│   └── final/                   ← rebuildable analysis-ready datasets
├── models/                      ← one directory per modeling stream
│   ├── m1_<short_name>/         ← e.g. m1_milp, m2_ode, m3_xgboost
│   ├── m2_<short_name>/
│   └── ...
├── scripts/                     ← top-level orchestrating scripts
├── figures/                     ← all PDFs / PNGs (with m<N>_ prefix)
├── tables/                      ← all .tex / .csv table fragments (with m<N>_ prefix)
├── logs/                        ← solver and step logs (moved here after jobs finish)
├── paper/                       ← LaTeX sources (or root-level .tex for simple projects)
└── results/                     ← serialized numerical results (.json/.npz/.parquet)
```

Hard rules:

- Keep `data/raw/` immutable. Never overwrite raw source files.
- Anything reproducible from `data/raw/` + scripts goes in
  `data/intermediate/` or `data/final/`. Step 16 cleanup may delete
  these.
- Every modeling stream gets a unique `m<N>_` prefix used for files
  across `models/`, `figures/`, `tables/`, `results/`. This prefix is
  how downstream review and audit steps locate the artifacts.

## Mathematical Symbol Conventions

- Scalars and indices: italic Latin or Greek (`$x$`, `$\beta$`, `$i$`).
- Vectors: bold italic lowercase (`$\boldsymbol{x}$`).
- Matrices: bold upright uppercase (`$\mathbf{X}$`).
- Sets: calligraphic uppercase (`$\mathcal{S}$`).
- Probability: `$\mathbb{P}$`. Expectation: `$\mathbb{E}$`.
  Indicator: `$\mathbb{1}\{\cdot\}$`.
- Estimated/predicted: hat (`$\hat{\beta}$`). Optimal: star
  (`$x^{*}$`). Time-derivative: dot (`$\dot{x}$`).

Every distinct symbol used in the paper must appear in a symbol table
(`paper/symbols.tex` or a `\begin{tabular}` block in the main `.tex`).
Reusing a symbol for two meanings — even in different chapters — is a
scoring penalty.

## LaTeX Document Requirements

A modeling-competition paper, regardless of contest, must include:

1. **Summary / Abstract** — usually a separate page; the most
   high-leverage single artifact in the paper. It must cover problem
   understanding, approach, key results, and distinguishing features.
   For CUMCM problems with explicit sub-questions, prefer the excellent
   paper pattern: a short opening setup followed by one paragraph per
   sub-question, each paragraph reporting model/algorithm, key result,
   and verification or required attachment.
2. **Problem Restatement** — paraphrase the problem in your own words.
3. **Problem Analysis** — break the problem into sub-questions, explain
   the modeling choices at a high level.
4. **Assumptions and Justifications** — every assumption is listed,
   each with a one-line justification. Do not list more than ~10;
   subdivide if necessary.
5. **Symbol Table** — see above.
6. **Model Formulation** — the math. State objectives, decision
   variables, constraints, governing equations.
7. **Model Solution** — algorithms, computational tools, results.
8. **Sensitivity Analysis** — vary key parameters, report stability.
9. **Strengths and Weaknesses** — honest self-assessment.
10. **Conclusions** — what was learned and what we recommend.
11. **References** — `\bibliographystyle{plain}` or the contest's
    required style.
12. **Appendix** — full code listings if required by the contest;
    extended derivations.

## Figure Style

All figures must follow this style. The palette is academic, distinct
from the original Paper Factory's commercial blue/magenta.

### Figure Selection

Excellent CUMCM papers use figures sparingly but purposefully. Before
drawing a figure, assign it one primary narrative role:

- `explain_model`: explain geometry, physical mechanism, variables,
  regions, or sub-problem dependencies before formulas.
- `report_result`: present the final result, path, key state, or main
  output for a sub-problem.
- `validate_result`: justify why the adopted result is credible, for
  example by showing convergence, a feasibility boundary, a tight
  constraint, sensitivity, or an independent algorithm comparison.
- `show_limitation`: show a rejected branch, failure mode, or limitation.
  These figures belong in sensitivity analysis, model evaluation, or
  appendix, not as the main result figure.

Every sub-problem should have at least one visual anchor: either a main
result figure or a main result table. Complex geometric or physical
criteria should be preceded by an explanatory diagram. Search and
optimization results should include a curve, boundary, convergence plot,
or comparison table that supports the final adopted value.

### Canvas and Export

- Vector PDF (preferred) or 600+ dpi PNG.
- Aspect ratio: 5:3 unless the data demands otherwise. Page size 540 ×
  324 pt is a safe default.
- Background: white `#FFFFFF`.

### Color Palette

| Role | Color | RGB |
|------|-------|-----|
| Primary | Deep blue `#2E5C8A` | 46, 92, 138 |
| Secondary | Brick red `#C04D4D` | 192, 77, 77 |
| Tertiary | Forest green `#4D9D5B` | 77, 157, 91 |
| Quaternary | Amber `#D49B3E` | 212, 155, 62 |
| Quinary | Royal purple `#6B4D9A` | 107, 77, 154 |
| Neutral gridlines | Light gray `#E8E8E8` | 232, 232, 232 |
| Zero / axis | Dark gray `#404040` | 64, 64, 64 |

Usage rules:

- One-series chart: primary blue.
- Two-series chart: primary blue + brick red.
- Multi-series: cycle in the order above; never use colors outside the
  palette.
- Categorical comparisons of optimal vs. baseline: brick red for the
  proposed/winning category, neutral for baselines.

### Typography

- Body text and labels: Times New Roman or another serif that matches
  the document body.
- Math inside figures: rendered with LaTeX (`matplotlib.rcParams[
  "text.usetex"] = True` or equivalent).
- Axis tick labels: 9–11 pt. Axis labels: 11–13 pt. Legend: 9–11 pt.

### Composition

- Show left and bottom axes. Hide top and right spines unless they
  carry data.
- Major gridlines only, dashed light gray.
- Every figure must be readable in isolation: caption explains what the
  reader is looking at without referring back to the main text.
- Annotate the *important* points (optimum, intersection, regime
  boundary) directly on the figure with a short label and leader line.
- Algorithm illustrations: use `algorithm2e` or `algorithmicx` in
  LaTeX rather than rendering pseudo-code in matplotlib.

### Important Figure Rules

- All explanatory notes belong in the LaTeX `\caption{}` or `\note{}`,
  not painted inside the figure file.
- Never include a redundant title inside the figure if the LaTeX
  caption will say the same thing.
- Export PDF for production; PNG only for previews or when raster is
  required (e.g., heatmaps with millions of cells).

## Code Conventions

### Per-script header

Every solver script (.py / .jl / .m / .R) starts with a header comment:

```python
# m1_milp_baseline.py
# Stream: m1_milp
# Inputs:  data/final/instance.parquet
# Outputs: results/m1_baseline.json, tables/m1_baseline.tex
# Random seed: 42 (set both numpy and any solver-internal seeds)
```

### Reproducibility

- Fix random seeds at the top of every script.
- Pin solver versions where they affect numerical output (e.g.
  Gurobi 11 vs 12). Note the version in the script header.
- Write results to disk as structured data (JSON / Parquet / NPZ),
  not as flat text. The downstream report-generation step reads from
  these files; transcribed numbers are a common source of errors.

### Script Naming

Inside each `models/m<N>_<name>/` directory:

- `01_data.py`        — load and stage inputs
- `02_model.py`       — formulate
- `03_solve.py`       — run the solver
- `04_postprocess.py` — derived quantities, summary stats
- `05_sensitivity.py` — parameter sweeps
- `06_figures.py`     — generate figures for this model

For very small models, collapsing into a single script is fine. For
larger models, more granular numbering is fine. The convention is the
two-digit prefix; the names after it can adapt.

### Error Recovery

When a solver job FAILS:

1. Read the stdout and stderr logs immediately (`logs/<stem>.log` and
   `logs/<stem>_stderr.log`).
2. Diagnose the root cause — solver license, infeasibility, type
   error, OOM.
3. Fix the script and re-submit.
4. If a Python package is missing, install via `pip install --user` or
   the project's venv if one exists. Do not pollute system Python.
5. If a commercial solver license is missing (Gurobi/CPLEX), fall back
   to an open solver (HiGHS via PuLP/CVXPY, SCIP) and document the
   substitution in the model directory's README.

## Table Conventions

Tables go in `tables/m<N>_<name>.tex` as LaTeX fragments — the
caption and `\begin{table}` environment belong in the main paper,
the file contains only the inner tabular.

For tables generated from Python:

```python
import pandas as pd
df.to_latex(
    "tables/m1_main_results.tex",
    index=False,
    float_format="%.3f",
    column_format="lrrr",
    escape=False,
)
```

Key rules:

- Booktabs rules only (`\toprule`, `\midrule`, `\bottomrule`).
- Numeric columns right-aligned, text left-aligned.
- 3 decimals by default; 4 for sensitive comparisons. Never quote 8
  decimals — it looks unserious.
- Significance stars only when the model has a frequentist
  interpretation that warrants them.
- Notes go in the LaTeX caption / `\note{}`, not as extra rows.

## Cross-step state files

These files are created across the workflow and are subject to strict
update discipline:

- `problem/problem_brief.md` — written in Step 0, generally immutable
  afterwards. Edit only if a step uncovers a misreading of the problem,
  and log the change in `assumption_ledger.md`.
- `assumption_ledger.md` — the canonical record of every modeling
  assumption, its scope, and its justification. Created in Step 4;
  updated in place by every step that introduces or revises an
  assumption. Reviewers in Step 11 and 13 read this file.
- `audit_issue_ledger.md` — created in Step 4 once the first audit
  runs. Cross-step issue tracker. Items have status
  (BLOCKING/MAJOR/MINOR/RESOLVED) and may carry a `PROTECTED` flag —
  PROTECTED items must not be removed or downgraded by later revision
  steps without an explicit decision logged in the ledger.
- `findings_brief.md` (legacy name kept for runner compatibility) —
  the running synthesis of the chosen model's results and limitations.
  Audit sections from Step 5, 6 are appended, not overwritten.

## What you may NOT do

- Do not rely on tools or solvers that are not installed locally
  without first checking with `command -v` and adapting.
- Do not write competition-specific boilerplate
  (page-count tricks, hidden text) — competitions detect this and it
  costs you the prize tier.
- Do not fabricate data, citations, or results.
- Do not skip the sensitivity analysis section even when the model is
  deterministic — at minimum, vary input data within plausible bounds.
- Do not use emoji or informal voice anywhere in the paper.
