from __future__ import annotations

import inspect
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from factory_core.canonical import canonical_bytes, canonical_sha256
from factory_core.phase9_forensic_replay import _effective
from factory_core.phase9_p0_evidence import (
    PHASE9_P0_FORMAL_DOMAIN,
    PHASE9_P0_TEST_FIXTURE_DOMAIN,
    P0_REQUIREMENTS,
    Phase9P0EvidenceError,
    _require_pristine_formal_source,
    _trusted_python_runtime_identity,
    _validate_capabilities,
    _validate_junit,
    _pytest_outcomes,
    evidence_root_sha256_from_files,
    formal_p0_paths,
    phase9_p0_acceptance_spec,
    phase9_p0_execution_context_bindings,
    phase9_p0_test_nodes,
    validate_formal_phase9_p0_evidence,
    produce_formal_phase9_p0_evidence,
)


def test_p0_probe_effective_verdict_is_fail_closed() -> None:
    assert _effective(["PASS", "PASS", "PASS"]) == "PASS"
    assert _effective(["PASS", "FAIL", "PASS"]) == "FAIL"
    assert _effective(["NOT_APPLICABLE"]) == "INDETERMINATE"


def test_fixed_p0_spec_names_each_requirement_and_exact_test_node() -> None:
    spec = phase9_p0_acceptance_spec()
    assert spec["evidence_domain"] == PHASE9_P0_FORMAL_DOMAIN
    assert [item["requirement"] for item in spec["requirements"]] == list(
        P0_REQUIREMENTS
    )
    assert all(item["test_nodes"] for item in spec["requirements"])
    assert len(phase9_p0_test_nodes()) == 13
    unsigned = dict(spec)
    claimed = unsigned.pop("spec_sha256")
    assert claimed == canonical_sha256(unsigned)


def test_formal_producer_has_no_caller_selected_command_or_trust_switch() -> None:
    assert set(inspect.signature(produce_formal_phase9_p0_evidence).parameters) == {
        "source_repository",
        "evidence_root",
        "python_executable",
        "authority_database",
        "expected_source_fence_sha256",
        "workflow_id",
    }


def test_formal_producer_rejects_caller_selected_fake_python_runtime(
    tmp_path: Path,
) -> None:
    fake = tmp_path / ".venv/bin/python"
    fake.parent.mkdir(parents=True)
    fake.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake.chmod(0o755)
    with pytest.raises(
        Phase9P0EvidenceError,
        match="current trusted producer Python",
    ):
        _trusted_python_runtime_identity(fake, Path(__file__).resolve().parents[1])


def test_execution_context_binding_rejects_other_checkout_producer_bytes(
    tmp_path: Path,
) -> None:
    live = Path(__file__).resolve().parents[1]
    candidate = tmp_path / "different-candidate"
    for relative in (
        "factory_core/__init__.py",
        "factory_core/canonical.py",
        "factory_core/phase9_run_generation.py",
        "factory_core/phase9_p0_evidence.py",
        "tools/trusted_pytest_reporter.py",
        "uv.lock",
    ):
        target = candidate / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(live / relative, target)
    (candidate / "factory_core/phase9_p0_evidence.py").write_bytes(
        (candidate / "factory_core/phase9_p0_evidence.py").read_bytes()
        + b"\n# byte-different producer from another checkout\n"
    )
    subprocess.run(("git", "init", "-q"), cwd=candidate, check=True)
    subprocess.run(
        ("git", "config", "user.email", "p0@example.invalid"),
        cwd=candidate,
        check=True,
    )
    subprocess.run(
        ("git", "config", "user.name", "P0 Test"), cwd=candidate, check=True
    )
    subprocess.run(
        ("git", "commit", "--allow-empty", "-qm", "parent"),
        cwd=candidate,
        check=True,
    )
    subprocess.run(("git", "add", "-A"), cwd=candidate, check=True)
    subprocess.run(("git", "commit", "-qm", "different candidate"), cwd=candidate, check=True)
    with pytest.raises(
        Phase9P0EvidenceError,
        match="loaded formal producer differs from candidate Git bytes",
    ):
        phase9_p0_execution_context_bindings(
            source_repository=candidate,
            python_executable=Path(sys.executable),
        )


def test_formal_capability_fence_rejects_integer_lookalikes() -> None:
    with pytest.raises(Phase9P0EvidenceError, match="exact boolean"):
        _validate_capabilities(
            {
                "network_access": 0,
                "provider_call": 0,
                "outbox_dispatch": 0,
                "delivery": 0,
                "release": 0,
                "migration": 0,
                "deployment": 0,
                "cutover": 0,
            },
            "capabilities",
        )


@pytest.mark.parametrize(
    "raw",
    (
        b"PASS AR_007_DELIVERY_BYPASS\n",
        b"collected 9 items\n9 passed in 0.01s\n",
        b"collected 9 items\n",
        b"",
    ),
)
def test_pytest_semantic_parser_rejects_arbitrary_pass_or_truncation(raw: bytes) -> None:
    with pytest.raises(Phase9P0EvidenceError):
        _pytest_outcomes(raw, phase9_p0_test_nodes())


