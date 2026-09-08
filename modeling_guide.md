# Modeling Guide

This document is the canonical conventions guide for math-modeling
competition projects inside the modeling-factory workflow. It plays the
same role that `analysis_guide.md` plays for the original Paper Factory:
every step prompt is expected to read it before writing code or prose.

## Contest-core operating contract

New projects run under a scheduler-enforced 74-hour clock. Finish exploration
and all authored content by T−6h, reserve the terminal six hours for content
freeze, deterministic checks, Final Audit, compilation, attachment inspection,
and atomic delivery, and treat T−2h as delivery freeze. After delivery freeze,
substantive reopening requires explicit human approval.

Validation must fit the problem type. Always perform sensitivity/robustness and
an independent recomputation where meaningful. Optimization problems add
direction-correct bounds, budget ladders, plateau semantics, and cross-algorithm
checks; prediction problems add cross-validation, residual diagnostics, and
generalization error; simulation problems add step/grid/sample convergence.
Do not manufacture optimization bounds for a non-optimization problem.

Keep three artifact layers distinct: authored business truth; immutable machine
evidence such as solver/audit receipts and snapshot hashes; and rebuildable
Markdown/Web projections. Fix the source of a projection and regenerate it.
Never hand-edit a receipt or treat Agent narration as execution evidence.

## Solver Execution

This factory does not use Slurm. It provides `solver_submit.sh` to
launch arbitrary solver scripts as local background processes with a
jobid-based interface. From a project directory:

