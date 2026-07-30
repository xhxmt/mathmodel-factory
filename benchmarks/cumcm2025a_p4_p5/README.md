# CUMCM 2025A P4/P5 Repair Experiment

This package preserves the useful output recovered from the
`fix/cumcm2025a-p4-p5-model` worktree without committing the generated paper
project. It is an experimental benchmark, not a replacement for the canonical
delivery under `complete/cumcm_2025_a_current_pass`.

## Contents

- `src/10_repaired_p4_p5.py`: corrected search horizons, feasible schedule
  decoding, multi-start P4 search, and globally rescored P5 candidates.
- `src/11_repaired_p5_cloud.py`: historical bounded Cloud Run experiment
  entrypoint. Its recorded run used a compatibility numeric stack and required
  a local audited rescore.
- `src/12_rescore_p5_cloud.py`: local rescore of a Cloud-produced P5 candidate.
- `tests/test_repaired_p4_p5_solver.py`: 14 regression tests against the
  contained CUMCM project.
- `results/`: recovered experimental result and comparison records.

## Reproduce

The contained project pins Python 3.13.5, NumPy 2.4.6, and SciPy 1.17.1 in its
model contract. On this host, the system `python3` provides that environment:

```bash
python3 -m pytest -q benchmarks/cumcm2025a_p4_p5/tests
python3 benchmarks/cumcm2025a_p4_p5/src/10_repaired_p4_p5.py \
  --project complete/cumcm_2025_a_current_pass --only p4
```

Set `CUMCM2025A_PROJECT` when the contained project is elsewhere. Do not use
the repository Cloud extra for result reproduction unless its NumPy/SciPy
versions match the project contract.

## Recorded Result

- P4: `EXPERIMENTAL_FEASIBLE`, objective 11.52.
- P5: `EXPERIMENTAL_FEASIBLE`; bounded search best-total objective 15.2 and
  best-fairness objective 13.6.
- The experiment does not prove a global optimum and must not mutate current
  delivery manifests, PDFs, or `CURRENT_PASS` classification.