def test_pytest_semantic_parser_requires_every_exact_node_and_zero_nonpass() -> None:
    nodes = phase9_p0_test_nodes()
    lines = [f"collected {len(nodes)} items"]
    lines.extend(f"{node} PASSED [ 11%]" for node in nodes)
    lines.append(
        f"============================== {len(nodes)} passed in 0.01s =============================="
    )
    result = _pytest_outcomes(("\n".join(lines) + "\n").encode(), nodes)
    assert result["collected"] == len(nodes)
    assert result["passed"] == len(nodes)
    assert sum(result[name] for name in (
        "failed", "errors", "skipped", "xfailed", "xpassed", "warnings"
    )) == 0

    failed = ("\n".join(lines).replace(" PASSED ", " FAILED ", 1) + "\n").encode()
    with pytest.raises(Phase9P0EvidenceError, match="non-PASS"):
        _pytest_outcomes(failed, nodes)


@pytest.mark.parametrize("trailer", ("PASS", "12 passed in 0.01s"))
def test_pytest_semantic_parser_rejects_any_trailing_record(trailer: str) -> None:
    nodes = phase9_p0_test_nodes()
    lines = [f"collected {len(nodes)} items"]
    lines.extend(f"{node} PASSED [100%]" for node in nodes)
    lines.extend(
        (
            f"================ {len(nodes)} passed in 0.01s ================",
            trailer,
        )
    )
    with pytest.raises(Phase9P0EvidenceError, match="final log record"):
        _pytest_outcomes(("\n".join(lines) + "\n").encode(), nodes)


def _junit(nodes: tuple[str, ...]) -> bytes:
    import xml.etree.ElementTree as ET

    suite = ET.Element(
        "testsuite",
        tests=str(len(nodes)),
        errors="0",
        failures="0",
        skipped="0",
    )
    for node in nodes:
        parts = node.split("::")
        ET.SubElement(
            suite,
            "testcase",
            classname=".".join((parts[0][:-3].replace("/", "."), *parts[1:-1])),
            name=parts[-1],
        )
    return ET.tostring(suite)


def test_junit_validator_binds_exact_ordered_case_identities() -> None:
    nodes = phase9_p0_test_nodes()
    _validate_junit(_junit(nodes), nodes)
    wrong = list(nodes)
    wrong[0] = wrong[0].replace("test_phase9_modes", "test_wrong")
    duplicate = list(nodes)
    duplicate[1] = duplicate[0]
    for changed in (tuple(reversed(nodes)), tuple(wrong), tuple(duplicate)):
        with pytest.raises(Phase9P0EvidenceError, match="exact ordered"):
            _validate_junit(_junit(changed), nodes)


@pytest.mark.parametrize("pollution", ("conftest.py", "sitecustomize.py", "ignored.pyc"))
def test_pristine_formal_source_rejects_untracked_and_ignored_import_pollution(
    tmp_path: Path, pollution: str
) -> None:
    root = tmp_path / "source"
    root.mkdir()
    subprocess.run(("git", "init", "-q"), cwd=root, check=True)
    subprocess.run(("git", "config", "user.email", "p0@example.invalid"), cwd=root, check=True)
    subprocess.run(("git", "config", "user.name", "P0 Test"), cwd=root, check=True)
    (root / ".gitignore").write_text("*.pyc\n", encoding="utf-8")
    (root / "tracked.py").write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(("git", "add", "."), cwd=root, check=True)
    subprocess.run(("git", "commit", "-qm", "candidate"), cwd=root, check=True)
    (root / pollution).write_text("raise RuntimeError('pollution')\n", encoding="utf-8")
    with pytest.raises(Phase9P0EvidenceError, match="untracked, or ignored"):
        _require_pristine_formal_source(root)


def test_formal_validator_rejects_fixture_domain_before_any_ready_claim() -> None:
    receipts = {
        name: {
            "schema": "phase9-candidate-p0-receipt-v3",
            "evidence_domain": PHASE9_P0_TEST_FIXTURE_DOMAIN,
            "requirement": name,
        }
        for name in P0_REQUIREMENTS
    }
    files = {path: b"{}" for path in formal_p0_paths()}
    with pytest.raises(Phase9P0EvidenceError):
        validate_formal_phase9_p0_evidence(
            receipts=receipts,
            files=files,
            candidate={"commit": "a" * 40, "tree": "b" * 40, "parent": "c" * 40},
            coordinate={
                "project_id": "demo",
                "workflow_id": "workflow",
                "run_generation": "generation",
            },
            source_inventory_sha256="d" * 64,
        )


def test_root_digest_binds_every_artifact_length_and_bytes() -> None:
    files = {"a/file.json": canonical_bytes({"value": 1})}
    first = evidence_root_sha256_from_files(files)
    changed = {"a/file.json": canonical_bytes({"value": 2})}
    assert evidence_root_sha256_from_files(changed) != first
    assert evidence_root_sha256_from_files(
        {**files, "a/other.log": b"passed\n"}
    ) != first
