from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest

from factory_core.canonical import canonical_bytes, canonical_sha256
from tools.build_phase9_audit_bundle import (
    AUDITED_BASELINE_IDENTITY,
    DEFAULT_SOURCE_PATHS,
    FULL_REPOSITORY_BROWSER_TEST_PATHS,
    PACKAGE_README,
    PRODUCTION_BLOCK_REASON,
    _build_for_policy,
    _expected_evidence_paths,
    _manifest,
    _full_repository_executes_test_path,
    _secret_scan,
    _verify_checksum_payload,
    _verify_extracted_package_for_policy,
    _verify_manifest_payload,
    _verify_requirement_map,
    build,
    build_review_status,
)
from tools.build_phase9_test_summary import _build_summary_for_policy
from tools.run_audit_command import main as run_audit_command
from tools.run_full_repo_with_frontend_deps import composite_stage_contract


SOURCE_REPOSITORY = Path(__file__).resolve().parents[1]
SAMPLE_SUITE_SPECS = {
    "sample": {
        "kind": "pytest",
        "required_stages": [],
        "requirements": ["P9-SAMPLE"],
        "required_targets": ["tests/test_sample.py"],
    }
}
SAMPLE_SOURCE_PATHS = (
    "docs/operations/PHASE9_REQUIREMENT_IMPLEMENTATION_TEST_EVIDENCE_MAP.tsv",
    "docs/operations/PHASE9_TEST_SUITE_CONTRACT.json",
    "tests/test_sample.py",
    "tools/run_audit_command.py",
    "tools/trusted_pytest_reporter.py",
    "tracked.txt",
)


def _git(repository: Path, *arguments: str) -> bytes:
    return subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", *arguments],
        cwd=repository,
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={
            "PATH": "/usr/bin:/bin",
            "LC_ALL": "C",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
        },
    ).stdout


def _repository(tmp_path: Path) -> tuple[Path, Path]:
    repository = tmp_path / "repo"
    repository.mkdir()
    _git(repository, "init", "-q")
    _git(repository, "config", "user.name", "Phase9 Test")
    _git(repository, "config", "user.email", "phase9-test@example.invalid")
    (repository / "parent.txt").write_text("parent\n", encoding="utf-8")
    _git(repository, "add", "--", "parent.txt")
    _git(repository, "commit", "-qm", "parent")

    for directory in ("factory_core", "tools", "tests", "docs/operations"):
        (repository / directory).mkdir(parents=True, exist_ok=True)
    (repository / "factory_core/__init__.py").write_text("", encoding="utf-8")
    for relative in (
        "factory_core/canonical.py",
        "tools/run_audit_command.py",
        "tools/trusted_pytest_reporter.py",
    ):
        (repository / relative).write_bytes((SOURCE_REPOSITORY / relative).read_bytes())
    (repository / "tests/test_sample.py").write_text(
        "def test_candidate():\n    assert 6 * 7 == 42\n", encoding="utf-8"
    )
    (repository / "tracked.txt").write_text("candidate\n", encoding="utf-8")
    contract = {
        "schema": "paper-factory-phase9-test-suite-contract-v3",
        "required_environments": ["fresh", "source"],
        "suites": [
            {
                "id": "sample",
                "kind": "pytest",
                "required_stages": [],
                "requirements": ["P9-SAMPLE"],
                "description": "deterministic package test",
                "required_targets": ["tests/test_sample.py"],
            }
        ],
    }
    contract_path = repository / "docs/operations/PHASE9_TEST_SUITE_CONTRACT.json"
    contract_path.write_bytes(canonical_bytes(contract) + b"\n")
    mapping = (
        "requirement_id\tstatus\timplementation\ttests\tcommand_records\t"
        "raw_logs\tsummary_evidence\n"
        "P9-SAMPLE\tCLOSED_OFFLINE\ttools/run_audit_command.py\t"
        "tests/test_sample.py\tcommand_records/source_sample_final.json;"
        "command_records/fresh_sample_final.json\t"
        "test_logs/source_sample_final.log;test_logs/fresh_sample_final.log\t"
        "evidence/FINAL_TEST_SUMMARY.json\n"
        "P9-PRODUCTION-RUN\tBLOCKED_EXTERNAL\ttools/run_audit_command.py\t"
        "tests/test_sample.py\t\t\tevidence/PRODUCTION_STATUS.json\n"
    )
    (repository / "docs/operations/PHASE9_REQUIREMENT_IMPLEMENTATION_TEST_EVIDENCE_MAP.tsv").write_text(
        mapping, encoding="utf-8"
    )
    _git(repository, "add", "--", ".")
    _git(repository, "commit", "-qm", "candidate")
    return repository, contract_path


