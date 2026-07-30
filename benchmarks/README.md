# Benchmark ownership

- `evaluation/` owns evaluator and judge-calibration experiments.
- `experiments/` owns ablation launchers and comparison tooling.
- Generated evaluation results remain in their existing directories and are
  not runtime workflow state.

These paths are compatibility owners during the v2 transition. New production
orchestration code must not import them.
