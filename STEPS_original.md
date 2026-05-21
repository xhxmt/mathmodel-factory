# Local Paper Skill

This is the local-computer version of the paper factory. Use local binaries and the helper scripts in this directory. Do not use Slurm, `sbatch`, `srun`, `sacct`, or `module load`.

## Local Infrastructure

- Factory root: this `local_factory/` directory
- Project directories: `ongoing/{base}/` while running, `complete/{base}/` after delivery
- Local Stata wrapper: `../../stata_submit.sh` from within a project directory
- Local compile helper: `../../compile_paper.sh "$(pwd)" {base}`
- Prompt templates: `prompts/step*.txt`

## General Rules

- Run each step with a fresh agent context.
- Follow `analysis_guide.md` in the project directory for Stata usage, figure style, file layout, and esttab conventions.
- Keep raw artifacts under `data/raw/`, rebuildable staged outputs under `data/intermediate/`, and analysis-ready datasets under `data/final/` unless the project already uses a legacy `analysis/` hierarchy.
- Update `checkpoint.md` after every verified step.
- Maintain `audit_issue_ledger.md` as the canonical cross-step issue tracker once it is first created in Step 4. Later audit, review, revision, and final-review steps must update statuses in place rather than silently dropping concerns.
- Never stop at a plan or scaffold if the step requires concrete outputs on disk.

## Setup

For a new project:
- Read the research question from `checkpoint.md`.
- Locate the relevant `.dta` files or other source data.
- Write `project_brief.md` with the research question, data locations, project path, and any relevant context.
- Do not begin Step 1 until setup is complete.

## Step Outputs

### Step 1: Research and Data Foundations

Complete these substeps before Step 2:
- 1A deep research: `codex_research.md`, `codex_references.bib`
- 1B data wrangling: `data_wrangle.md` and analysis-ready datasets in `data/final/` or the legacy equivalent
- 1B.5 data context: `data_context.md`
- 1C key variables: `key_variables.md`
- 1D viability gate: `viability_gate.md`; if verdict is `KILL`, also write `kill_memo.md` and stop
- 1E descriptive map: `descriptive_map.md`

### Step 2: Parallel Findings Packages

Produce:
- `findings_memo_1.md` through `findings_memo_6.md`
- `findings_critique_1.md` through `findings_critique_6.md`
- six validated focused findings packages, each with its own prefixed do files, logs, tables, and figures

Run six parallel findings streams. Each stream should build one focused candidate package with only 1-2 core tables and 1-2 core figures, not a broad project map. As each memo finishes, send it to a critic. Findings and criticism should loop until each package either validates cleanly or is clearly not worth carrying forward. Ground the packages in `data_context.md`, `data_wrangle.md`, `key_variables.md`, and `descriptive_map.md`.

### Step 3: Findings Package Selection

Produce:
- `findings_decision.md`
- `findings_brief.md`

Choose one validated Step 2 package to carry forward. Do not synthesize across packages and do not merge pieces from multiple packages. The selected package becomes the authoritative `findings_brief.md`; the others are discarded.

### Step 4: Extensions and Argument Architecture

Produce:
- `extension_brief_1.md`, `extension_brief_2.md`, `extension_brief_3.md`, `extension_brief_4.md`, `extension_brief_5.md`, `extension_brief_6.md`, and `extension_brief_7.md`
- `paper_map_1.md` through `paper_map_5.md`
- `paper_map_review_1.md` through `paper_map_review_5.md`
- `proposal_audit.md`
- `audit_issue_ledger.md`
- `argument_decision.md`
- updated unified analysis outputs, figures, tables, and `findings_brief.md`

Run seven parallel extension agents to stress-test and elaborate the selected Step 3 package. The extension mandates are: identification, supplementary data, heterogeneity, primary robustness, objection robustness, descriptive support, and one open-ended moonshot. Then run five argument architects in parallel, each proposing one focused paper around the same selected package.

After the architects:
- run one architecture review for each `paper_map_{N}.md`
- run the proposal auditor to trace the data build and populate `proposal_audit.md` plus `audit_issue_ledger.md`
- run the decider to choose the winning paper map and write `argument_decision.md`
- run the executor to rebuild a single coherent analysis package that follows the chosen map and updates `findings_brief.md`

### Step 5: Data Audit and Argument Research

Produce:
- Data Audit section appended to `findings_brief.md`
- `argument_research.md` when useful
- updated `audit_issue_ledger.md`

### Step 6: Methods Audit

Produce:
- Methods Audit section appended to `findings_brief.md`
- updated `audit_issue_ledger.md`

### Step 7: Paper Draft

Produce:
- `{base}_paper.tex`
- at least 5 figures and the supporting tables/logs/do files
- `sample_support.md`
- `dropped_findings.md`

Leave the abstract placeholder in place.

### Step 8: Code Review (Gate 1: Replication And Number QA)

Produce:
- `code_review.md`
- updated `audit_issue_ledger.md`

Fix code, tables, figures, and paper text where needed. Step 8 is the hard replication/number-consistency gate. It should close code-paper mismatches and number errors, but it does not clear conceptual or estimand risks merely because the arithmetic matches.

### Step 9: Constructive Review

Produce:
- `review_comments.md`

### Step 10: Revision

Produce:
- revised `{base}_paper.tex`
- `revision_summary.md`
- updated supporting files such as `findings_brief.md`, `sample_support.md`, and `dropped_findings.md` as needed
- updated `audit_issue_ledger.md`

### Step 11: Final Review (Gate 2: Estimand And Claim-Validity QA)

Produce:
- `final_review.md`
- updated `audit_issue_ledger.md`

Use one verdict line at the top:
- `VERDICT: PASS_WITH_DIRECT_FIXES`
- `VERDICT: REOPEN_STEP10_TEXT`
- `VERDICT: REOPEN_STEP10_ANALYSIS`

If a reopen verdict is issued, Step 10 runs once more, then Step 11 is repeated. Step 11 cannot pass while `audit_issue_ledger.md` still contains unresolved blocking issues or while earlier conceptual audit concerns have disappeared without an explicit resolution path.

### Step 12: Citation Audit

Produce:
- `citation_audit.md`
- corrected `references.bib`
- corrected `{base}_paper.tex`

### Step 13: Table Formatting

Produce:
- cleaned table `.tex` files
- any re-generated figures needed for legend placement
- `table_formatting.md`

### Step 14: Abstract

Produce:
- `abstract_draft.md`
- a final title inserted into `{base}_paper.tex`

### Step 15: De-robotification

Produce:
- `derobotification.md`
- final prose edits in `{base}_paper.tex`

### Step 16: Delivery

Produce:
- compiled `{base}_paper.pdf`
- copy of the final PDF in `papers/`
- cleanup of rebuildable intermediate data

After delivery, the runner moves the project from `ongoing/` to `complete/`.
