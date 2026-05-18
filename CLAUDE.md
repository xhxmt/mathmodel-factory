# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A local orchestrator that takes a research question and produces a finished economics/sociology paper PDF by running a 16-step pipeline. Each step is executed in a fresh Codex (`gpt-5.5`) or Claude CLI agent context, supervised by shell-level monitors in `run_paper.sh`. The factory itself is bash + a small amount of Python; the "work" is done by the LLM agents launched per step.

There are three relevant audiences for code in this repo:
1. The shell that launches and supervises agents (`launch_agents.sh`, `run_paper.sh`).
2. The prompts each agent reads (`prompts/step*.txt`, `STEPS.md`, `analysis_guide.md`).
3. The agents themselves, which write Stata, LaTeX, and markdown into project directories under `ongoing/`.

`README.md` is the user-facing intro. `STEPS.md` is the canonical contract for what each step must produce. `analysis_guide.md` is the contract for Stata/figure/file-layout conventions inside a project.

## Common commands

User-facing CLI (run from repo root):

```bash
./launch_agents.sh new [--no-start] <base> "Research question"   # create project
./launch_agents.sh resume <base> [<base2> ...]                   # start/resume runner
./launch_agents.sh <base1> [<base2> ...]                         # implicit resume
./launch_agents.sh run <base>                                    # foreground run
./launch_agents.sh pause <base>                                  # pause runner
./launch_agents.sh status                                        # status table
./launch_agents.sh attach <base>                                 # tail -f runner log
./launch_agents.sh trace <base> [--lines N] [--follow]           # live agent trace
```

Direct runner invocations (mostly used by `launch_agents.sh`, but useful for debugging):

```bash
./run_paper.sh <project_dir>                  # run/resume
./run_paper.sh --infer-step <project_dir>     # print step number derived from disk state
./run_paper.sh --status <project_dir>         # one-line summary
```

Inside a project directory (`ongoing/<base>/`):

```bash
../../stata_submit.sh do/file.do              # submit Stata job, prints local jobid
../../stata_submit.sh --status <jobid>        # RUNNING | COMPLETED | FAILED | EXITED | UNKNOWN
../../stata_submit.sh --wait <jobid>          # block until terminal state
../../compile_paper.sh "$(pwd)" <base_name>   # pdflatex / bibtex / pdflatex x2
python3 ../../scripts/verify_numbers.py "$(pwd)" <base_name>   # cross-check paper numbers vs logs
```

There are no tests, lint, or build commands — the artifacts are the paper files. "Did the step work" is verified by `infer_step` in `run_paper.sh` checking for specific files of a minimum length.

## Top-level architecture

### Two-layer launcher

`launch_agents.sh` is a thin CLI wrapper that creates projects, manages pids/locks, and shells out to `run_paper.sh`. It writes to:

- `run_state/process_registry` — `base_name pid` lines per active runner
- `ongoing/<base>/.runner.pid` — per-project pid file
- `ongoing/<base>/.paused` — pause marker (runner exits early if present)
- `ongoing/<base>/.killed` — kill marker (set by Step 1 viability gate)

`run_paper.sh` is the per-project workflow driver. On startup it execs a **snapshot** of itself in `logs/runner_snapshots/` (via `RUN_PAPER_SNAPSHOT=1`) so edits to `run_paper.sh` mid-flight do not affect already-running projects.

### File-state is authoritative

`infer_step()` in `run_paper.sh` does not trust `checkpoint.md`. It determines the last completed step by checking which output files exist on disk (e.g., `codex_research.md`, `findings_brief.md`, `final_review.md`, `${base}_paper.tex` containing `\begin{document}` and an `ABSTRACT PLACEHOLDER` marker, etc.) plus minimum line counts. On startup the runner overwrites `checkpoint.md` to match. **Consequence**: to make the runner redo a step, delete its output artifacts — editing `checkpoint.md` does nothing.

The `.review_state.json` file (written when a human-review cycle resumes the project from an earlier step) makes `infer_step` ignore artifacts older than the review request, so a rewind actually rewinds.

### Step dispatch

Each step has a `run_step_N` function. They call one of these primitives:

- `run_codex <prompt> <timeout> <hang_timeout>` — Codex agent only.
- `run_claude_worker <prompt> <timeout>` — Claude CLI only (`claude -p ... --dangerously-skip-permissions --effort max`).
- `run_claude_then_codex` / `run_codex_then_claude` — primary, then fallback on failure or missing artifacts.
- `run_codex_parallel` — multiple Codex agents in parallel with per-agent hang detection.
- `run_claude_fallback <step>` — last resort. Tells Claude to redo the whole step **using its own tools** (no further Codex launches).

Hang detection compares trace-file freshness (under `~/.codex/sessions/` for Codex, `~/.claude/projects/` for Claude) to `hang_timeout`, but **does not kill** the agent if descendant processes include `stata*`, `srun`, `python*`, `Rscript`, `R`, or `julia` — real work counts as alive.

### Prompt rendering

Prompts live in `prompts/step*.txt`. `render_prompt` prepends a common preamble (read `analysis_guide.md`; read `human_review.md` if present and treat it as newest reviewer guidance; do not reuse completed projects) and substitutes placeholders:

- `__PROJECT_PATH__` → absolute project dir
- `__RESEARCH_QUESTION__` → from `checkpoint.md`
- `__BASE_NAME__` → project base name
- `__FACTORY__` → factory root
- Step 2 also substitutes `__STREAM_ID__` and `__STREAM_PREFIX__` to identify the 1–6 findings stream.

