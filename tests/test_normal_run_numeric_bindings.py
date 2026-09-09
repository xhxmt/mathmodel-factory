"""Bind the whole required numeric set through normal producer receipts."""
import json

import pytest

from factory_core.finalization import build_final_input_manifest, verify_final_input_snapshot, FinalizationSnapshotChanged
from factory_core.storage import SQLiteStateStore
from scripts.canonical_claims import accept, verify
from scripts.solver_job_receipt import receipt_paths
from scripts.verify_numbers import generate_manifest, verify_paper
from tests.support.normal_run import controlled_service
from tests.test_normal_run_packet_chain import write


def numeric_project(tmp_path, *, count=2):
    service, project, backend = controlled_service(tmp_path)
    (project / "results").mkdir(exist_ok=True)
    script = write(project, "models/producer.py", "from pathlib import Path\nfrom factory_core.json_values import dumps\n"
        "Path('../results/values.json').write_text(dumps({'key_results': ["
        "{'claim_id': 'Q1', 'value': 2}, {'claim_id': 'Q2', 'value': 3}]}))\n")
    job = service.submit_solver(project, runtime="python", script=script,
        max_time_seconds=10, output_paths=("results/values.json",))
    job = service.solver_status(project, job["job_id"])
    assert job["status"] == "completed", backend.result.stderr
    _, completed = receipt_paths(project / ".factory/solver_receipts", job["job_id"])
    records = [accept(project, f"Q{i+1}", f"results/values.json::key_results[{i}].value",
                      completed.relative_to(project).as_posix(), [], "Adopt the declared controlled result.")
               for i in range(count)]
    write(project, "problem/problem_brief.md", "Report both controlled values.")
    write(project, f"{project.name}_paper.tex", "\\begin{document}\nThe values are 2 and 3.\n\\end{document}\n")
    registry = dict(contract_version="claim-registry-v1",
        questions=[dict(id="QUESTION", statement="Report both values.",
                        source=dict(path="problem/problem_brief.md", line=1), required_roles=["execution"])],
        claims=[dict(id=f"Q{i+1}", kind="numeric", statement=f"Reported quantity {i+1}.",
                     question_ids=["QUESTION"], required_roles=["execution"],
                     artifacts=[dict(path="results/values.json", field=f"key_results[{i}].value")])
                for i in range(2)], delivery_requirements=[])
    write(project, "claim_registry.json", json.dumps(registry))
    return project, registry, records


def test_all_numeric_claims_and_source_fields_pass_normal_number_verification(tmp_path):
    project, _, _ = numeric_project(tmp_path)
    assert verify(project) == []
    generate_manifest(project)
    assert verify_paper(project, project.name)
    with pytest.raises(ValueError, match="content-freeze approval"):
        build_final_input_manifest(project)
    approve_content(project)
    snapshot = build_final_input_manifest(project)
    verify_final_input_snapshot(project, snapshot)


def test_unrelated_valid_ledger_does_not_hide_missing_numeric_locator(tmp_path):
    project, registry, _ = numeric_project(tmp_path, count=1)
    registry["claims"][1]["artifacts"][0].pop("field")
    write(project, "claim_registry.json", json.dumps(registry))
    errors = verify(project)
    assert any("Q2: NUMERIC_CLAIM_LOCATOR_MISSING" in error for error in errors)
    assert any("Q2: numeric claim candidates" in error for error in errors)


def test_binding_one_field_cannot_exempt_other_results_in_same_file(tmp_path):
    project, _, _ = numeric_project(tmp_path, count=1)
    errors = verify(project)
    assert any("Q2: numeric claim candidates" in error for error in errors)
    assert "UNBOUND_DERIVED_KEY_RESULT: results/values.json::key_results[1].value" in errors
    generate_manifest(project)
    assert not verify_paper(project, project.name)


@pytest.mark.parametrize("fault", ["claim_id", "source_field", "version", "missing_version"])
def test_each_derived_result_requires_consistent_claim_source_and_version(tmp_path, fault):
    project, _, records = numeric_project(tmp_path)
    item = dict(claim_id="Q1", canonical_source=records[0]["accepted"]["locator"],
                canonical_version=records[0]["version"], value=2)
    write(project, "results/summary.json", json.dumps(dict(key_results=[item])))
    assert verify(project) == []
    if fault == "claim_id":
        item["claim_id"] = "unknown"
    elif fault == "source_field":
        item["canonical_source"] = records[1]["accepted"]["locator"]
    elif fault == "version":
        item["canonical_version"] = records[1]["version"]
    else:
        item.pop("canonical_version")
    write(project, "results/summary.json", json.dumps(dict(key_results=[item])))
    assert "UNBOUND_DERIVED_KEY_RESULT: results/summary.json::key_results[0].value" in verify(project)


def test_new_upstream_source_invalidates_number_gate_and_final_snapshot(tmp_path):
    project, _, _ = numeric_project(tmp_path)
    assert verify(project) == []
    generate_manifest(project)
    approve_content(project)
    snapshot = build_final_input_manifest(project)
    values = json.loads((project / "results/values.json").read_text())
    values["key_results"][0]["value"] = 4
    write(project, "results/values.json", json.dumps(values))
    assert any("accepted upstream source changed" in error for error in verify(project))
    assert not verify_paper(project, project.name)
    with pytest.raises(FinalizationSnapshotChanged):
        verify_final_input_snapshot(project, snapshot)


def test_non_scalar_numeric_locator_is_reported_for_its_claim(tmp_path):
    project, registry, _ = numeric_project(tmp_path)
    registry["claims"][1]["artifacts"][0]["field"] = "key_results"
    write(project, "claim_registry.json", json.dumps(registry))
    assert any("Q2: NUMERIC_CLAIM_LOCATOR_INVALID" in error for error in verify(project))


def approve_content(project):
    store = SQLiteStateStore(project)
    store.record_decision("content_freeze", dict(selected_option_id="approve_content_freeze",
        approved=True, selected_at=store.now_epoch()))
