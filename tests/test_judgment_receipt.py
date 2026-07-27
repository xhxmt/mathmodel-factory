import hashlib
import json
from pathlib import Path

from scripts.judgment_receipt import (
    RECEIPT_CONTENT_HASH_FIELD,
    ROLE_METADATA_SCHEMA,
    annotate_role_metadata,
    bind_configuration_group,
    build_receipt,
    verify_receipt,
)
from scripts.judge_decision_router import route_decision


def _write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def _rehash_receipt(value: dict) -> None:
    unsigned = dict(value)
    unsigned.pop(RECEIPT_CONTENT_HASH_FIELD, None)
    value[RECEIPT_CONTENT_HASH_FIELD] = hashlib.sha256(
        json.dumps(
            unsigned,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "demo"
    objective = {
            "schema_version": "objective-evidence-v1",
            "project_id": "demo",
            "input_fingerprint": "a" * 64,
            "findings": [],
            "summary": {"decision": "INDETERMINATE"},
            "metadata": {},
            "decision_semantics": "EVIDENCE_COLLECTION_ONLY",
            "quality_verdict": "UNAVAILABLE",
    }
    objective["bundle_sha256"] = hashlib.sha256(
        json.dumps(objective, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    objective_path = project / "judge_packets/objective_evidence.json"
    _write(objective_path, json.dumps(objective) + "\n")
    objective_record = {
        "path": "judge_packets/objective_evidence.json",
        "bytes": objective_path.stat().st_size,
        "sha256": hashlib.sha256(objective_path.read_bytes()).hexdigest(),
        "schema_version": "objective-evidence-v1",
        "bundle_sha256": objective["bundle_sha256"],
        "input_fingerprint": objective["input_fingerprint"],
        "summary": objective["summary"],
    }
    configuration_fingerprint = "c" * 64
    for role in ("math", "execution", "paper"):
        context_path = project / f"judge_packets/{role}/context.txt"
        _write(context_path, f"context {role}\n")
        manifest = {
            "version": 3,
            "role": role,
            "project": "demo",
            "context": {
                "sha256": hashlib.sha256(context_path.read_bytes()).hexdigest(),
                "size": context_path.stat().st_size,
            },
            "objective_evidence": objective_record,
        }
        manifest["packet_fingerprint"] = hashlib.sha256(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        _write(project / f"judge_packets/{role}/manifest.json", json.dumps(manifest))
        _write(project / f"judge_outputs/{role}.md", f"VERDICT: PASS\n{{\"role\":\"{role}\"}}\n")
        prompt_path = project / f"judge_outputs/{role}.rendered_prompt.txt"
        _write(prompt_path, f"rendered prompt for {role}\n")
        _write(
            project / f"judge_outputs/{role}.md.llm-result.json",
            json.dumps({"configuration_fingerprint": configuration_fingerprint}),
        )
        annotate_role_metadata(
            project,
            role,
            registry_model_id=f"judge-{role}",
            backend="openai",
            model="model-v1",
            transport="api_agent_run",
            prompt_file=prompt_path,
        )
    bind_configuration_group(project)
    aggregate_roles = [
        {"role": role, "status": "PASS", "verdict": "PASS"}
        for role in ("math", "execution", "paper")
    ]
    aggregate = {
        "schema_version": "judge-aggregate-v3",
        "verdict": "PASS",
        "status": "PASS",
        "score_available": True,
        "overall_score": 80,
        "roles": aggregate_roles,
        "role_statuses": {role: "PASS" for role in ("math", "execution", "paper")},
        "vetoes": [],
        "indeterminate_roles": [],
        "packet_completeness": {
            role: {
                "enforced": True,
                "eligible": True,
                "unmet_requirements": [],
                "error": None,
            }
            for role in ("math", "execution", "paper")
        },
    }
    _write(
        project / "judge_outputs/aggregate.json",
        json.dumps(aggregate) + "\n",
    )
    _write(project / "judge_evaluation.md", "VERDICT: PASS\n")
    visual = {"schema": "pdf-visual-gate-v1", "status": "PASS", "findings": []}
    _write(project / "judge_outputs/visual_gate.json", json.dumps(visual) + "\n")
    _write(
        project / "judge_outputs/decision_route.json",
        json.dumps(route_decision(aggregate, visual, policy_mode="shadow")),
    )
    return project


def test_annotate_role_metadata_binds_actual_response_and_packet(tmp_path):
    project = tmp_path / "demo"
    _write(project / "judge_packets/math/context.txt", "context\n")
    _write(project / "judge_packets/math/manifest.json", "{}\n")
    _write(project / "judge_outputs/math.md", "VERDICT: PASS\n{}\n")
    prompt = project / "judge_outputs/math.rendered_prompt.txt"
    _write(prompt, "rendered prompt\n")
    _write(
        project / "judge_outputs/math.md.llm-result.json",
        '{"configuration_fingerprint":"' + "c" * 64 + '"}\n',
    )

    metadata = annotate_role_metadata(
        project,
        "math",
        registry_model_id="judge-a",
        backend="deepseek",
        model="deepseek-chat",
        transport="api_agent_run",
        prompt_file=prompt,
    )

    assert metadata["receipt_schema"] == ROLE_METADATA_SCHEMA
    assert metadata["registry_model_id"] == "judge-a"
    assert len(metadata["configuration_fingerprint"]) == 64
    assert len(metadata["response_sha256"]) == 64


def test_receipt_verification_detects_replaced_role_output(tmp_path):
    project = _project(tmp_path)
    fingerprint = "a" * 64

    receipt = build_receipt(project, "demo", input_fingerprint=fingerprint)
    valid, errors = verify_receipt(
        project, "demo", expected_input_fingerprint=fingerprint
    )

    assert receipt["status"] == "VALID"
    assert valid is True
    assert errors == []

    _write(project / "judge_outputs/paper.md", "VERDICT: REVISE\n{}\n")
    valid, errors = verify_receipt(
        project, "demo", expected_input_fingerprint=fingerprint
    )
    assert valid is False
    assert any("artifact changed" in error for error in errors)


def test_receipt_verification_detects_replaced_rendered_prompt(tmp_path):
    project = _project(tmp_path)
    fingerprint = "a" * 64
    receipt = build_receipt(project, "demo", input_fingerprint=fingerprint)
    assert receipt["status"] == "VALID"

    _write(project / "judge_outputs/math.rendered_prompt.txt", "changed prompt\n")
    valid, errors = verify_receipt(
        project, "demo", expected_input_fingerprint=fingerprint
    )
    assert valid is False
    assert any("artifact changed" in error for error in errors)


def test_receipt_requires_all_actual_call_metadata(tmp_path):
    project = _project(tmp_path)
    (project / "judge_outputs/execution.md.llm-result.json").unlink()

    receipt = build_receipt(project, "demo", input_fingerprint="b" * 64)

    assert receipt["status"] == "INVALID"
    assert any("required file is missing" in error for error in receipt["errors"])


def test_receipt_tampering_cannot_drop_roles_even_if_hash_is_recomputed(tmp_path):
    project = _project(tmp_path)
    build_receipt(project, "demo", input_fingerprint="a" * 64)
    path = project / "judge_outputs/judgment_receipt.json"
    receipt = json.loads(path.read_text(encoding="utf-8"))
    receipt["roles"] = []
    receipt["status"] = "VALID"
    receipt["errors"] = []
    _rehash_receipt(receipt)
    _write(path, json.dumps(receipt))

    valid, errors = verify_receipt(project, "demo", expected_input_fingerprint="a" * 64)

    assert valid is False
    assert any("exactly three" in error for error in errors)


def test_receipt_rejects_cross_role_configuration_mismatch(tmp_path):
    project = _project(tmp_path)
    metadata_path = project / "judge_outputs/execution.md.llm-result.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["configuration_fingerprint"] = "d" * 64
    _write(metadata_path, json.dumps(metadata))

    receipt = build_receipt(project, "demo", input_fingerprint="a" * 64)

    assert receipt["status"] == "INVALID"
    assert any("configuration fingerprint" in error for error in receipt["errors"])


def test_receipt_turns_malformed_utf8_metadata_into_invalid_receipt(tmp_path):
    project = _project(tmp_path)
    (project / "judge_outputs/math.md.llm-result.json").write_bytes(b"\xff\xfe")

    receipt = build_receipt(project, "demo", input_fingerprint="a" * 64)

    assert receipt["status"] == "INVALID"
    assert any("invalid JSON file" in error for error in receipt["errors"])


def test_receipt_rejects_manifest_objective_evidence_mismatch(tmp_path):
    project = _project(tmp_path)
    manifest_path = project / "judge_packets/execution/manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["objective_evidence"]["sha256"] = "d" * 64
    unsigned = dict(manifest)
    unsigned.pop("packet_fingerprint")
    manifest["packet_fingerprint"] = hashlib.sha256(
        json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    _write(manifest_path, json.dumps(manifest))

    receipt = build_receipt(project, "demo", input_fingerprint="a" * 64)

    assert receipt["status"] == "INVALID"
    assert any("objective evidence mismatch" in error for error in receipt["errors"])


def test_receipt_rejects_aggregate_role_status_disagreement(tmp_path):
    project = _project(tmp_path)
    aggregate_path = project / "judge_outputs/aggregate.json"
    aggregate = json.loads(aggregate_path.read_text(encoding="utf-8"))
    aggregate["role_statuses"]["math"] = "FAIL"
    _write(aggregate_path, json.dumps(aggregate))

    receipt = build_receipt(project, "demo", input_fingerprint="a" * 64)

    assert receipt["status"] == "INVALID"
    assert any("role_statuses disagrees" in error for error in receipt["errors"])


def test_receipt_rejects_forged_route_even_when_labels_look_valid(tmp_path):
    project = _project(tmp_path)
    route_path = project / "judge_outputs/decision_route.json"
    route = json.loads(route_path.read_text(encoding="utf-8"))
    route["new_decision"] = "REOPEN_REVISION_TEXT"
    route["decision_changed"] = True
    _write(route_path, json.dumps(route))

    receipt = build_receipt(project, "demo", input_fingerprint="a" * 64)

    assert receipt["status"] == "INVALID"
    assert any("does not match recomputation" in error for error in receipt["errors"])


def test_receipt_verifier_requires_exact_derived_artifact_set(tmp_path):
    project = _project(tmp_path)
    build_receipt(project, "demo", input_fingerprint="a" * 64)
    path = project / "judge_outputs/judgment_receipt.json"
    receipt = json.loads(path.read_text(encoding="utf-8"))
    del receipt["derived_artifacts"]["report"]
    _rehash_receipt(receipt)
    _write(path, json.dumps(receipt))

    valid, errors = verify_receipt(project, "demo", expected_input_fingerprint="a" * 64)

    assert valid is False
    assert "judgment receipt derived_artifacts is invalid" in errors
