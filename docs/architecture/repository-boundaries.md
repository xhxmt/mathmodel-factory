# Repository boundaries

> Historical design snapshot: Legacy/Authority/Phase paths and rollback procedures described below are retired from this mainline as of 2026-09-16. Current execution and separation boundaries: [NATIVE_MAINLINE.md](NATIVE_MAINLINE.md). Stage mappings and evidence contracts remain implemented unless explicitly superseded there.

## Current owners

| Boundary | Owner | Compatibility path |
|---|---|---|
| Workflow state, scheduler, recovery | `factory_core/` | `run_paper.sh` |
| Human CLI entry | `apps/cli/` | `launch_agents.sh`, `scripts/project_ctl.py` |
| Web ASGI entry | `apps/web/backend/main.py` | `web/backend/main.py` contains the transitional implementation |
| Web frontend implementation | `web/frontend/` | `apps/web/frontend/` documents the future move boundary |
| Service deployment | `deploy/systemd/` | no runtime state is stored here |
| Evaluator and ablations | `evaluation/`, `experiments/` | grouped by `benchmarks/README.md` |
| Frozen shell control | `legacy/shell/` | root shell launchers select it explicitly |
| Retired domain assets | `legacy/social_science/` | read-only historical ownership |

`FACTORY` is the configurable runtime data root. It defaults to the repository
root for compatibility. `ongoing/`, `complete/`, `papers/`, `logs/`,
`run_state/`, credentials, and generated project artifacts are runtime data,
not source packages, and are not moved by the v2 refactor. Generated paper
artifacts are not shipped by a source clone; provision runtime data separately
when restoring the showcase or reproducing historical calibration runs.

The root `pyproject.toml` and `uv.lock` own Python dependency resolution.
`web/backend/requirements.lock` is the hash-locked deployment export. `web/frontend/package-lock.json` is the frontend lock.
Runtime start scripts only validate that prepared dependencies exist; they do
not create environments or install packages.