def _fresh_copy(repository: Path, root: Path) -> Path:
    fresh = root / "fresh"
    fresh.mkdir()
    inventory = _git(repository, "ls-tree", "-rz", "--name-only", "HEAD")
    for encoded in inventory.split(b"\0"):
        if not encoded:
            continue
        relative = encoded.decode("utf-8")
        target = fresh / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(_git(repository, "show", f"HEAD:{relative}"))
    return fresh


def _run_record(
    repository: Path, audit: Path, cwd: Path, environment: str
) -> None:
    exit_code = run_audit_command(
        [
            "--id", f"{environment}_sample_final",
            "--suite", "sample",
            "--environment", environment,
            "--kind", "pytest",
            "--repository", str(repository),
            "--audit-root", str(audit),
            "--cwd", str(cwd),
            "--log", str(audit / f"test_logs/{environment}_sample_final.log"),
            "--record", str(audit / f"command_records/{environment}_sample_final.json"),
            "--", sys.executable, "-B", "-m", "pytest", "-q", "-p",
            "no:cacheprovider",
            f"--basetemp={audit / 'runtime' / (environment + '_sample_final-pytest') / 'basetemp'}",
            "tests/test_sample.py",
        ]
    )
    assert exit_code == 0


def _audit_evidence(
    repository: Path,
    contract: Path,
    tmp_path: Path,
    *,
    include_preflight_failure: bool = False,
) -> Path:
    audit = tmp_path / "audit"
    audit.mkdir()
    fresh = _fresh_copy(repository, tmp_path)
    if include_preflight_failure:
        identifier = "source_sample_missing_python"
        missing_python = tmp_path / "missing-python"
        exit_code = run_audit_command(
            [
                "--id", identifier,
                "--suite", "sample",
                "--environment", "source",
                "--kind", "pytest",
                "--repository", str(repository),
                "--audit-root", str(audit),
                "--cwd", str(repository),
                "--log", str(audit / f"test_logs/{identifier}.log"),
                "--record", str(audit / f"command_records/{identifier}.json"),
                "--", str(missing_python), "-B", "-m", "pytest", "-q", "-p",
                "no:cacheprovider",
                f"--basetemp={audit / 'runtime' / (identifier + '-pytest') / 'basetemp'}",
                "tests/test_sample.py",
            ]
        )
        assert exit_code == 125
    _run_record(repository, audit, repository, "source")
    _run_record(repository, audit, fresh, "fresh")
    summary = _build_summary_for_policy(
        audit_root=audit,
        records_root=audit / "command_records",
        suite_contract_path=contract,
        expected_suite_specs=SAMPLE_SUITE_SPECS,
        runtime_validation=True,
    )
    (audit / "evidence/FINAL_TEST_SUMMARY.json").write_bytes(
        canonical_bytes(summary) + b"\n"
    )
    commit, _parent = _git(
        repository, "rev-list", "--parents", "-n", "1", "HEAD"
    ).decode().split()
    production = {
        "schema": "paper-factory-phase9-production-status-v1",
        "candidate_commit": commit,
        "status": "BLOCKED",
        "reason": PRODUCTION_BLOCK_REASON,
        "authorization_scope": {
            "migration": False,
            "network_or_provider": False,
            "outbox_dispatch": False,
            "delivery": False,
            "release": False,
            "deployment": False,
            "cutover": False,
        },
    }
    (audit / "evidence/PRODUCTION_STATUS.json").write_bytes(
        canonical_bytes(production) + b"\n"
    )
    (audit / "review").mkdir()
    identity = summary["candidate"]
    review_status = build_review_status(identity, summary)
    (audit / "review/REVIEW_STATUS.json").write_bytes(
        canonical_bytes(review_status) + b"\n"
    )
    status_block = (
        f"Candidate commit: `{identity['commit']}`\n"
        f"Candidate tree: `{identity['tree']}`\n"
        f"Candidate parent: `{identity['parent']}`\n"
        f"Final test summary: `{summary['summary_sha256']}`\n"
        "Production: `BLOCKED`\n"
        "A2_0016-A2_0019: `NOT APPLIED`\n"
        "Formal Phase9-A: `NOT RUN`\n"
        "Run4 forensic replay: `NOT RUN`\n"
        "Phase 9: `NOT COMPLETE`\n"
        "Phase10-B: `NOT STARTED`\n"
        "Independent audit: `PENDING`\n"
    )
    for name in ("FIX_CLOSURE.md", "VALIDATION_NOTES.md", "PRO_AUDIT_PROMPT_ZH.md"):
        (audit / "review" / name).write_text(
            f"# {name}\n\n{status_block}",
            encoding="utf-8",
        )
    return audit


