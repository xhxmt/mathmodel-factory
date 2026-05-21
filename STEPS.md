# Modeling Factory Workflow

This is the math-modeling-competition adaptation of the local paper factory (CUMCM 国赛 / 美赛 MCM-ICM / 华为杯). Original social-science version preserved at `STEPS_original.md`.

## Local Infrastructure

- Factory root: this repository
- Project directories: `ongoing/{base}/` while running, `complete/{base}/` after delivery
- Local solver wrapper: `../../solver_submit.sh` from within a project directory (Python / Julia / Matlab / R / Gurobi — set `--type` and `--max-time`)
- MinerU PDF → Markdown converter: `../../scripts/mineru_parse.py` (requires `MINERU_TOKEN` in repo `.env`)
- Method library: `../../method_library/` (AHP / TOPSIS / MILP / ARIMA / ODE seed; agents MAY only cite methods already registered here)
- Local compile helper: `../../compile_paper.sh "$(pwd)" {base}`
- Prompt templates: `prompts/step*.txt`
- Stata wrapper (`../../stata_submit.sh`) is retained for cross-mode compatibility but is **not** used in this workflow.

## General Rules

- Run each step in a fresh agent context (no continuation across steps).
- Follow `modeling_guide.md` in the project directory for solver invocation, project layout, math notation, LaTeX section list, figure palette, code reproducibility, and table formatting. If both `modeling_guide.md` and the legacy `analysis_guide.md` are present, **modeling_guide.md wins**.
- Project layout: `problem/` (题目原文 + 解析产物), `data/{raw,intermediate,final}/`, `models/<id>/` (按候选建模流分子目录), `scripts/`, `figures/`, `tables/`, `results/<subproblem>/`, `logs/`, `paper/` (LaTeX source).
- Update `checkpoint.md` after every verified step.
- `audit_issue_ledger.md` is created at Step 4 and is the cross-step issue tracker. Audit, review, revision, and final-review steps must update statuses in place rather than silently dropping concerns. Issues tagged `PROTECTED` (creative claims worth defending) MUST NOT be deleted or downgraded by later steps.
- All numerical results in the paper must trace back to a logged solver run in `logs/` or `results/`.
- Time budget for the entire workflow: target 74 hours (CUMCM 国赛 standard, 周四 18:00 → 周日 20:00). Each step's prompt carries a time-budget recommendation derived from `problem/feasibility_constraints.md`.
- Never stop at a plan or scaffold if the step requires concrete outputs on disk.

## Setup (step −1 → 0)

Implemented by `prompts/step0_problem_parsing.txt`. Triggered automatically by `run_paper.sh` when the seeded `__RESEARCH_QUESTION__` is an absolute path to a PDF or `.md` file (modeling-mode detection in `is_modeling_input()`).

Produce in `problem/`:
- `source.md` — competition problem statement, as Markdown (auto-converted via `mineru_parse.py` if input was a PDF)
- `source.mineru/` — sidecar with `layout.json`, `content_list.json`, `images/`
- `problem_brief.md` — authoritative restatement, sub-problem decomposition, dependencies, scoring tendency
- `terminology_table.md` — ambiguous-term → precise-definition table (≥ 5 entries)
- `data_inventory.md` — supplied attachments + missing data + suggested external sources
- `feasibility_constraints.md` — 74-hour time budget allocation, solver-time caps, submission-format hard constraints
- `candidate_methods.md` — recommended methods cited from `method_library/`, with method ↔ sub-problem mapping

Stop after the 6 files exist and `checkpoint.md` reads `Last completed step: 0`.

## Step Outputs

### Step 1: Background Research + Method Pre-selection

Produce:
- `research_brief.md` — literature / industry references relevant to the problem domain and chosen method families; cite real sources with full attribution. Do not fabricate.
- `viable_streams.md` — concrete list of N (typically 3–5) candidate modeling streams, each with: method family (from `method_library/`), expected sub-problem coverage, data-feasibility note, time-budget estimate
- `viability_gate.md` — verdict on each candidate stream after data and time-budget reality check. Top line:
  - `VERDICT: PROCEED` (≥ 2 streams pass) — continue to Step 2
  - `VERDICT: KILL` (no stream is feasible) — also write `kill_memo.md` explaining why, then stop. Factory short-circuits to step 16 cleanup.

Read `problem/*.md` and `method_library/README.md` first. Reference real, citable sources only.

