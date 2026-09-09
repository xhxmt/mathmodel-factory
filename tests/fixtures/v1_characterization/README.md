# v1 characterization corpus

This directory is a machine-readable index of the v1 behavior that exists at
commit `357947948f034325ea6202694c20bf435910d011`. Each case points to source and
an existing pytest characterization. The tests create disposable projects
under pytest `tmp_path`; the corpus does not copy or read a real Run4 SQLite
database and does not dispatch a model, provider, browser, or network request.

These files are descriptors, not claims that immutable production captures
exist. `v1_expected` records only assertions made by the named tests.
`missing_evidence` records every known difference between that synthetic proof
and a frozen end-to-end fixture. In particular, the repository has a two-stage
Solver receipt characterization but no checked-in ten-job receipt fixture or
machine-defined “10/10” scoring rubric; that gap is explicit in
`V1-SOLVER-10OF10-001`.

Run the read-only index/schema check with:

```bash
pytest -q tests/test_phase0_architecture_baseline.py
```

Run the behavior behind a case with the exact node IDs in its `pytest_nodes`
array. Adding a category requires a stable fixture ID, resolvable source paths,
machine-readable expectations, named contracts, and explicit gap IDs.