def test_audit_bundle_is_deterministic_single_root_and_closed(tmp_path):
    repository, contract = _repository(tmp_path)
    audit = _audit_evidence(repository, contract, tmp_path)
    first = tmp_path / "first.zip"
    second = tmp_path / "second.zip"
    first_result = _build_for_policy(
        repository, audit, first, root_name="PHASE9_TEST",
        freeze_utc="2026-09-01T12:00:00Z", source_paths=SAMPLE_SOURCE_PATHS,
        suite_specs=SAMPLE_SUITE_SPECS,
    )
    second_result = _build_for_policy(
        repository, audit, second, root_name="PHASE9_TEST",
        freeze_utc="2026-09-01T12:00:00Z", source_paths=SAMPLE_SOURCE_PATHS,
        suite_specs=SAMPLE_SUITE_SPECS,
    )
    assert first.read_bytes() == second.read_bytes()
    assert first_result["sha256"] == hashlib.sha256(first.read_bytes()).hexdigest()
    assert first_result["sha256"] == second_result["sha256"]
    assert first_result["crc_verified"] is True
    with zipfile.ZipFile(first) as archive:
        assert archive.testzip() is None
        assert {name.split("/", 1)[0] for name in archive.namelist()} == {"PHASE9_TEST"}
        assert "PHASE9_TEST/PACKAGE_MANIFEST.json" in archive.namelist()
        assert "PHASE9_TEST/checksums/SHA256SUMS" in archive.namelist()
        assert "PHASE9_TEST/identity/CANDIDATE_FILE_INVENTORY.tsv" in archive.namelist()
        assert "PHASE9_TEST/source/tracked.txt" in archive.namelist()
        assert archive.read("PHASE9_TEST/PACKAGE_README.md") == PACKAGE_README
        identity = json.loads(
            archive.read("PHASE9_TEST/identity/CANDIDATE_IDENTITY.json")
        )
        assert identity["baseline"] == AUDITED_BASELINE_IDENTITY
        extracted = tmp_path / "extracted"
        archive.extractall(extracted)
    verified = _verify_extracted_package_for_policy(
        extracted / "PHASE9_TEST", suite_specs=SAMPLE_SUITE_SPECS
    )
    assert verified["semantic_verified"] is True


def test_audit_bundle_baseline_is_exact_frozen_audited_candidate():
    assert AUDITED_BASELINE_IDENTITY == {
        "commit": "2de2f29d25970c2a3cefa4f674fc53894d782f57",
        "tree": "6f911507c16fbec1d80eca345bf2133ce50ede9d",
        "parent": "f7a2eb85a90730639166f35e4deae708d4762d00",
    }


