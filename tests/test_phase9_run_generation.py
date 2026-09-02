from __future__ import annotations

from dataclasses import replace
import hashlib
import os
from pathlib import Path
import pwd
import sqlite3

import pytest

from factory_core.authority_operations import AuthorityOperations
from factory_core.contract_pins import compile_contract_pin_set
from factory_core.canonical import canonical_bytes
from factory_core.phase9_run_generation import (
    CREATE,
    DELIVERY_DISABLED,
    EXECUTION_CONTEXT_EVIDENCE_SCHEMA,
    GIT_SOURCE_IDENTITY_SCHEMA,
    OFFICIAL_INPUT_FILE_EVIDENCE_SCHEMA,
    OFFICIAL_INPUT_MANIFEST_EVIDENCE_SCHEMA,
    OPERATOR_AUTHORIZATION_EVIDENCE_SCHEMA,
    ROTATE,
    RUN_GENERATION_REQUEST_SCHEMA,
    ExecutionContextEvidenceV1,
    GitSourceIdentityV1,
    OfficialInputFileEvidenceV1,
    OfficialInputManifestEvidenceV1,
    OperatorAuthorizationEvidenceV1,
    Phase9RunGenerationConflict,
    Phase9RunGenerationSafetyError,
    Phase9RunGenerationService,
    RunGenerationRequestV1,
    read_current_git_source_identity,
    run_generation_request_from_dict,
)
from factory_core.workflow_contract_v2 import compile_workflow_contract_bundle_v2
from tests.support.authority_production import install_foundation


DEFAULT_SOURCE_REPOSITORY = Path(__file__).resolve().parents[1]
OFFICIAL_BYTES = b"verified official phase9 bytes\n"
RUN_TABLES = (
    "authority_production_run_generations",
    "authority_production_run_generation_current",
    "authority_production_run_generation_creation_receipts",
    "authority_production_run_generation_idempotency",
    "authority_production_run_generation_successions",
)


def _source_repository() -> Path:
    """Use a real Git identity root when code runs from a no-.git extraction."""

    raw = os.environ.get("PHASE9_TEST_SOURCE_REPOSITORY")
    if raw is None:
        return DEFAULT_SOURCE_REPOSITORY
    path = Path(raw)
    if not path.is_absolute():
        raise AssertionError("PHASE9_TEST_SOURCE_REPOSITORY must be absolute")
    return path


def _request(
    *,
    operation_kind: str = CREATE,
    key: str = "generation-key-1",
    predecessor: str | None = None,
    predecessor_receipt: str | None = None,
    occurred_at: int = 2000,
) -> RunGenerationRequestV1:
    source = read_current_git_source_identity(_source_repository())
    authorization = OperatorAuthorizationEvidenceV1(
        OPERATOR_AUTHORIZATION_EVIDENCE_SCHEMA,
        f"authorization-{key}",
        "CONTROLLED_OS_ACCOUNT",
        "6" * 64,
        True,
        os.geteuid(),
        pwd.getpwuid(os.geteuid()).pw_name,
        "product-owner",
        "phase9-operator",
        operation_kind,
        "demo",
        "legacy_current",
        source.source_commit,
        1900,
        3000,
        "1" * 64,
    )
    request = RunGenerationRequestV1(
        RUN_GENERATION_REQUEST_SCHEMA,
        key,
        operation_kind,
        "demo",
        "legacy_current",
        1,
        "project-generation-phase9-1",
        "native_v2",
        "stage_v1",
        predecessor,
        predecessor_receipt,
        "FORENSIC_REPLAY",
        "LEGACY_NOT_APPLICABLE",
        DELIVERY_DISABLED,
        source,
        compile_contract_pin_set(compile_workflow_contract_bundle_v2()),
        OfficialInputManifestEvidenceV1(
            OFFICIAL_INPUT_MANIFEST_EVIDENCE_SCHEMA,
            "official-input-generation-1",
            (
                OfficialInputFileEvidenceV1(
                    OFFICIAL_INPUT_FILE_EVIDENCE_SCHEMA,
                    "official/problem.pdf",
                    len(OFFICIAL_BYTES),
                    hashlib.sha256(OFFICIAL_BYTES).hexdigest(),
                ),
            ),
        ),
        ExecutionContextEvidenceV1(
            EXECUTION_CONTEXT_EVIDENCE_SCHEMA,
            "phase9-context-1",
            "3" * 64,
            "4" * 64,
            "5" * 64,
            1950,
        ),
        authorization,
        occurred_at,
    )
    return replace(
        request,
        project_generation=request.derived_project_generation,
    )


