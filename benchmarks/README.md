# Benchmark ownership

- `evaluation/` owns evaluator and judge-calibration experiments.
- `experiments/` owns ablation launchers and comparison tooling.
- Generated evaluation results remain in their existing directories and are
  not runtime workflow state.

These paths are compatibility owners during the v2 transition. New production
orchestration code must not import them.

`cumcm2025a_p4_p5/` is a recovered, bounded repair experiment with reviewed
small result fixtures. It depends on the ignored contained project for the
canonical geometry and data model.