def test_portable_bundle_preserves_preflight_failure_without_fake_events(tmp_path):
    repository, contract = _repository(tmp_path)
    audit = _audit_evidence(
        repository, contract, tmp_path, include_preflight_failure=True
    )
    archive_path = tmp_path / "preflight.zip"
    result = _build_for_policy(
        repository,
        audit,
        archive_path,
        root_name="PHASE9_TEST",
        freeze_utc="2026-09-01T12:00:00Z",
        source_paths=SAMPLE_SOURCE_PATHS,
        suite_specs=SAMPLE_SUITE_SPECS,
    )
    assert result["semantic_verified"] is True
    extracted = tmp_path / "preflight-extracted"
    with zipfile.ZipFile(archive_path) as archive:
        names = set(archive.namelist())
        assert (
            "PHASE9_TEST/command_records/source_sample_missing_python.json"
            in names
        )
        assert "PHASE9_TEST/test_logs/source_sample_missing_python.log" in names
        assert not any(
            name.endswith("source_sample_missing_python.jsonl") for name in names
        )
        archive.extractall(extracted)
    verified = _verify_extracted_package_for_policy(
        extracted / "PHASE9_TEST", suite_specs=SAMPLE_SUITE_SPECS
    )
    assert verified["semantic_verified"] is True


def test_audit_bundle_rejects_incomplete_semantic_evidence(tmp_path):
    repository, _ = _repository(tmp_path)
    audit = tmp_path / "audit"
    (audit / "evidence").mkdir(parents=True)
    with pytest.raises((FileNotFoundError, RuntimeError)):
        _build_for_policy(
            repository, audit, tmp_path / "bad.zip", root_name="PHASE9_TEST",
            freeze_utc="2026-09-01T12:00:00Z", source_paths=SAMPLE_SOURCE_PATHS,
            suite_specs=SAMPLE_SUITE_SPECS,
        )


def test_bundle_evidence_closure_requires_composite_event_artifact():
    summary = {
        "records": [
            {
                "command_record_path": "command_records/source_full.json",
                "raw_log": {"path": "test_logs/source_full.log"},
                "source_inventory": {"path": "evidence/source_inventories/source.json"},
                "dependency_inventory": {
                    "path": "evidence/dependency_inventories/source.json"
                },
                "trusted_pytest": {
                    "event_artifact": {"path": "evidence/pytest_events/source.jsonl"}
                },
                "composite_suite": {
                    "event_artifact": {
                        "path": "evidence/composite_events/source.jsonl"
                    }
                },
            }
        ],
        "failed_attempts": [],
    }

    assert "evidence/composite_events/source.jsonl" in _expected_evidence_paths(
        summary
    )


def test_bundle_evidence_closure_accepts_only_real_preflight_artifacts():
    summary = {
        "records": [],
        "failed_attempts": [
            {
                "command_record_path": "command_records/source_full_preflight.json",
                "raw_log": {"path": "test_logs/source_full_preflight.log"},
                "source_inventory": {
                    "path": "evidence/source_inventories/source_full_preflight.json"
                },
                "dependency_inventory": None,
                "trusted_pytest": None,
                "composite_suite": None,
            }
        ],
    }

    paths = _expected_evidence_paths(summary)
    assert "command_records/source_full_preflight.json" in paths
    assert "test_logs/source_full_preflight.log" in paths
    assert "evidence/source_inventories/source_full_preflight.json" in paths
    assert not any("dependency_inventories" in path for path in paths)
    assert not any("pytest_events" in path for path in paths)
    assert not any("composite_events" in path for path in paths)


def test_formal_frozen_source_set_closes_composite_runner_and_browser_tests():
    assert "tests/phase9_delivery_test_support.py" not in DEFAULT_SOURCE_PATHS
    assert {
        "factory_core/authority_operations.py",
        "factory_core/authority_operator_workflow.py",
        "factory_core/authority_production_schema.py",
        "tools/phase9_composite_evidence.py",
        "tools/run_full_repo_with_frontend_deps.py",
        "web/README.md",
        "web/frontend/package.json",
        "web/frontend/package-lock.json",
        "web/frontend/tests/phase6-controller.test.mjs",
        "web/frontend/tests/phase6-build-browser.test.mjs",
        "web/frontend/tests/phase6-panel.harness.html",
    }.issubset(DEFAULT_SOURCE_PATHS)