### Step 2: Parallel Modeling Proposals

For each stream `N` ∈ {1..N} that passed Step 1's viability gate, produce:
- `m{N}_spec.md` — full modeling specification: assumptions, decision variables, objective, constraints, solver choice
- `m{N}_demo_result.{json,csv,log}` — small-scale demonstration solve (toy data or sub-sample) confirming the formulation is implementable
- `m{N}_critique.md` — critic agent's review, looping with the spec until it ends with `VERDICT: VALIDATED` or `VERDICT: ABANDONED`

A stream is **ready** when `m{N}_spec.md` ≥ 30 lines AND a `m{N}_demo_result.*` file exists. A stream is **validated** when `m{N}_critique.md` starts with `VERDICT: VALIDATED`. Streams may run in parallel; abandoned streams produce a `m{N}_critique.md` ending with `VERDICT: ABANDONED` and a short reason — they are NOT deleted (informs Step 3 trade-off discussion).

### Step 3: Method Selection (human intervention point)

Produce:
- `method_decision.md` — one validated stream promoted to primary, one validated stream optionally promoted to auxiliary/contrast; rationale grounded in `m{N}_critique.md` files and `problem/feasibility_constraints.md`
- `chosen_method.md` — symbolic-link-style summary: which `m{N}_*` files are now load-bearing for Step 4+

**Human gate**: if `human_review.md` exists in the project root and contains a `## Step 3 decision:` section, that decision is authoritative. Otherwise the decider agent picks the validated stream with the best innovation × feasibility product (not pure feasibility — evaluators expect creativity).

### Step 4: Full Model Construction

Produce:
- `model.md` — promoted from `m{primary}_spec.md` and expanded: full math formulation, derivations, edge cases, sub-problem coupling
- `symbol_table.md` — every variable / parameter / set used anywhere in the model, with type + units + range. Required by `modeling_guide.md` and CUMCM submission rules.
- `assumption_ledger.md` — assumption clearing-house. Each entry: `id | statement | source (题目/团队) | impact-if-violated | status`. This file becomes the cross-step `audit_issue_ledger.md`-style tracker for the rest of the workflow.
- `models/<id>/` — runnable code for the primary model + sanity-check tests (e.g., `pytest` for Python streams). One subdir per stream actually being implemented (typically primary + auxiliary).

### Step 5: Full Solve

Produce:
- `results/<subproblem>/{values.json,plots.pdf,solver.log}` — one subdirectory per sub-problem identified in `problem/problem_brief.md`
- `solve_log.md` — per-run table: solver, runtime, MIP-gap or convergence indicator, output files

Every solver invocation MUST go through `../../solver_submit.sh` with explicit `--max-time` (rough target: ≤ 2 hours per single run, with the bulk of the 74-hour budget reserved for ranges-of-parameters and sensitivity in Step 6). Independent sub-problem solves should run in parallel.

### Step 6: Sensitivity + Robustness

Produce:
- `sensitivity_report.md` — parameter perturbations, assumption-relaxation impacts, scenario comparison
- `figures/sensitivity_*.{pdf,png}` — required: at least one tornado / one-at-a-time figure AND one scenario-comparison figure
- updated `assumption_ledger.md` — flag any assumption whose perturbation changes the qualitative answer

CUMCM evaluators expect sensitivity. Missing it is a known scoring trap (also recorded in `problem/feasibility_constraints.md` § 已识别的硬性扣分项).

### Step 7: Model Evaluation

Produce:
- `evaluation.md` — strengths, weaknesses, generalization potential, comparison with auxiliary stream (if selected) and discarded streams from Step 2

### Step 8: Visualization Polish

Produce:
- regenerated `figures/*.{pdf,png}` conforming to `modeling_guide.md` color palette, typography, and self-contained-caption rule
- `visualization_log.md` — figure inventory: file, what it shows, which sub-problem it supports, caption draft

Each figure must read independently of the paper text (caption tells the full story). Independent step rather than merged with Step 5–6 because CUMCM scoring weights figure quality heavily.

### Step 9: Paper Draft

Produce:
- `{base}_paper.tex` — full LaTeX draft containing the canonical CUMCM sections per `modeling_guide.md` (摘要 / 问题重述 / 问题分析 / 模型假设 / 符号说明 / 模型建立 / 模型求解 / 灵敏度分析 / 模型评价 / 参考文献 / 附录)
- Leave `ABSTRACT_PLACEHOLDER` literal at the abstract location — filled in Step 14