def _evidence_paths(fixture, request, *, write=True):
    root = fixture.project_dir.parent / f"{fixture.project_dir.name}-official-inputs"
    official = root / "official" / "problem.pdf"
    context = fixture.project_dir.parent / f"{fixture.project_dir.name}-context.json"
    if write:
        official.parent.mkdir(parents=True, exist_ok=True)
        official.write_bytes(OFFICIAL_BYTES)
        context.write_bytes(canonical_bytes(request.execution_context.as_dict()))
    return root, context


def _service(
    fixture, *, request=None, fault_hook=None, prepare_evidence=True
) -> Phase9RunGenerationService:
    value = _request() if request is None else request
    official_root, context = _evidence_paths(
        fixture, value, write=prepare_evidence
    )
    return Phase9RunGenerationService(
        fixture.database,
        expected_source_fence_sha256=fixture.preflight.source_fence_sha256,
        source_repository=_source_repository(),
        official_input_root=official_root,
        execution_context_receipt_path=context,
        fault_hook=fault_hook,
        clock=lambda: 2000,
    )


def _counts(database: Path) -> dict[str, int]:
    connection = sqlite3.connect(database)
    try:
        return {
            table: connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            for table in RUN_TABLES
        }
    finally:
        connection.close()


def test_atomic_create_binds_candidate_inputs_coordinates_and_exact_replay(tmp_path):
    fixture = install_foundation(tmp_path)
    request = _request()
    service = _service(fixture, request=request)

    first = service.create_or_rotate(request)
    replay = service.create_or_rotate(request)

    assert first.run_generation == f"run-generation:{request.request_sha256}"
    assert first.replayed is False
    assert replay == replace(first, replayed=True)
    connection = sqlite3.connect(fixture.database)
    connection.row_factory = sqlite3.Row
    try:
        generation = connection.execute(
            "SELECT * FROM authority_production_run_generations"
        ).fetchone()
        current = connection.execute(
            "SELECT * FROM authority_production_run_generation_current"
        ).fetchone()
        workflow = connection.execute(
            "SELECT * FROM authority_workflows WHERE workflow_id='legacy_current'"
        ).fetchone()
        receipt = connection.execute(
            "SELECT * FROM authority_production_run_generation_creation_receipts"
        ).fetchone()
    finally:
        connection.close()
    assert generation["run_generation"] == first.run_generation
    assert generation["source_commit"] == request.source.source_commit
    assert generation["source_tree"] == request.source.source_tree
    assert generation["source_parent"] == request.source.source_parent
    assert generation["delivery_capability"] == "DISABLED"
    assert generation["official_input_manifest_sha256"] == (
        request.official_inputs.manifest_sha256
    )
    assert generation["official_input_raw_bytes_set_sha256"] == (
        request.official_inputs.raw_bytes_set_sha256
    )
    assert current["run_generation"] == first.run_generation
    assert current["creation_receipt_sha256"] == first.receipt_sha256
    assert workflow["project_generation"] == request.project_generation
    assert workflow["run_generation"] == first.run_generation
    assert workflow["runtime_generation"] == request.runtime_generation
    assert workflow["scheduler_generation"] == request.scheduler_generation
    assert workflow["authority_state"] == "RECORDED_SHADOW"
    assert receipt["receipt_sha256"] == first.receipt_sha256
    assert _counts(fixture.database) == {table: 1 for table in RUN_TABLES}


def test_typed_request_json_round_trip_is_canonical_identity_stable():
    request = _request()
    decoded = run_generation_request_from_dict(
        request.as_dict(), trusted_now=2000
    )
    assert decoded == request
    assert decoded.request_sha256 == request.request_sha256
    assert decoded.derived_run_generation == request.derived_run_generation


def test_same_key_different_canonical_request_conflicts_without_mutation(tmp_path):
    fixture = install_foundation(tmp_path)
    request = _request()
    service = _service(fixture, request=request)
    service.create_or_rotate(request)
    before = _counts(fixture.database)

    with pytest.raises(Phase9RunGenerationConflict, match="different request bytes"):
        service.create_or_rotate(replace(request, occurred_at=2001))

    assert _counts(fixture.database) == before


