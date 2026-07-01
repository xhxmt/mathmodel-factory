# CUMCM 2024A Paper Writing Revision Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rewrite the polished 2024A paper and its web showcase to use a five-question-first narrative, with final answers before diagnostics and evidence, and sync the revised paper to the deployed site.

**Architecture:** Keep the LaTeX source as the single source of truth for the paper text, then regenerate the PDF and update the GitHub Pages showcase to point at the refreshed artifact. Treat the paper body, figure captions, sensitivity framing, and web metadata as separate edit surfaces so each change is localized and testable.

**Tech Stack:** LaTeX/XeLaTeX, `pdftotext`, `rg`, Git, static HTML for `xhxmt.github.io`.

---

### Task 1: Rewrite the paper narrative in the LaTeX source

**Files:**
- Modify: `complete/test_cumcm2024a_polished/test_cumcm2024a_paper.tex`
- Test: `complete/test_cumcm2024a_polished/test_cumcm2024a_paper.pdf`

- [ ] **Step 1: Inspect the current paper structure**

Run: `pdftotext -layout complete/test_cumcm2024a_polished/test_cumcm2024a_paper.pdf - | sed -n '1,220p'`
Expected: the existing abstract, problem analysis, and result口径 are visible so the rewrite can preserve the technical content while changing the narrative order.

- [ ] **Step 2: Rewrite the abstract and problem-analysis entries**

Use the existing LaTeX content to make these explicit changes:

```tex
% Abstract shape
% 1) One sentence that states the unified model mainline.
% 2) Five paragraphs, each beginning with "针对问题 i" and ending with the result/output file.
% 3) Problem 2 states t^\star=412.473838 s first, then mentions continuous-time collision localization.
% 4) Problem 3 states p^\star≈0.450 m first, with 0.429883 m moved to sensitivity/appendix.
% 5) Problem 4 states the geometric construction first, then the "can it be shorter" discussion, then the SAT verification.
% 6) Problem 5 states the peak interval, peak handle, amplification factor, and timestep convergence before v_0^\star=1.258747 m/s.
```

Replace engineering-heavy wording with paper language and remove the main-thread mentions of `RELAXED`, `fallback`, internal JSON paths, and workflow terms.

- [ ] **Step 3: Verify the rewritten prose no longer advertises debug口径**

Run: `pdftotext -layout complete/test_cumcm2024a_polished/test_cumcm2024a_paper.pdf - | rg -n '0\\.429883|1\\.443369|RELAXED|fallback|workflow|results/.*json|m1_|m2_|m3_'`
Expected: only the allowed paper evidence remains in validation/sensitivity sections, and no internal workflow language remains in the main narrative.

- [ ] **Step 4: Commit the source rewrite**

Run:
```bash
git add complete/test_cumcm2024a_polished/test_cumcm2024a_paper.tex
git commit -m "refactor: rewrite 2024a paper narrative"
```
Expected: one commit containing the LaTeX source rewrite.

### Task 2: Reframe sensitivity, conclusions, and figure captions

**Files:**
- Modify: `complete/test_cumcm2024a_polished/sensitivity_report.md`
- Modify: `complete/test_cumcm2024a_polished/revision_summary.md`
- Modify: `complete/test_cumcm2024a_polished/visualization_log.md`
- Modify: `complete/test_cumcm2024a_polished/test_cumcm2024a_paper.tex`
- Test: `complete/test_cumcm2024a_polished/figures/*`

- [ ] **Step 1: Rewrite sensitivity headings and framing**

Use the following labels and wording:

```markdown
## 问题 3 螺距结果的抽样加密验证
## 问题 5 限速结果的时间步长收敛性
```

Frame the content as support for the final submission口径, not as failure/risk exposure.

- [ ] **Step 2: Reorder the conclusion to list only final recommended values**

Use this ordering in the paper conclusion:

```tex
% P1: full 0--300 s trajectory and small chord-length residual
% P2: t^\star=412.473838 s, first touch between benches 1 and 9
% P3: p^\star\approx0.450 m, with 0.429883 m demoted to coarse-grid lower bound
% P4: R_1=3.005 m, R_2=1.503 m, L=13.621230 m, zero collision in SAT scan
% P5: v_0^\star=1.258747 m/s, with 1.443369 m/s described only as coarse-step upper bound
```