```bash
SOLVER_SUBMIT="../../solver_submit.sh"

# Python (the default for most modeling work):
JOBID=$("$SOLVER_SUBMIT" --type python scripts/m1_solve.py)

# With a wall-clock cap (recommended for any non-trivial job):
JOBID=$("$SOLVER_SUBMIT" --type python --max-time 600 \
  --input data/final/instance.json \
  --output results/problem1/values.json \
  --seed 20260804 \
  scripts/m1_solve.py)

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

"$SOLVER_SUBMIT" --status "$JOBID" --json
# → solver-job-evidence-v2; trust only terminal receipt_ready=true evidence
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
- Declare every project input, expected output, and random seed with repeated
  `--input`, `--output`, and `--seed`. The wrapper hashes code and inputs at
  submission, then hashes declared outputs at completion. The solver receives
  `FACTORY_SOLVER_JOB_ID`; write it into the final result provenance inside the
  job, because editing an output after completion invalidates the receipt.
- A Solver receipt proves that a seed was declared, not that arbitrary model
  code consumed it. Set the seed explicitly in the script and record the
  observed seed in result provenance whenever reproducibility depends on it.
- Use `--status "$JOBID" --json` as the only public evidence query. Native and
  Legacy jobs share `solver-job-evidence-v2`; old jobs without immutable
  submission/completion receipts return `receipt_ready=false` and are not proof
  of execution identity. Native receipts additionally require matching hashes in
  the append-only workflow event stream (`event_stream_bound=true`).
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

It resolves one active root (`<base>_paper.tex`, otherwise
`paper/paper.tex`), fixes the search order to the root directory followed by
the project directory, runs `pdflatex`/`xelatex`, selects exactly one of
BibTeX/Biber when needed, then runs two more engine passes. Stale AUX/BCF/BBL
state is removed before pass one, and any bibliography backend failure or
unresolved citation is fatal. Every engine pass uses `-recorder`; the final
`logs/compilation/latex_inputs.json` must prove that all three passes share the
same declared project-input identity. External reads are accepted only from
controlled TeX/font runtime roots; project symlinks and all other external or
undeclared inputs fail closed. `logs/compilation/bibliography_build_receipt.json`
binds backend/version, first-pass AUX/BCF, `.bib`, project `.bst`, and the
generated `.bbl`. Missing, cyclic, dynamic, symlinked, out-of-project, or
recorder-mismatched inputs are fatal. Do not hide
an input behind a filename macro; use literal `\input`, `\include`, `\subfile`,
`\bibliography`, `\addbibresource`, `\includegraphics`, or
`\lstinputlisting` paths. For competitions that require specific document
classes:

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
│   ├── problem_plan.json        ← validated problem-specific dependency DAG (Step 0)
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

Storage format support and automatic judge review are separate contracts.
Adopted jobs keep every declared input/output and initial snapshot in the
required evidence chain. Small primitive numeric `.npy`/`.npz` files receive a
lossless `numpy-review-capsule-v1` representation, including original bytes,
SHA-256/size, member names, dtype, shape, storage order, and every element's
index, byte offset and exact value. Grounding regenerates this representation
before accepting a quote. Raw snapshots and decoded text both consume the packet
budget; samples and unbound summaries cannot satisfy full evidence coverage.

The current decoder supports NPY 1.0/2.0/3.0, boolean, signed/unsigned 8–64-bit
integers, float16/32/64 and complex64/128, including endianness and C/F order.
Limits per source: 128 KiB raw bytes, 128 KiB expanded NPZ members, 32 members,
4 KiB NPY headers, 8 dimensions with each extent at most 4096, 4096 total
elements, and 256 KiB rendered review bytes, subject also to the role budget.
Object/pickle arrays, structured/string/date dtypes and other formats remain
unsupported for automatic direct review. `.parquet` remains a permitted storage
format, but there is currently no verified Parquet decoder in this review path;
a required Parquet artifact therefore makes the packet incomplete. A companion
JSON file does not waive the original required artifact. These limitations must
be resolved before claiming a complete execution review.

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

- Numerical JSON producers use `factory_core.json_values.dumps` to normalize
  NumPy scalars/arrays into actual JSON bool/int/float/list values. Unsupported
  types and non-finite numbers fail with a field path; Python bool is supported.
- Local native Python jobs enforce their declared project-file reads through
  `python-audit-open-v2` for new local jobs (legacy v1 receipts remain readable).
  Reading or appending to a preexisting output requires declaring it as an input;
  the submission preserves its initial bytes and hash separately from the final
  output. Newly created or truncating-replaced outputs may be read back as this
  run's generated data. Declare indirect attachments and imported project code
  as inputs. This names Python audited file I/O, not arbitrary native-library
  I/O or an OS sandbox. Other runtime receipts remain declaration-only.
- Accepted numeric claims use the versioned contract in
  `docs/operations/RERUN_REPAIR_AND_TECHNICAL_CONTINUATION.md`. Summaries and
  tables must be regenerated when the explicitly accepted source/run changes.

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
- `problem/problem_plan.json` — the `problem-plan-v1` task DAG written in Step
  0. Keep it aligned with `problem_brief.md`; validate changes with
  `scripts/validate_problem_plan.py`. It schedules scientific dependencies
  inside the fixed lifecycle and never advances SQLite workflow state.
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
- `results/canonical_results.json` — when present under the current contract,
  every subproblem records an explicit project-relative `source`/`source_file`.
  Its selected method and solver provenance must agree with `chosen_method.md`
  and the source `values.json`; prose is never allowed to override this chain.
- `quality_contract.json` — continuous-time hard constraints require independent
  endpoint/event localization, a certified interval/error bound, or validated
  dual implementations. Rechecking the same sampled time array is useful
  diagnostics but is not independent hard-pass evidence.
- Quality-contract v4 optimization checks are direction-aware: maximize uses an
  upper bound and a nondecreasing budget ladder; minimize uses a lower bound and
  a nonincreasing ladder. Each check binds a proof locator, plateau semantics,
  and an on-disk cross-check with at least two algorithm families.
- `results/derived_artifacts.json` — pins canonical results, the versioned
  generator, and generated table/headline/xlsx outputs. Create it with
  `scripts/create_derived_manifest.py`; verify it with
  `scripts/verify_derived_artifacts.py`, which regenerates in isolation and
  rejects current-output edits or undeclared/missing outputs.
- Large scientific JSON may have a declared `.evidence-view.json` derivative
  using `scripts/json_evidence_view.py` (`json-evidence-view-v1`). It retains
  every key, scalar and undeclared array and replaces only explicitly named
  numeric arrays with source-pointer/count/hash references and visible
  limitations. The original file stays intact. `judge_packet.py` reconstructs
  the view from that complete file; any mismatch makes the view unavailable.
  Register the view as the claim artifact only when all evidence needed for
  that claim remains inline. A reference never proves that the judge read or
  validated the array. Required array-level claims still need complete inline
  evidence or a separate review; never use a view to weaken their requirements.
- `.factory/audits/profiles/{model,results,paper}/latest.json` — machine-owned
  stage feedback. On retry, read `evidence.checks` and its reports before
  editing. Fix the source artifact; do not hand-edit `AUDIT-*` ledger rows or
  treat a stage PASS as delivery approval.
- `.factory/audits/latest.json` — only a current `final` record with verified
  judgment and final-acceptance receipts can authorize delivery. The final
  audit recompiles, reruns the full paper/provenance suite, checks rendered
  pages, runs all three isolated Judge roles in enforce mode, and rejects any
  content change during judging.
- Final publication is immutable and pointer-based:
  `papers/releases/<base>/<snapshot>/` contains the audited PDF, submission ZIP,
  manifest and receipts; `papers/<base>/current.json` is the only authoritative
  current version. Flat PDF/ZIP files are compatibility aliases.
- Project files cannot grant a quality bypass. Only an administrator record in
  `web/auth.db` may continue after Gate 2 or authorize one exact final snapshot;
  the real verdict remains visible and no PASS may be fabricated.
- Control-plane grants in `web/auth.db` do not resolve Human Gates or advance
  the project workflow. Those decisions live in the schema-v9
  `.factory/state.db`; conversely, a project decision cannot grant Web access or
  a delivery override.

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

Normal-run evidence, numeric field binding and status contracts are documented in
[docs/operations/NORMAL_RUN_AUDIT_CONTRACT.md](docs/operations/NORMAL_RUN_AUDIT_CONTRACT.md).