@pytest.mark.parametrize(
    "checkpoint",
    (
        "after_contract_pin",
        "after_generation",
        "after_receipt",
        "after_current_pointer",
        "before_commit",
    ),
)
def test_faults_rollback_pin_generation_receipt_pointer_and_workflow(
    tmp_path, checkpoint
):
    fixture = install_foundation(tmp_path)

    def fail(actual: str) -> None:
        if actual == checkpoint:
            raise RuntimeError(f"fault:{checkpoint}")

    with pytest.raises(RuntimeError, match=f"fault:{checkpoint}"):
        _service(fixture, fault_hook=fail).create_or_rotate(_request())

    assert _counts(fixture.database) == {table: 0 for table in RUN_TABLES}
    connection = sqlite3.connect(fixture.database)
    try:
        pins = connection.execute(
            "SELECT COUNT(*) FROM authority_contract_pin_sets"
        ).fetchone()[0]
        workflow = connection.execute(
            "SELECT project_generation, run_generation FROM authority_workflows "
            "WHERE workflow_id='legacy_current'"
        ).fetchone()
    finally:
        connection.close()
    assert pins == 0
    assert workflow == ("legacy_unknown", "legacy_unknown")


def test_rotate_requires_and_persists_exact_concrete_predecessor_receipt(tmp_path):
    fixture = install_foundation(tmp_path)
    service = _service(fixture)
    first = service.create_or_rotate(_request())
    rotate = _request(
        operation_kind=ROTATE,
        key="generation-key-2",
        predecessor=first.run_generation,
        predecessor_receipt=first.receipt_sha256,
        occurred_at=2001,
    )

    second = service.create_or_rotate(rotate)

    assert second.operation_kind == ROTATE
    connection = sqlite3.connect(fixture.database)
    connection.row_factory = sqlite3.Row
    try:
        current = connection.execute(
            "SELECT * FROM authority_production_run_generation_current"
        ).fetchone()
        succession = connection.execute(
            "SELECT * FROM authority_production_run_generation_successions "
            "WHERE run_generation=?",
            (second.run_generation,),
        ).fetchone()
    finally:
        connection.close()
    assert current["run_generation"] == second.run_generation
    assert succession["predecessor_run_generation"] == first.run_generation
    assert succession["predecessor_creation_receipt_sha256"] == first.receipt_sha256
    assert _counts(fixture.database) == {
        RUN_TABLES[0]: 2,
        RUN_TABLES[1]: 1,
        RUN_TABLES[2]: 2,
        RUN_TABLES[3]: 2,
        RUN_TABLES[4]: 2,
    }


def test_rotate_rejects_wrong_receipt_and_keeps_current_generation(tmp_path):
    fixture = install_foundation(tmp_path)
    service = _service(fixture)
    first = service.create_or_rotate(_request())
    rotate = _request(
        operation_kind=ROTATE,
        key="generation-key-2",
        predecessor=first.run_generation,
        predecessor_receipt="f" * 64,
        occurred_at=2001,
    )

    with pytest.raises(Phase9RunGenerationConflict, match="predecessor/current"):
        service.create_or_rotate(rotate)

    assert _counts(fixture.database) == {
        table: 1 for table in RUN_TABLES
    }


def test_database_guards_reject_direct_pointer_or_workflow_generation_updates(
    tmp_path,
):
    fixture = install_foundation(tmp_path)
    result = _service(fixture).create_or_rotate(_request())
    connection = sqlite3.connect(fixture.database)
    try:
        with pytest.raises(sqlite3.DatabaseError, match="succession graph"):
            connection.execute(
                "UPDATE authority_production_run_generation_current "
                "SET updated_at=updated_at+1 WHERE workflow_id='legacy_current'"
            )
        connection.rollback()
        with pytest.raises(sqlite3.DatabaseError, match="companion graph"):
            connection.execute(
                "UPDATE authority_workflows SET run_generation='invented-run' "
                "WHERE workflow_id='legacy_current'"
            )
        connection.rollback()
        current = connection.execute(
            "SELECT run_generation FROM authority_production_run_generation_current"
        ).fetchone()[0]
    finally:
        connection.close()
    assert current == result.run_generation