- [ ] **Step 3: Align figure roles with the five-question narrative**

Make sure the paper’s main figures are assigned these roles:

```text
P1: solve_p1_inspiral_snapshots -> explain_model/report_result
P2: solve_p2_terminal_collision -> report_result/validate_result
P3: solve_p3_pitch_geometry or solve_p3_bisection -> validate_result
P4: solve_p4_scurve_geometry + solve_p4_uturn_snapshots -> explain_model/report_result
P5: solve_p5_speed_amplification -> validate_result
```

Any figure that shows failed or coarse results must be pushed to sensitivity or appendix material.

- [ ] **Step 4: Commit the framing updates**

Run:
```bash
git add complete/test_cumcm2024a_polished/sensitivity_report.md complete/test_cumcm2024a_polished/revision_summary.md complete/test_cumcm2024a_polished/visualization_log.md complete/test_cumcm2024a_polished/test_cumcm2024a_paper.tex
git commit -m "refactor: reframe validation and conclusion"
```
Expected: the validation framing and conclusion ordering are updated together.

### Task 3: Rebuild and verify the paper output

**Files:**
- Test: `complete/test_cumcm2024a_polished/test_cumcm2024a_paper.pdf`
- Test: `complete/test_cumcm2024a_polished/test_cumcm2024a_paper.log`

- [ ] **Step 1: Recompile the paper**

Run: `./compile_paper.sh complete/test_cumcm2024a_polished test_cumcm2024a_paper`
Expected: a fresh PDF is generated without LaTeX errors.

- [ ] **Step 2: Check the rendered PDF for the revised narrative**

Run: `pdftotext -layout complete/test_cumcm2024a_polished/test_cumcm2024a_paper.pdf - | sed -n '1,260p'`
Expected: the abstract is five-question-first, P2/P3/P4/P5 report the final口径 before diagnostics, and the conclusion reads as a final-answer list.

- [ ] **Step 3: Check for leftover internal jargon**

Run: `pdftotext -layout complete/test_cumcm2024a_polished/test_cumcm2024a_paper.pdf - | rg -n 'RELAXED|fallback|workflow|solver_submit|cache|results/.*json'`
Expected: no matches in the main narrative.

- [ ] **Step 4: Commit the rebuilt artifact state if the repository tracks it**

Run:
```bash
git add complete/test_cumcm2024a_polished/test_cumcm2024a_paper.pdf complete/test_cumcm2024a_polished/test_cumcm2024a_paper.tex
git commit -m "chore: rebuild revised 2024a paper"
```
Expected: the source and regenerated PDF remain synchronized in the working tree.

### Task 4: Sync the showcase on `tfisher.de`

**Files:**
- Modify: `xhxmt.github.io/index.html`
- Modify: `xhxmt.github.io/images/*` if the preview thumbnail or linked artifact changes
- Test: `xhxmt.github.io/index.html`

- [ ] **Step 1: Update the showcased paper metadata**

Keep the site metadata aligned with the revised paper title, abstract, and final PDF name:

```html
finalPdf: "test_cumcm2024a_paper.pdf",
paperTitle: "基于螺线解析积分与分离轴 SAT 碰撞检测的“板凳龙”运动学仿真与调头优化模型",
paperAbstract: "... revised abstract text ..."
```

- [ ] **Step 2: Verify the page still points to the right artifact**

Run: `sed -n '1590,1625p' xhxmt.github.io/index.html`
Expected: the `cumcm2024a` entry references the revised PDF and the updated abstract text.

- [ ] **Step 3: Commit the showcase sync**

Run:
```bash
git add xhxmt.github.io/index.html
git commit -m "docs: sync tfisher.de showcase to revised 2024a paper"
```
Expected: the published site metadata and the paper source remain consistent.

### Task 5: Push and publish the changes

**Files:**
- Repository state: all commits on `main`

- [ ] **Step 1: Review the commit history**

Run: `git log --oneline -5`
Expected: the narrative rewrite, validation reframing, paper rebuild, and site sync commits are present.

- [ ] **Step 2: Push the branch**

Run: `git push`
Expected: the updated paper and showcase land on the remote GitHub repository.

- [ ] **Step 3: Verify the deployment target**

Check the published `tfisher.de` pages or the deployment report for the updated metadata and linked PDF.
Expected: the site serves the revised paper.

