# Analysis Guide

## Local Stata Execution

This local paper factory does not use Slurm. Instead, it provides a small wrapper that launches Stata as a local background process and returns a local job id.

From a project directory under `ongoing/` or `complete/`, use:

```bash
STATA_SUBMIT="../../stata_submit.sh"
JOBID=$("$STATA_SUBMIT" do/filename.do)
echo "Submitted local Stata job $JOBID"
```

Check status:

```bash
"$STATA_SUBMIT" --status "$JOBID"
```

Wait for completion:

```bash
"$STATA_SUBMIT" --wait "$JOBID"
```

Important rules:
- Do not call `sbatch`, `srun`, `sacct`, or `module load`.
- Do not pipe into Stata stdin. Always use `stata_submit.sh` or `stata_wrapper.sh`.
- The Stata batch log appears in the project root as `filename.log`. Move it into `logs/` after the job completes.
- `--time` is accepted for compatibility but is advisory only in local mode.

If the wrapper cannot find Stata automatically, set `STATA_BIN` to the full path of your local Stata executable before running the factory.

### Working While Jobs Run

Do not block on one Stata job if other work is available. Submit a job, continue writing the next do file or reading results, and poll status periodically. The task is not finished until the relevant Stata jobs have completed and their logs have been reviewed.

### Parallel Do Files

When multiple do files are independent and all read from the same built dataset, submit them in parallel:

```bash
STATA_SUBMIT="../../stata_submit.sh"
JOB1=$("$STATA_SUBMIT" do/03_univariate.do)
JOB2=$("$STATA_SUBMIT" do/04_bivariate.do)
JOB3=$("$STATA_SUBMIT" do/05_trends.do)
echo "Jobs: $JOB1 $JOB2 $JOB3"
```

Each sibling do file must load the analysis dataset independently. Do not assume shared Stata memory across local background jobs.

## Local LaTeX Compilation

Use the local compile helper instead of `module load`:

```bash
../../compile_paper.sh "$(pwd)" your_base_name
```

This runs `pdflatex`, `bibtex` when needed, then two more `pdflatex` passes.

## Figure Style Specification

All figures must follow this style.

### Canvas and Export
- Export format: vector PDF
- Page size: 540 x 324 pt (7.5 x 4.5 in, 5:3 aspect ratio)
- Background: white (`#FFFFFF`)

### Color Palette
| Role | Color | RGB |
|------|-------|-----|
| Primary fill (bars, CIs, error bars, main series) | Blue `#1A85FF` | 26, 133, 255 |
| Secondary fill / point estimates / markers | Magenta `#D41159` | 212, 17, 89 |
| Low-density dashed line | Dark magenta `#C10534` | 193, 5, 52 |
| Grid lines | Light gray `#F0F0F0` | 240, 240, 240 |
| Zero reference line | Gray `#A0A0A0` | 160, 160, 160 |
| Event-time reference line | Red `#FF0000` | 255, 0, 0 |

Color usage rules:
- Single-series bar charts: use Blue `#1A85FF`.
- Two-series bar charts: use Blue `#1A85FF` for the first series and Magenta `#D41159` for the second.
- Coefficient/dot plots: use Magenta `#D41159` for point estimates and Blue `#1A85FF` for confidence intervals.
- Line plots: use Blue `#1A85FF` (solid) for the first series and Dark magenta `#C10534` (dashed) for the second.
- Do not use colors outside this palette.

### Typography
- Font: Helvetica (fallback: Arial)
- Axis labels, tick labels, legend text: about 17 pt
- Title (if used): about 19 pt, centered
- Numeric style: `.05`, `-.05`, `0`

### Axes and Grid
- Show left and bottom axes in black; hide top and right spines
- Axis/tick stroke: about 0.65 pt
- Major gridlines only: dashed, `#F0F0F0`, about 1 pt
- Horizontal zero line when relevant: `#A0A0A0`, dashed
- Vertical event marker at treatment cutoff: `#FF0000`, dashed, at x = -0.5

### Important Figure Rules
- All explanatory notes go in LaTeX `\note{}`, not in the figure graphic.
- Remove `note()`, `title()`, and `subtitle()` from Stata graph commands except panel labels.
- Export PDF only.

## Data Layout and Cleanup

Use a consistent storage layout so delivery can safely prune rebuildable data.

- `data/raw/`: downloaded or original source artifacts only
- `data/intermediate/`: rebuildable staged products
- `data/final/`: rebuildable analysis-ready datasets
- `tmp/` and `replication/temp/`: scratch space only

Legacy projects may already use `analysis/raw*`, `analysis/intermediate`, `analysis/final`, or `analysis/unified`. If that layout already exists, keep it internally consistent; otherwise use the `data/raw`, `data/intermediate`, `data/final` structure above.

Hard rules:
- Keep source artifacts separate from rebuildable outputs.
- Do not save rebuildable analysis datasets in the project root.
- If a file can be recreated from source artifacts plus scripts, it belongs in `data/intermediate/`, `data/final/`, or the analogous legacy `analysis/` directory.

## Do File Conventions

Use globals like:

```stata
global project "/path/to/project"
global data "$project/data"
global raw "$data/raw"
global intermediate "$data/intermediate"
global final "$data/final"
global figures "$project/figures"
global tables "$project/tables"
global logs "$project/logs"
```

### Do File Naming

Organize active do files by purpose:
- `01_explore.do`
- `02_descriptive.do`
- `03_main_analysis.do`
- `04_figures.do`
- `05_heterogeneity.do`
- `06_mechanisms.do`
- `07_robustness.do`

Step-specific prompts may impose stricter prefixes such as `desc_`, `cx_`, or `cl_`. Follow the prompt when it is more specific than this general scheme.

### Stata Error Recovery

When a do file fails:
1. Read the log immediately.
2. Diagnose the root cause.
3. Fix the do file and re-run it.
4. If a user-written package is missing, install it and re-run.

### esttab Table Formatting

Generate clean tables from the start:

```stata
esttab m1 m2 m3 m4 using "$tables/tab_name.tex", replace ///
    booktabs ///
    fragment ///
    nomtitles ///
    label ///
    b(%9.3f) se(%9.3f) ///
    star(* 0.05 ** 0.01 *** 0.001) ///
    stats(N r2, labels("Observations" "\$R^2\$") fmt(%9,0gc %9.3f)) ///
    compress ///
    nonotes
```

Key rules:
- Always use `coeflabel()` to replace raw variable names with readable labels.
- Use `fragment` so LaTeX controls the table environment and caption.
- Use `compress` to help tables fit on the page.
- Read the generated `.tex` and remove duplicate header rows or notes if needed.