def test_default_off_fence_rejects_enabled_writer(tmp_path):
    fixture = install_foundation(tmp_path)
    operations = AuthorityOperations(
        fixture.database,
        expected_source_fence_sha256=fixture.preflight.source_fence_sha256,
    )
    operations.configure_writer(
        new_writer_id="writer-a",
        enabled=True,
        expected_writer_epoch=0,
        expected_switch_epoch=0,
        operator_subject="operator-a",
        reason="exercise default-off generation fence",
        occurred_at=1500,
    )

    with pytest.raises(Phase9RunGenerationSafetyError, match="writer and consumer disabled"):
        official_root, context = _evidence_paths(fixture, _request())
        operations.create_or_rotate_run_generation(
            _request(), source_repository=_source_repository(),
            official_input_root=official_root,
            execution_context_receipt_path=context,
            clock=lambda: 2000,
        )

    assert _counts(fixture.database) == {table: 0 for table in RUN_TABLES}


def test_request_rejects_legacy_generation_and_unbound_authorization(tmp_path):
    fixture = install_foundation(tmp_path)
    request = _request()
    with pytest.raises(Phase9RunGenerationSafetyError, match="must be concrete"):
        _service(fixture).create_or_rotate(
            replace(request, runtime_generation="legacy_unknown")
        )

    wrong_auth = replace(request.operator_authorization, workflow_id="other-workflow")
    with pytest.raises(Phase9RunGenerationSafetyError, match="differs from request"):
        _service(fixture).create_or_rotate(
            replace(request, operator_authorization=wrong_auth)
        )
    assert _counts(fixture.database) == {table: 0 for table in RUN_TABLES}


def test_create_authorization_uses_trusted_clock_not_backfilled_occurrence(tmp_path):
    fixture = install_foundation(tmp_path)
    request = _request(occurred_at=1940)
    request = replace(
        request,
        execution_context=replace(request.execution_context, captured_at=1930),
        operator_authorization=replace(
            request.operator_authorization,
            issued_at=1900,
            expires_at=1950,
        ),
    )
    with pytest.raises(
        Phase9RunGenerationSafetyError,
        match="not valid at trusted current time",
    ):
        _service(fixture, request=request).create_or_rotate(request)
    assert _counts(fixture.database) == {table: 0 for table in RUN_TABLES}


def test_create_rejects_future_authorization_and_request_clock_skew(tmp_path):
    fixture = install_foundation(tmp_path)
    request = _request()
    future = replace(
        request,
        operator_authorization=replace(
            request.operator_authorization,
            issued_at=2001,
            expires_at=3000,
        ),
    )
    with pytest.raises(
        Phase9RunGenerationSafetyError,
        match="not valid at trusted current time",
    ):
        _service(fixture, request=future).create_or_rotate(future)

    skewed = replace(
        request,
        occurred_at=1600,
        execution_context=replace(request.execution_context, captured_at=1500),
    )
    with pytest.raises(
        Phase9RunGenerationSafetyError,
        match="exceeds trusted clock skew",
    ):
        _service(fixture, request=skewed).create_or_rotate(skewed)
    assert _counts(fixture.database) == {table: 0 for table in RUN_TABLES}


def test_create_rejects_invented_project_generation_and_unverified_os_identity(
    tmp_path,
):
    fixture = install_foundation(tmp_path)
    request = _request()
    with pytest.raises(Phase9RunGenerationSafetyError, match="must be derived"):
        _service(fixture).create_or_rotate(
            replace(request, project_generation="invented-project-label")
        )

    wrong_uid = replace(
        request.operator_authorization,
        operator_uid=request.operator_authorization.operator_uid + 1,
    )
    with pytest.raises(Phase9RunGenerationSafetyError, match="executing OS account"):
        _service(fixture).create_or_rotate(
            replace(request, operator_authorization=wrong_uid)
        )

    unsupported = replace(
        request.operator_authorization,
        authorization_mechanism="SIGNED_AUTHORIZATION",
    )
    with pytest.raises(Phase9RunGenerationSafetyError, match="only verified"):
        _service(fixture).create_or_rotate(
            replace(request, operator_authorization=unsupported)
        )
    assert _counts(fixture.database) == {table: 0 for table in RUN_TABLES}