def test_full_repository_mapping_only_accepts_actually_collected_test_paths():
    browser_targets = {
        str(target)
        for stage in composite_stage_contract()
        if stage["id"] == "phase6_browser"
        for target in stage["targets"]
    }
    assert FULL_REPOSITORY_BROWSER_TEST_PATHS == browser_targets
    assert all(
        _full_repository_executes_test_path(path)
        for path in FULL_REPOSITORY_BROWSER_TEST_PATHS
    )
    assert _full_repository_executes_test_path("tests/test_phase9_audit_bundle.py")
    assert _full_repository_executes_test_path("tests/legacy_workflow_test.py")
    assert not _full_repository_executes_test_path("factory_core/cli.py")
    assert not _full_repository_executes_test_path("tests/conftest.py")
    assert not _full_repository_executes_test_path(
        "web/frontend/tests/unexecuted-browser.test.mjs"
    )


def test_requirement_map_rejects_omitted_composite_stage_target(tmp_path):
    audit = tmp_path / "audit"
    for relative in (
        "command_records/source.json", "command_records/fresh.json",
        "test_logs/source.log", "test_logs/fresh.log",
        "evidence/FINAL_TEST_SUMMARY.json",
    ):
        path = audit / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"evidence\n")
    records = [
        {
            "suite": "full_repository",
            "environment": environment,
            "command_record_path": f"command_records/{environment}.json",
            "raw_log": {"path": f"test_logs/{environment}.log"},
        }
        for environment in ("source", "fresh")
    ]
    browser_targets = sorted(FULL_REPOSITORY_BROWSER_TEST_PATHS)
    implementation = "tools/run_full_repo_with_frontend_deps.py"
    mapping = (
        "requirement_id\tstatus\timplementation\ttests\tcommand_records\t"
        "raw_logs\tsummary_evidence\n"
        f"P9-EVIDENCE-CLOSURE\tCLOSED_OFFLINE\t{implementation}\t"
        f"{';'.join(browser_targets)}\t"
        "command_records/source.json;command_records/fresh.json\t"
        "test_logs/source.log;test_logs/fresh.log\t"
        "evidence/FINAL_TEST_SUMMARY.json\n"
        "P9-PRODUCTION-RUN\tBLOCKED_EXTERNAL\t\t\t\t\t"
        "evidence/PRODUCTION_STATUS.json\n"
    ).encode("utf-8")
    suite_specs = {
        "full_repository": {
            "kind": "composite",
            "required_targets": [implementation],
            "required_stages": [
                "python_pytest", "frontend_production_build", "phase6_browser"
            ],
            "composite_stages": composite_stage_contract(),
            "requirements": ["P9-EVIDENCE-CLOSURE"],
        }
    }
    candidate_paths = {
        implementation,
        "web/frontend/package.json",
        *browser_targets,
    }
    with pytest.raises(
        RuntimeError,
        match="full-repository stage targets are not reachable.*package.json",
    ):
        _verify_requirement_map(
            mapping,
            candidate_paths=candidate_paths,
            frozen_paths=candidate_paths,
            audit_root=audit,
            summary={"records": records},
            suite_specs=suite_specs,
        )


@pytest.mark.parametrize("tamper", ["generic_markdown", "stale_status"])
def test_bundle_rejects_review_material_not_bound_to_verified_candidate(
    tmp_path, tamper
):
    repository, contract = _repository(tmp_path)
    audit = _audit_evidence(repository, contract, tmp_path)
    if tamper == "generic_markdown":
        (audit / "review/FIX_CLOSURE.md").write_text(
            "# Closure\n\nOffline evidence only.\n", encoding="utf-8"
        )
    else:
        path = audit / "review/REVIEW_STATUS.json"
        status = json.loads(path.read_bytes())
        status["candidate"]["commit"] = "0" * 40
        status.pop("review_status_sha256")
        status["review_status_sha256"] = canonical_sha256(status)
        path.write_bytes(canonical_bytes(status) + b"\n")

    with pytest.raises(RuntimeError, match="review"):
        _build_for_policy(
            repository, audit, tmp_path / "bad-review.zip",
            root_name="PHASE9_TEST",
            freeze_utc="2026-09-01T12:00:00Z",
            source_paths=SAMPLE_SOURCE_PATHS,
            suite_specs=SAMPLE_SUITE_SPECS,
        )


