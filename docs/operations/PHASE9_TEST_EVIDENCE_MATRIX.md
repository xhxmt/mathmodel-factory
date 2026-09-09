# Phase9 test evidence matrix

Status: this is the **immutable-package evidence contract**, not an outcome
report or independent verdict. A `CLOSED_OFFLINE` row in the companion TSV is
valid only inside a package whose cited command records, raw logs and summary
exist and pass the summary/builder checks; the checked-in TSV alone is not
evidence. The package requires seven source/fresh pairs: 14 final command
records and 14 complete final raw logs, plus every append-only failed attempt.
The `full_repository` pair additionally requires two
parent-captured composite event artifacts; those artifacts are evidence records,
not synthetic pytest nodes.

The machine-readable authority for required suites and targets is
`PHASE9_TEST_SUITE_CONTRACT.json`. Every row below is required in both `source`
and `fresh` environments. Records and logs use the stable names
`command_records/{source|fresh}_<suite>_final.json` and
`test_logs/{source|fresh}_<suite>_final.log`.

| Suite ID | Required targets | Contract covered |
| --- | --- | --- |
| `phase9_focused` | `tests/test_phase9_entry_gate.py`; `tests/test_phase9_run_generation.py`; `tests/test_phase9_forensic_replay.py`; `tests/test_authority_production_migration.py`; `tests/test_phase9_delivery_fence.py`; `tests/test_phase9_p0_evidence.py`; `tests/test_phase9_acceptance_probes.py`; `tests/test_authority_outbox_delivery.py`; `tests/test_phase5_shadow_supervisor.py` | P0, entry, generation, immutable exact-recovery equality, global cross-workflow/orphan idempotency-key reservation, thread/process concurrency, A2_0017/A2_0018/A2_0019, typed replay/terminal, 17 acceptance probes, outbox/supervisor and delivery fence |
| `entry_ar007` | `tests/test_phase9_entry_gate.py`; `tests/test_phase9_p0_evidence.py`; `tests/test_phase9_delivery_fence.py`; `tests/test_atomic_release.py` | Nine formal P0 receipts, live gate, AR-007 and zero release side effects |
| `a2_migration` | `tests/test_authority_production_migration.py`; `tests/test_authority_operations.py` | Frozen A2_0010-A2_0018 bytes, additive A2_0019, Authority operations and interruption/restore |
| `phase1_8_continuous` | `tests/test_phase1_8_durable_continuous_chain.py`; `tests/test_m01_runtime_parity.py` | Phase1–8 durable identity/current chain and runtime parity |
| `phase7_8_regression` | `tests/test_phase78_enabled_e2e.py`; `tests/test_phase78_bootstrap_contract.py`; `tests/test_phase5_shadow_supervisor.py` | Phase7+8 enabled/bootstrap paths and durable supervisor restart regression |
| `release_workflow` | `tests/test_phase9_delivery_fence.py`; `tests/test_atomic_release.py`; `tests/test_delivery_contract.py`; `tests/test_workflow_state.py`; `tests/test_audit_service.py`; `tests/test_native_orchestration.py`; `tests/test_package_submission.py` | release, final acceptance/submission, submission packaging, and workflow-state delivery boundaries |
| `full_repository` | `tools/run_full_repo_with_frontend_deps.py`; stages `python_pytest`, `frontend_production_build`, `phase6_browser` | All Python tests, a production frontend build, and the documented `npm run test:phase6` Chromium tests; every stage and all non-pass outcomes retained |

The `full_repository.composite_stages` array is the machine-readable projection
of the single runner-owned stage definition. Each row fixes the symbolic argv,
cwd, executed source targets, and (for frontend stages) exact npm-script
name/body. Runner execution, summary reconstruction and portable bundle
verification all require exact equality; `required_stages` alone is not enough.

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
tree and mounts one explicit browser runtime read-only. It recursively hashes
both external trees, all safe in-root symlinks and paths before and after
execution, and binds the lockfile, Node/npm versions, browser executable bytes
and browser version in each record. Source and fresh must use the same normalized
stage contract and dependency/runtime identities. Both frontend stage argv use
the byte-bound Node to invoke the resolved, byte-bound npm CLI directly; a Node
that appears only in a version probe is rejected. The standalone production
build uses Vite's `--configLoader runner` to bypass the default bundled-config
path that tries to materialize `.vite-temp` below read-only `node_modules`, and
writes its output only to that invocation's private basetemp; the Phase 6 stage gets
the recorded browser executable through `PHASE6_CHROMIUM_EXECUTABLE`. A missing
locked development dependency or browser runtime is a failure, never a skip.
The candidate-bound `package.json` must retain the exact `vite build` command
and the documented two-file `test:phase6` Node command. An exit-zero build is
still non-passing unless its private outDir contains a safe, nonempty
`index.html` and `assets/` tree whose complete path/byte/SHA-256 inventory is
reproducible after all stages and exactly matches between source and fresh.

