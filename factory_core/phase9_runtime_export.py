"""Exact, append-only recovery of dispatcher receipt materialization."""
from pathlib import Path
from .canonical import canonical_bytes, canonical_sha256
from .phase9_forensic_replay import _regular_file_bytes, _strict_json, _authority_runtime_source_sha256
from .phase9_replay_evidence import _runtime_record_body, _write_new


def write_equal(path, raw):
    path = Path(path)
    if path.resolve() != path or path.is_symlink():
        raise ValueError("export path is not canonical")
    if path.exists():
        if _regular_file_bytes(path, maximum=64 * 1024 * 1024, label="resumed export") != raw:
            raise ValueError("existing export bytes differ")
        return
    _write_new(path, raw)


def bind_equal(connection, binding):
    values = (binding["attempt_id"], binding["receipt_kind"], binding["replay_coordinate_sha256"],
              canonical_bytes(binding).decode(), binding["binding_sha256"])
    row = connection.execute("SELECT * FROM authority_production_phase9_runtime_receipt_bindings WHERE attempt_id=? AND receipt_kind=?", values[:2]).fetchone()
    if row is not None:
        if tuple(row) != values:
            raise ValueError("existing export receipt binding differs")
    else:
        connection.execute("INSERT INTO authority_production_phase9_runtime_receipt_bindings VALUES(?,?,?,?,?)", values)


def existing_record(connection, request, kind, role, path, body, raw):
    import hashlib
    row = connection.execute(
        "SELECT * FROM authority_production_phase9_replay_runtime_records WHERE workflow_id=? AND run_generation=? AND receipt_kind=? AND logical_id=?",
        (request.workflow_id, request.run_generation, kind, role)).fetchone()
    if row is None:
        return None
    expected = {"receipt_kind": kind, "logical_id": role, "logical_path": path,
                "raw_bytes_sha256": hashlib.sha256(raw).hexdigest(), "byte_length": len(raw),
                **{key: body[key] for key in ("receipt_sha256", "dependency_fingerprint_sha256", "input_sha256", "output_sha256", "invocation_id", "attempt_id", "process_scope_id")},
                "packet_sha256": body.get("packet_sha256")}
    record = _runtime_record_body(row)
    source = _authority_runtime_source_sha256(connection, request=request, **expected)
    if (any(row[key] != value for key, value in expected.items())
            or row["execution_domain"] != "FORMAL_PHASE9_A"
            or row["authority_source_sha256"] != source
            or canonical_sha256(record) != row["record_sha256"]
            or _strict_json(row["record_json"].encode(), "existing runtime record") != {**record, "record_sha256": row["record_sha256"]}):
        raise ValueError("existing runtime record differs from exact execution proof")
    return row["record_sha256"]


def stage(authority, runtime_id, root, name, value):
    body = {"schema": "phase9-runtime-export-stage-v1", "runtime_id": runtime_id,
            "evidence_root": str(root), "stage": name, "value_sha256": canonical_sha256(value)}
    digest = canonical_sha256(body)
    from .phase9_authority_lease import authority_state_commit_lease
    with authority_state_commit_lease(authority.project_root):
        connection = authority._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT stage_json,stage_sha256 FROM authority_production_phase9_runtime_export_stages WHERE runtime_id=? AND evidence_root=? AND stage=?", (runtime_id, str(root), name)).fetchone()
            encoded = canonical_bytes(body).decode()
            if row is not None:
                if tuple(row) != (encoded, digest):
                    raise ValueError("existing export stage differs")
            else:
                connection.execute("INSERT INTO authority_production_phase9_runtime_export_stages VALUES(?,?,?,?,?)", (runtime_id, str(root), name, encoded, digest))
            connection.commit()
        finally:
            connection.close()