def test_bundle_requires_every_suite_target_to_be_reachable_from_requirements(
    tmp_path,
):
    repository, contract = _repository(tmp_path)
    mapping = repository / (
        "docs/operations/PHASE9_REQUIREMENT_IMPLEMENTATION_TEST_EVIDENCE_MAP.tsv"
    )
    mapping.write_text(
        mapping.read_text(encoding="utf-8").replace(
            "\ttests/test_sample.py\t", "\t\t"
        ),
        encoding="utf-8",
    )
    _git(repository, "add", "--", str(mapping.relative_to(repository)))
    _git(repository, "commit", "-qm", "remove reverse test mapping")
    audit = _audit_evidence(repository, contract, tmp_path)

    with pytest.raises(RuntimeError, match="not reachable from requirements"):
        _build_for_policy(
            repository, audit, tmp_path / "unmapped.zip",
            root_name="PHASE9_TEST",
            freeze_utc="2026-09-01T12:00:00Z",
            source_paths=SAMPLE_SOURCE_PATHS,
            suite_specs=SAMPLE_SUITE_SPECS,
        )


def test_audit_bundle_rejects_private_key_material():
    private_key_header = b"-----BEGIN " + b"PRIVATE KEY-----\n"
    with pytest.raises(RuntimeError, match="credential material"):
        _secret_scan({"evidence/bad.txt": private_key_header})


@pytest.mark.parametrize(
    ("relative", "content"),
    [
        ("test_logs/old_candidate.log", b"1 passed in 0.01s\n"),
        ("evidence/state.sqlite3", b"SQLite format 3\x00"),
        ("evidence/.env", b"TOKEN=not-a-real-secret\n"),
        ("receipts/fake.json", b"{}\n"),
        ("evidence/nested.zip", b"PK\x03\x04"),
    ],
)
def test_bundle_rejects_every_unbound_or_forbidden_artifact(
    tmp_path, relative, content
):
    repository, contract = _repository(tmp_path)
    audit = _audit_evidence(repository, contract, tmp_path)
    target = audit / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    with pytest.raises(
        RuntimeError,
        match="forbidden audit evidence|artifact inventory|reverse closure",
    ):
        _build_for_policy(
            repository,
            audit,
            tmp_path / "bad.zip",
            root_name="PHASE9_TEST",
            freeze_utc="2026-09-01T12:00:00Z",
            source_paths=SAMPLE_SOURCE_PATHS,
            suite_specs=SAMPLE_SUITE_SPECS,
        )


def test_bundle_rejects_hardlinked_evidence(tmp_path):
    repository, contract = _repository(tmp_path)
    audit = _audit_evidence(repository, contract, tmp_path)
    source = audit / "test_logs/source_sample_final.log"
    alias = audit / "test_logs/alias.log"
    alias.hardlink_to(source)
    with pytest.raises(RuntimeError, match="hardlink|one regular file"):
        _build_for_policy(
            repository,
            audit,
            tmp_path / "bad.zip",
            root_name="PHASE9_TEST",
            freeze_utc="2026-09-01T12:00:00Z",
            source_paths=SAMPLE_SOURCE_PATHS,
            suite_specs=SAMPLE_SUITE_SPECS,
        )


def test_extracted_verifier_rejects_semantic_summary_tamper(tmp_path):
    repository, contract = _repository(tmp_path)
    audit = _audit_evidence(repository, contract, tmp_path)
    archive_path = tmp_path / "candidate.zip"
    _build_for_policy(
        repository,
        audit,
        archive_path,
        root_name="PHASE9_TEST",
        freeze_utc="2026-09-01T12:00:00Z",
        source_paths=SAMPLE_SOURCE_PATHS,
        suite_specs=SAMPLE_SUITE_SPECS,
    )
    extracted = tmp_path / "tampered"
    with zipfile.ZipFile(archive_path) as archive:
        archive.extractall(extracted)
    summary_path = extracted / "PHASE9_TEST/evidence/FINAL_TEST_SUMMARY.json"
    summary = json.loads(summary_path.read_bytes())
    summary["warning_total"] = 99
    summary_path.write_bytes(canonical_bytes(summary) + b"\n")
    with pytest.raises(RuntimeError, match="manifest identity|portably reproducible"):
        _verify_extracted_package_for_policy(
            extracted / "PHASE9_TEST", suite_specs=SAMPLE_SUITE_SPECS
        )