The parent drains the pytest and composite pipes concurrently. The composite
artifact fixes the exact ordered argv, cwd, sanitized environment, exit code,
timing, and SHA-256/byte slice in the complete raw log for each of
`python_pytest`, `frontend_production_build`, and `phase6_browser`. The first
nonzero stage determines the composite exit after all three stages have run, so
a later success cannot erase an earlier failure. A zero-exit browser command is
converted by the child runner to stage exit 88 unless its unique Node test
summary reports at least one test, all tests passed, and zero
failed/cancelled/skipped/todo tests. The parent independently repeats that TAP
check, so an altered stage event cannot restore PASS.

A conforming package's `evidence/FINAL_TEST_SUMMARY.json` is rebuilt from raw
logs, not from reported exit codes. It requires all 14 final records, retains
earlier failed attempts, validates required targets and exact source/fresh
candidate/source identity, and reports collected, passed, failed, errors,
skipped, xfailed, xpassed and warnings. Source/fresh exactness means equal parsed
outcome categories and exact ordered test-node/outcome inventories for each
suite, not merely equal exit codes or counts. For a composite record, Python
statistics are parsed only from the byte-exact `python_pytest` slice; build and
browser output after that slice cannot obscure or impersonate its pytest
summary. The overall result also requires zero exits for all stages, a complete
browser PASS, and exact source/fresh composite-contract hashes. Ambiguous,
appended, missing, reordered, or hash-mismatched stage evidence fails closed.
The browser contract includes the ordered TAP test identity/outcome inventory,
so two environments with equal aggregate counts but different collected browser
nodes are not exact and cannot produce an overall PASS. Browser-launch coverage
is supplied by the two candidate-bound test modules themselves; the composite
transport does not claim a second, independent browser-launch observer.

No passed-count statement is evidence unless its complete command record and
raw log are packaged. Unbound aggregate counts are omitted. Package CRC,
one-root/path/mode/timestamp checks and the manifest/SHA256SUMS closure are
separate final-package verification, not a pytest suite. The portable verifier
also reconstructs the declared Git tree from every candidate-inventory row and
cross-binds every row to every recorded execution inventory.

Requirement-map and portable semantic verification remain mandatory. The map
verifier recognizes only Python files reached by the default full-repository
pytest collection and the two exact documented Phase 6 browser modules; citing
`full_repository` cannot make an arbitrary file count as an executed test, and
both browser modules must be reachable from a closed requirement. It also
requires every concrete composite target to be reachable in the proper column:
`web/frontend/package.json` as an implementation and both browser modules as
tests. The abstract `tests` target denotes the complete default Python
collection and is proven by the trusted pytest event inventory.

A failure before the child process starts uses the distinct
`paper-factory-phase9-audit-preflight-failure-v1` record. It preserves the
original requested argv, candidate/source inventory, producer and Python byte
identity when available, exact failure stage/type, wrapper exit 125, and one
canonical raw failure event with `process_started=false`. It deliberately has
no dependency inventory, pytest event, composite event, stage exit, or test
count. The summary and portable package verifier retain and validate this as a
failed attempt, but only the exact source/fresh `*_final` execution records can
satisfy the required suite set.

That record is created only after a safe evidence coordinate and initial source
inventory exist. Failures while establishing those prerequisites—missing,
aliased or non-ordinary audit/source roots; noncanonical, escaped, occupied or
unsafe record/log paths; or an unprovable initial source inventory—are reported
as `NON_RECORDABLE_INVOCATION_VALIDATION` with zero evidence writes. This is a
fail-closed boundary, not an omitted failed attempt: before it, no trustworthy
append-only artifact location or candidate identity exists to record.
