from __future__ import annotations

from dataclasses import fields, replace
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from factory_core.contract_pins import (
    ContractPinValidationError,
    compile_contract_pin_analysis,
    compile_contract_pin_set,
    compile_runtime_contract_pin,
    contract_pin_analysis_bytes,
    contract_pin_set_sha256,
    runtime_contract_sha256,
    validate_contract_pin_set,
)
from factory_core.workflow_contract import (
    compile_workflow_contract_bundle,
    workflow_contract_analysis_sha256,
    workflow_contract_sha256,
)
from factory_core.workflow_contract_v2 import (
    WorkflowContractV2ValidationError,
    compile_workflow_contract_bundle_v2,
    validate_workflow_contract_bundle_v2,
    workflow_contract_v2_analysis_sha256,
    workflow_contract_v2_sha256,
)
from tests.support.m03_source_manifest import contract_compiler_implementation_sha256


ROOT = Path(__file__).resolve().parents[1]


def test_frozen_m02_workflow_identities_remain_exact() -> None:
    v1 = compile_workflow_contract_bundle()
    assert workflow_contract_sha256(v1) == (
        "2e2f3b9cb48788db5e7a28d0f0343518c3d1cd5969a57e045ef2fc946df5455d"
    )
    assert workflow_contract_analysis_sha256(v1) == (
        "a9d7aa2a134075ba53b9f2c09b94c8182d209a683639609a27caa4c1e535c92b"
    )


def test_workflow_v2_validates_v1_before_additive_identities() -> None:
    bundle = compile_workflow_contract_bundle_v2()
    forged_v1 = replace(bundle.v1_bundle, runtime_generation="forged")
    forged = replace(bundle, v1_bundle=forged_v1)
    with pytest.raises(WorkflowContractV2ValidationError):
        validate_workflow_contract_bundle_v2(forged)


def test_workflow_v2_analysis_refs_do_not_change_semantic_identity() -> None:
    first = compile_workflow_contract_bundle_v2(analysis_evidence_refs=("evidence:a",))
    second = compile_workflow_contract_bundle_v2(analysis_evidence_refs=("evidence:b",))
    assert workflow_contract_v2_sha256(first) == workflow_contract_v2_sha256(second)
    assert workflow_contract_v2_analysis_sha256(first) != (
        workflow_contract_v2_analysis_sha256(second)
    )


@pytest.mark.parametrize(
    "field_name",
    [
        item.name
        for item in fields(type(compile_contract_pin_set(compile_workflow_contract_bundle_v2())))
        if item.name != "schema_version"
    ],
)
def test_each_contract_pin_is_source_authorized(field_name: str) -> None:
    workflow = compile_workflow_contract_bundle_v2()
    pins = compile_contract_pin_set(workflow)
    forged = replace(pins, **{field_name: "f" * 64})
    with pytest.raises(ContractPinValidationError, match=field_name):
        validate_contract_pin_set(forged, workflow)


def test_runtime_pin_is_separate_from_repository_source_manifest() -> None:
    runtime = compile_runtime_contract_pin()
    assert runtime.python_implementation
    assert runtime.python_version
    assert runtime_contract_sha256(runtime) == runtime_contract_sha256()
    assert "factory_core" not in repr(runtime)


def test_contract_compiler_identity_is_build_only_analysis_input() -> None:
    workflow = compile_workflow_contract_bundle_v2()
    pins = compile_contract_pin_set(workflow)
    compiler_sha = contract_compiler_implementation_sha256(ROOT)
    first = compile_contract_pin_analysis(
        pin_set=pins,
        workflow=workflow,
        workflow_contract_analysis_sha256=workflow_contract_v2_analysis_sha256(workflow),
        contract_compiler_implementation_sha256=compiler_sha,
        evidence_refs=("evidence:a",),
    )
    second = replace(first, evidence_refs=("evidence:b",))
    assert contract_pin_set_sha256(first.pin_set, workflow) == (
        contract_pin_set_sha256(second.pin_set, workflow)
    )
    assert contract_pin_analysis_bytes(first, workflow) != (
        contract_pin_analysis_bytes(second, workflow)
    )
    assert "factory_core/engine.py" not in first.source_locators


def test_m03_identity_goldens() -> None:
    workflow = compile_workflow_contract_bundle_v2()
    pins = compile_contract_pin_set(workflow)
    assert workflow_contract_v2_sha256(workflow) == (
        "5e1d6af9793ec9654d45a5717a6db743b446a9527aaddff4f937cf7f11e1a856"
    )
    assert workflow_contract_v2_analysis_sha256(workflow) == (
        "19a3d2c06bfa4e2663adeae5621f22ed0adeee35c512fc4c54321e8b17001a85"
    )
    assert contract_pin_set_sha256(pins, workflow) == (
        "2bf00be163178ee9bb7e2d7e348e7add8b11e992e5cb1299778d5a56940d4502"
    )


def test_identity_replay_is_byte_identical_across_five_hash_seeds_and_matches_goldens() -> None:
    script = ROOT / "tests/support/m03_identity_replay.py"
    outputs = []
    for seed in ("0", "1", "17", "999", "random"):
        environment = dict(os.environ)
        environment["PYTHONHASHSEED"] = seed
        completed = subprocess.run(
            [sys.executable, str(script)],
            cwd=ROOT,
            env=environment,
            check=True,
            capture_output=True,
        )
        assert completed.stderr == b""
        outputs.append(completed.stdout)
    assert all(value == outputs[0] for value in outputs)
    replay = json.loads(outputs[0])
    classifier = json.loads(
        (ROOT / "tests/fixtures/m03_classifier_identity/golden_identity.json").read_text()
    )
    policy = json.loads(
        (ROOT / "tests/fixtures/m03_persisted_dirty_owner_policy/golden_identity.json").read_text()
    )
    pins = json.loads(
        (ROOT / "tests/fixtures/m03_contract_pins/golden_identity.json").read_text()
    )
    snapshot = json.loads(
        (ROOT / "tests/fixtures/m03_snapshot_v0/golden_identity.json").read_text()
    )
    command = json.loads(
        (ROOT / "tests/fixtures/m03_command_envelope/golden_identity.json").read_text()
    )
    assert replay["classifier"] == classifier
    assert replay["persisted_dirty_owner"] == policy
    assert replay["contract_compiler_implementation_sha256"] == pins[
        "contract_compiler_implementation_sha256"
    ]
    assert replay["workflow_v2"] == {
        "analysis_sha256": pins["workflow_v2_analysis_sha256"],
        "semantic_sha256": pins["workflow_v2_semantic_sha256"],
    }
    assert replay["contract_pins"] == {
        "analysis_sha256": pins["contract_pins_analysis_sha256"],
        "semantic_sha256": pins["contract_pins_semantic_sha256"],
    }
    assert replay["snapshot"] == snapshot
    for key, expected in command.items():
        assert replay[key] == expected
    assert replay["accepted_for_shadow_validation"] is True
    assert replay["authoritative"] is False
    assert replay["proposed_mutations"] == []
    assert replay["performed_side_effects"] == []