Optional per-step researcher notes can be supplied via `web/notes.json` keyed by base name and `step_N` — they get appended as `NOTE FROM THE RESEARCHER:` to every agent prompt at that step.

### The 16 steps (see `STEPS.md` for full contracts)

Setup (`-1 → 0`): Codex writes `project_brief.md` from `checkpoint.md`.

1. Research + data wrangle + viability gate (multi-phase, some parallel)
2. Six parallel findings streams with prefixed artifacts (`f1_*`–`f6_*`); each loops with a critic until `VERDICT: VALIDATED`
3. Pick one validated package; promote to `findings_brief.md`
4. Seven extensions in parallel → five argument architects → review → audit (with up-to-2-round decider/auditor loop) → executor rebuilds a single unified package
5–6. Data audit, methods audit (appended to `findings_brief.md`)
7. Paper draft → `{base}_paper.tex` with `ABSTRACT PLACEHOLDER`
8. **Gate 1**: code review (replication / number QA)
9–10. Constructive review and revision (Step 10 archives `do/archive/pre_step10`)
11. **Gate 2**: final review. Verdict line drives control flow:
    - `VERDICT: PASS_WITH_DIRECT_FIXES` → continue
    - `VERDICT: REOPEN_STEP10_TEXT` / `REOPEN_STEP10_ANALYSIS` → rewind to step 9, allowed once (gated by `.step11_reopened_once`, `.step11_reopen_to_step10` markers)
12–15. Citation audit, table formatting, abstract (replaces `ABSTRACT PLACEHOLDER`), de-robotification
16. Recompile, copy PDF to `papers/`, run `scripts/cleanup_project_artifacts.py`, move project `ongoing/` → `complete/`

### Cross-step state inside a project

Two artifacts persist across steps and must be kept in sync by every step that touches them:

- `findings_brief.md` — single canonical analysis summary; later audit sections are **appended** rather than rewritten
- `audit_issue_ledger.md` — created in Step 4, becomes the canonical cross-step issue tracker. Step 11 cannot pass while it has unresolved blocking issues. Audit/review/revision steps must update statuses in place, not silently drop concerns.

### Step 2 stream conventions

The six findings streams use file prefixes `f1_` through `f6_`. A stream is "ready" when `findings_memo_<N>.md` has ≥20 lines AND `figures/f<N>_*.{pdf,png}` and `tables/f<N>_*.{tex,csv}` both exist. It is "validated" when the matching `findings_critique_<N>.md` starts with `VERDICT: VALIDATED`. Streams 1–4 default to Codex; streams 5–6 default to Claude (diversifies the search).

### Stata execution model (no Slurm)

- Always invoke Stata via `stata_submit.sh`, never via stdin. It launches `stata_wrapper.sh` (which auto-detects `stata-mp`/`stata-se`/`stata` or honors `STATA_BIN`) under `nohup`, returns a jobid of the form `local_YYYYMMDDHHMMSS_PID`, and writes metadata to `run_state/stata_jobs/<jobid>.meta`.
- `--time` is accepted but advisory; there is no scheduler.
- Stata writes `<dofile>.log` next to the do file; after the job ends the agent is expected to move it into `logs/`. `stata_submit.sh --status` checks for `r(N);` (FAILED) or `end of do-file` (COMPLETED).
- Submit independent do files in parallel — do not block on one job when others can start.

### Figures and tables

All figures must follow the palette and typography defined in `analysis_guide.md` (Blue `#1A85FF`, Magenta `#D41159`, etc., Helvetica, 540×324pt PDF). Notes go in LaTeX `\note{}`, not the figure. Tables use `esttab ... booktabs fragment nomtitles label compress nonotes` so LaTeX owns the table environment.

### Data layout

Inside a project, source artifacts and rebuildable outputs are strictly separated:

- `data/raw/` — source only, never deleted
- `data/intermediate/`, `data/final/` — rebuildable; Step 16 cleanup may remove these
- Legacy projects may use `analysis/raw`, `analysis/intermediate`, `analysis/final`, `analysis/unified` — keep that layout if already present rather than mixing

`scripts/cleanup_project_artifacts.py` knows both layouts and prunes precisely from the structured one.

## Things to know before editing

- **Editing `run_paper.sh` is safe** while runners are active — they're already executing from a snapshot copy under `logs/runner_snapshots/`. Edits take effect on the next runner launch.
- **Don't add `analysis_guide.md`, `STEPS.md`, or the prompts to your changes lightly** — they are part of the agent contract. A change here changes what every future agent does.
- **Heartbeats and locks**: `.heartbeat` is `STEP TIMESTAMP` (or `ACTIVE:STEP TIMESTAMP`, `STUCK:STEP TIMESTAMP`). `.runner.lock/` is a mkdir-based lock with staleness reclamation gated by heartbeat age vs. the expected next-step timeout + 30min buffer. Lock files older than 4h with no parseable heartbeat are reclaimed.
- **Killed projects** (`.killed` present) are skipped by `resume`. Remove the marker by hand only if you understand why the viability gate killed it.
- **Prompts assume `cd $PROJECT`** — agents are launched with `-C "$PROJECT"` and read project-relative paths. Don't introduce absolute paths in prompts beyond the substituted placeholders.
- Runtime directories (`ongoing/`, `complete/`, `papers/`, `logs/`, `run_state/`) are gitignored. Don't commit them.