def test_request_source_must_equal_live_commit_tree_and_parent(tmp_path):
    fixture = install_foundation(tmp_path)
    request = _request()
    wrong = GitSourceIdentityV1(
        GIT_SOURCE_IDENTITY_SCHEMA,
        "f" * 40,
        request.source.source_tree,
        request.source.source_parent,
    )
    wrong_auth = replace(
        request.operator_authorization,
        source_commit=wrong.source_commit,
    )
    wrong_request = replace(
        request, source=wrong, operator_authorization=wrong_auth
    )
    wrong_request = replace(
        wrong_request,
        project_generation=wrong_request.derived_project_generation,
    )
    with pytest.raises(Phase9RunGenerationConflict, match="not current"):
        _service(fixture).create_or_rotate(wrong_request)
    assert _counts(fixture.database) == {table: 0 for table in RUN_TABLES}


def test_create_reads_real_official_bytes_and_rejects_wrong_or_extra_files(tmp_path):
    fixture = install_foundation(tmp_path)
    request = _request()
    root, _context = _evidence_paths(fixture, request)
    official = root / "official" / "problem.pdf"
    official.write_bytes(b"X" + OFFICIAL_BYTES[1:])
    with pytest.raises(Phase9RunGenerationSafetyError, match="official input bytes differ"):
        _service(
            fixture, request=request, prepare_evidence=False
        ).create_or_rotate(request)

    official.write_bytes(OFFICIAL_BYTES)
    (root / "unexpected.txt").write_bytes(b"not in manifest")
    with pytest.raises(Phase9RunGenerationSafetyError, match="inventory differs"):
        _service(
            fixture, request=request, prepare_evidence=False
        ).create_or_rotate(request)
    assert _counts(fixture.database) == {table: 0 for table in RUN_TABLES}


def test_create_rejects_symlinked_official_input(tmp_path):
    fixture = install_foundation(tmp_path)
    request = _request()
    root, _context = _evidence_paths(fixture, request)
    official = root / "official" / "problem.pdf"
    target = fixture.project_dir.parent / "outside-official.bin"
    target.write_bytes(OFFICIAL_BYTES)
    official.unlink()
    os.symlink(target, official)

    with pytest.raises(Phase9RunGenerationSafetyError, match="symlink"):
        _service(
            fixture, request=request, prepare_evidence=False
        ).create_or_rotate(request)
    assert _counts(fixture.database) == {table: 0 for table in RUN_TABLES}


def test_create_requires_exact_canonical_execution_context_receipt(tmp_path):
    fixture = install_foundation(tmp_path)
    request = _request()
    _root, context = _evidence_paths(fixture, request)
    context.write_bytes(canonical_bytes({"schema_version": "invented-context"}))

    with pytest.raises(
        Phase9RunGenerationSafetyError,
        match="execution context receipt canonical bytes/hash differ",
    ):
        _service(
            fixture, request=request, prepare_evidence=False
        ).create_or_rotate(request)
    assert _counts(fixture.database) == {table: 0 for table in RUN_TABLES}


def test_official_input_toctou_before_commit_rolls_back_every_row(tmp_path):
    fixture = install_foundation(tmp_path)
    request = _request()
    root, _context = _evidence_paths(fixture, request)
    official = root / "official" / "problem.pdf"

    def mutate(checkpoint: str) -> None:
        if checkpoint == "after_receipt":
            official.write_bytes(b"X" + OFFICIAL_BYTES[1:])

    with pytest.raises(Phase9RunGenerationSafetyError, match="official input bytes differ"):
        _service(
            fixture,
            request=request,
            fault_hook=mutate,
            prepare_evidence=False,
        ).create_or_rotate(request)
    assert _counts(fixture.database) == {table: 0 for table in RUN_TABLES}


def test_execution_context_toctou_before_commit_rolls_back_every_row(tmp_path):
    fixture = install_foundation(tmp_path)
    request = _request()
    _root, context = _evidence_paths(fixture, request)

    def mutate(checkpoint: str) -> None:
        if checkpoint == "after_receipt":
            context.write_bytes(b"{}")

    with pytest.raises(
        Phase9RunGenerationSafetyError,
        match="execution context receipt canonical bytes/hash differ",
    ):
        _service(
            fixture,
            request=request,
            fault_hook=mutate,
            prepare_evidence=False,
        ).create_or_rotate(request)
    assert _counts(fixture.database) == {table: 0 for table in RUN_TABLES}