Pull content from: `problem/`, `model.md`, `symbol_table.md`, `assumption_ledger.md`, `results/`, `sensitivity_report.md`, `evaluation.md`, `visualization_log.md`. Cite via `references.bib`.

### Step 10: Gate 1 — Numerical & Code Consistency Check

Produce:
- `code_review.md` — every numerical value cited in the paper must be traceable to a file in `logs/` or `results/`. List discrepancies and fixes.
- updated `assumption_ledger.md`
- corrected `{base}_paper.tex` if discrepancies found

Hard replication / number-consistency gate. Does NOT clear conceptual or modeling-validity risks merely because the arithmetic matches — that is Step 13 (Gate 2).

### Step 11: Constructive Review

Produce:
- `review_comments.md` — issue-level review covering: model correctness, assumption realism, solver convergence, sensitivity coverage, writing clarity, figure/table quality. Each issue gets a severity (BLOCKING / MAJOR / MINOR) and a recommended fix.
- updated `assumption_ledger.md` — new issues appended, existing issues whose status changed updated. Issues marked `PROTECTED` MUST NOT be deleted.

### Step 12: Revision

Produce:
- revised `{base}_paper.tex` and any supporting files (`model.md`, `figures/`, `tables/`, `results/`)
- `revision_summary.md` — per-issue: how it was addressed, what file changed, what number/figure was updated
- updated `assumption_ledger.md` — every BLOCKING and MAJOR issue from Step 11 must be either resolved or explicitly justified
- archive of pre-revision draft at `paper/archive/pre_step12/`

### Step 13: Gate 2 — Judge Simulation

Produce:
- `judge_evaluation.md` — three independent judge agents grade the paper against the CUMCM official rubric. Each judge writes: 6 dimension scores (建模合理性 20% / 求解正确性 20% / 创新性 20% / 写作清晰度 15% / 结果说服力 15% / 灵敏度分析 10%), per-dimension comments, top-3 improvement suggestions. Aggregate score + per-dimension mean reported at top.
- One verdict line at the very top:
  - `VERDICT: PASS` (aggregate ≥ threshold, no BLOCKING gaps) — continue to Step 14
  - `VERDICT: REOPEN_STEP12_TEXT` (writing / formatting issues only — rewind to Step 12 once, gated by `.step13_reopened_once`)
  - `VERDICT: REOPEN_STEP12_MODEL` (substantive modeling / solving issues — rewind to Step 12 once, may cascade rerun of Step 5/6 outputs)

Modeled on the original Step 11 reopen-gate; allowed at most once.

### Step 14: Abstract (human intervention point)

Produce:
- `abstract_draft.md` — strict four-paragraph CUMCM format: 问题理解 / 方法 / 结果 / 亮点
- `ABSTRACT_PLACEHOLDER` in `{base}_paper.tex` replaced with the final abstract

**Human gate**: abstract carries disproportionate weight in CUMCM scoring. If `human_review.md` contains a `## Step 14 abstract:` section, that text overrides the agent draft. Otherwise the agent proposes and a critic loop produces ≥ 2 candidate drafts before picking one.

### Step 15: Citation Audit + Table Formatting + De-robotification

Single-step polish bundle (formerly three separate steps in the social-science version). Produce:
- `citation_audit.md` — every `\cite{}` resolves to a real `references.bib` entry; remove any phantom citations
- cleaned table `.tex` files (booktabs, `esttab ... booktabs fragment nomtitles label compress nonotes` where applicable; LaTeX owns the table environment)
- `derobotification.md` — list of prose smells removed (e.g., "首先...其次...最后" laundry lists, hedge stacking, redundant restatements)
- final-prose `{base}_paper.tex`

### Step 16: Compile + Appendix + Package

Produce:
- compiled `{base}_paper.pdf` (via `../../compile_paper.sh`)
- code appendix integrated as `paper/appendix_code.tex` or `\inputminted{}` chunks
- `papers/{base}_paper.pdf` — copy delivered to the factory's papers/ dir
- `papers/{base}_submission.zip` — submission bundle (PDF + code + selected data, matching `problem/feasibility_constraints.md` § 提交格式硬约束)
- `scripts/cleanup_project_artifacts.py` invoked to prune rebuildable intermediates

After delivery, the runner moves the project from `ongoing/` to `complete/`.
