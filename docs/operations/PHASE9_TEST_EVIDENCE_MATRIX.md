# Phase9 test evidence matrix

Status: this is the **immutable-package evidence contract**, not an outcome
report or independent verdict. A `CLOSED_OFFLINE` row in the companion TSV is
valid only inside a package whose cited command records, raw logs and summary
exist and pass the summary/builder checks; the checked-in TSV alone is not
evidence. The package requires seven source/fresh pairs: 14 command records and
14 complete raw logs.

The machine-readable authority for required suites and targets is
`PHASE9_TEST_SUITE_CONTRACT.json`. Every row below is required in both `source`
and `fresh` environments. Records and logs use the stable names
`command_records/{source|fresh}_<suite>_final.json` and
`test_logs/{source|fresh}_<suite>_final.log`.

| Suite ID | Required targets | Contract covered |
| --- | --- | --- |
| `phase9_focused` | `tests/test_phase9_entry_gate.py`; `tests/test_phase9_run_generation.py`; `tests/test_phase9_forensic_replay.py`; `tests/test_authority_production_migration.py`; `tests/test_phase9_delivery_fence.py`; `tests/test_phase9_p0_evidence.py`; `tests/test_phase9_acceptance_probes.py`; `tests/test_authority_outbox_delivery.py`; `tests/test_phase5_shadow_supervisor.py` | P0, entry, generation, A2_0017/A2_0018/A2_0019, typed replay/terminal, 17 acceptance probes, outbox/supervisor and delivery fence |
| `entry_ar007` | `tests/test_phase9_entry_gate.py`; `tests/test_phase9_p0_evidence.py`; `tests/test_phase9_delivery_fence.py`; `tests/test_atomic_release.py` | Nine formal P0 receipts, live gate, AR-007 and zero release side effects |
| `a2_migration` | `tests/test_authority_production_migration.py`; `tests/test_authority_operations.py` | Frozen A2_0010-A2_0018 bytes, additive A2_0019, Authority operations and interruption/restore |
| `phase1_8_continuous` | `tests/test_phase1_8_durable_continuous_chain.py`; `tests/test_m01_runtime_parity.py` | Phase1–8 durable identity/current chain and runtime parity |
| `phase7_8_regression` | `tests/test_phase78_enabled_e2e.py`; `tests/test_phase78_bootstrap_contract.py`; `tests/test_phase5_shadow_supervisor.py` | Phase7+8 enabled/bootstrap paths and durable supervisor restart regression |
| `release_workflow` | `tests/test_phase9_delivery_fence.py`; `tests/test_atomic_release.py`; `tests/test_delivery_contract.py`; `tests/test_workflow_state.py`; `tests/test_audit_service.py`; `tests/test_native_orchestration.py`; `tests/test_package_submission.py` | release, final acceptance/submission, submission packaging, and workflow-state delivery boundaries |
| `full_repository` | `tools/run_full_repo_with_frontend_deps.py` | Complete feasible repository suite with dependency preflight and all outcomes retained |

The runner contract records candidate commit/tree/parent, full executed-source
inventory, producer bytes, cwd, absolute interpreter, full argv, sanitized
environment, times, exit status and complete raw-log length/SHA-256. Every
command is launched through the recorded `/usr/bin/bwrap` identity and exact
argv: network is unshared, source and evidence roots are read-only, and only
per-invocation HOME/cache/TMP/basetemp directories are writable. A source run
must reject tracked/index dirt. A fresh run must validate Git identity and the
exported byte inventory separately; both modes mask the Python environment's
host checkout so an editable install cannot import another candidate.

The full-repository pair overlays `node_modules` without modifying the source
tree. It recursively hashes all dependency files, safe in-root symlinks and
paths before and after execution, binds that tree and the lockfile in each
record, and requires source/fresh dependency identities to match.

A conforming package's `evidence/FINAL_TEST_SUMMARY.json` is rebuilt from raw
logs, not from reported exit codes. It requires all 14 final records, retains
earlier failed attempts, validates required targets and exact source/fresh
candidate/source identity, and reports collected, passed, failed, errors,
skipped, xfailed, xpassed and warnings. Source/fresh exactness means equal parsed
outcome categories and exact ordered test-node/outcome inventories for each
suite, not merely equal exit codes, counts, or matching last lines. Pytest
evidence must contain exactly one terminal result summary and it
must be the final meaningful line; ambiguous or appended summaries fail closed.

No passed-count statement is evidence unless its complete command record and
raw log are packaged. Unbound aggregate counts are omitted. Package CRC,
one-root/path/mode/timestamp checks and the manifest/SHA256SUMS closure are
separate final-package verification, not a pytest suite. The portable verifier
also reconstructs the declared Git tree from every candidate-inventory row and
cross-binds every row to every recorded execution inventory.