def test_portable_verifier_rejects_rehashed_nonfrozen_inventory_row(tmp_path):
    repository, contract = _repository(tmp_path)
    audit = _audit_evidence(repository, contract, tmp_path)
    archive_path = tmp_path / "candidate.zip"
    _build_for_policy(
        repository,
        audit,
        archive_path,
        root_name="PHASE9_TEST",
        freeze_utc="2026-09-01T12:00:00Z",
        source_paths=SAMPLE_SOURCE_PATHS,
        suite_specs=SAMPLE_SUITE_SPECS,
    )
    extracted = tmp_path / "inventory-tamper"
    with zipfile.ZipFile(archive_path) as archive:
        archive.extractall(extracted)
    package = extracted / "PHASE9_TEST"
    inventory_path = package / "identity/CANDIDATE_FILE_INVENTORY.tsv"
    lines = inventory_path.read_text(encoding="utf-8").splitlines()
    row_index = next(
        index for index, line in enumerate(lines) if line.startswith("parent.txt\t")
    )
    fields = lines[row_index].split("\t")
    fields[3] = hashlib.sha1(
        b"blob 9\0tampered\n", usedforsecurity=False
    ).hexdigest()
    lines[row_index] = "\t".join(fields)
    inventory_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    identity_path = package / "identity/CANDIDATE_IDENTITY.json"
    identity = json.loads(identity_path.read_bytes())
    raw_inventory = inventory_path.read_bytes()
    identity["candidate_inventory_bytes"] = len(raw_inventory)
    identity["candidate_inventory_sha256"] = hashlib.sha256(raw_inventory).hexdigest()
    identity.pop("identity_sha256")
    identity["identity_sha256"] = canonical_sha256(identity)
    identity_path.write_bytes(canonical_bytes(identity) + b"\n")

    manifest_path = package / "PACKAGE_MANIFEST.json"
    checksum_path = package / "checksums/SHA256SUMS"
    payload = {
        path.relative_to(package).as_posix(): path.read_bytes()
        for path in package.rglob("*")
        if path.is_file()
        and path not in {manifest_path, checksum_path}
    }
    manifest_path.write_bytes(_manifest(payload))
    checksum_payload = {
        path.relative_to(package).as_posix(): path.read_bytes()
        for path in package.rglob("*")
        if path.is_file() and path != checksum_path
    }
    checksum_path.write_bytes(
        b"".join(
            f"{hashlib.sha256(raw).hexdigest()}  {relative}\n".encode("ascii")
            for relative, raw in sorted(checksum_payload.items())
        )
    )
    with pytest.raises(
        RuntimeError,
        match="reconstruct the candidate tree|execution/candidate inventory row",
    ):
        _verify_extracted_package_for_policy(
            package, suite_specs=SAMPLE_SUITE_SPECS
        )


def test_formal_bundle_api_has_no_policy_shrink_parameters(tmp_path):
    repository, contract = _repository(tmp_path)
    audit = _audit_evidence(repository, contract, tmp_path)
    with pytest.raises(TypeError):
        build(
            repository,
            audit,
            tmp_path / "never.zip",
            root_name="PHASE9_TEST",
            freeze_utc="2026-09-01T12:00:00Z",
            source_paths=SAMPLE_SOURCE_PATHS,
        )


def test_manifest_and_checksum_verifiers_reject_duplicates_and_noncanonical_data():
    payload = {"a.txt": b"a\n", "z.txt": b"z\n"}
    manifest_raw = _manifest(payload)
    _verify_manifest_payload(manifest_raw, payload)
    manifest = json.loads(manifest_raw)
    manifest["files"].append(dict(manifest["files"][0]))
    manifest["files_sha256"] = "0" * 64
    with pytest.raises(RuntimeError, match="files hash|duplicated"):
        _verify_manifest_payload(canonical_bytes(manifest) + b"\n", payload)

    checksum = b"".join(
        f"{hashlib.sha256(raw).hexdigest()}  {path}\n".encode("ascii")
        for path, raw in sorted(payload.items())
    )
    _verify_checksum_payload(checksum, payload)
    with pytest.raises(RuntimeError, match="checksum closure"):
        _verify_checksum_payload(checksum + checksum.splitlines(keepends=True)[0], payload)
