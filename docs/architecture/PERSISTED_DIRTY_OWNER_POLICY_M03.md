# Persisted Dirty-Owner Policy M0.3

Status: additive shadow conformance contract; no production call site or table
is changed.

Pure dirty classification and persisted solver ownership are different
behaviors. `persisted-dirty-owner-policy-v1` describes the existing
postprocessing sequence precisely:

1. classify a changed artifact using the static registry;
2. recognize only `.factory/solver_receipts/<job>.submitted.json` and
   `.completed.json`;
3. extract the `job_id` and query the recorded solver job;
4. preserve the classifier owner for a non-receipt, missing job or null owner;
5. otherwise convert the recorded owner to `int` and replace only
   `owner_stage`; flag, cause and both fingerprints are unchanged.

This recorded owner takes precedence over the static registry only for that
receipt path. Direct tests compare the pure model with the frozen
`FactoryEngine._solver_receipt_owner_stage` behavior.

The implementation identity is an AST source-span manifest, not a hash of all
of `engine.py`. It binds exactly:

- `dirty.py::_SOLVER_RECEIPT_RE`
- `dirty.py::solver_receipt_job_id`
- `engine.py::FactoryEngine._stage_manifest_delta`
- `engine.py::FactoryEngine._solver_receipt_owner_stage`
- `storage.py::SQLiteStateStore.solver_job`
- `storage.py::SQLiteStateStore._solver_job_from_row`

Every entry records repository-relative path, qualified symbol, the
`python-ast-source-span-v1` schema, line span, byte size and SHA-256. Runtime
uses checked-in values; build/evidence regenerates them. Historical dirty rows
without the new two policy pins are
`UNAVAILABLE_LEGACY_UNBOUND`, not silently attributed to current code.
