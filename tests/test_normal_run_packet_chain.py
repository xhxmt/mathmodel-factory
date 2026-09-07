"""Normal packet construction, declared coverage, strict quotes and aggregate."""
import json

import pytest

from scripts.aggregate_judges import aggregate_outputs
from scripts.claim_graph import build_claim_registry, evaluate_claim_coverage
from scripts.judge_packet import build_packets, _completeness
from scripts.packet_evidence import PacketEvidence
from tests.test_aggregate_judges import _hard, _paper


ROLES = ("math", "execution", "paper")
QUOTE = "The controlled evidence value is two."


def write(project, path, content):
    target = project / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    return target


def packet_project(project):
    for path, content in {
        f"{project.name}_paper.tex": "A short paper with one reported result.",
        "problem/problem_brief.md": "Estimate the controlled result.",
        "model.md": "The model uses addition.",
        "models/03_solve.py": "print(1 + 1)\n",
        "results/values.json": '{"estimate":2}',
        "solve_log.md": "Controlled computation completed.",
        "evidence/primary.md": QUOTE,
        "evidence/secondary.md": QUOTE,
    }.items():
        write(project, path, content)
    registry = dict(
        contract_version="claim-registry-v1",
        questions=[dict(id="Q1", statement="Estimate the result.",
                        source=dict(path="problem/problem_brief.md", line=1), required_roles=list(ROLES))],
        claims=[dict(id="Q1_RESULT", statement="The result has source evidence.", question_ids=["Q1"],
                     required_roles=list(ROLES), artifacts=[
                         dict(path=path, roles=list(ROLES))
                         for path in ("evidence/primary.md", "evidence/secondary.md")])],
        delivery_requirements=[],
    )
    write(project, "claim_registry.json", json.dumps(registry))


def role_outputs(project, manifests):
    paths = {}
    for role in ROLES:
        chunk = PacketEvidence(manifests[role]["files"]).resolve("evidence/secondary.md")["chunk_id"]
        path = project / "judge_outputs" / f"{role}.md"
        path.parent.mkdir(exist_ok=True)
        if role != "paper":
            _hard(path, role, evidence=[dict(ref_id=role + "-ref", claim="The registered result.",
                chunk_id=chunk, quote=QUOTE, finding="Evidence is present.", severity="support")])
        else:
            _paper(path)
            payload = json.loads(path.read_text().split("\n", 1)[1])
            for dimension in payload["dimensions"].values():
                dimension["evidence"][0].update(chunk_id=chunk, quote=QUOTE)
            path.write_text("VERDICT: PASS\n" + json.dumps(payload))
        paths[role] = path
    return paths


def aggregate(project, paths):
    return aggregate_outputs(**{role + "_path": paths[role] for role in ROLES},
        **{role + "_manifest": project / "judge_packets" / role / "manifest.json" for role in ROLES})


def test_real_packet_alias_reaches_complete_coverage_and_grounded_aggregate(tmp_path):
    packet_project(tmp_path)
    manifests = build_packets(tmp_path)
    registry = build_claim_registry(tmp_path)
    for role, manifest in manifests.items():
        files = PacketEvidence(manifest["files"])
        assert files.by_path["evidence/secondary.md"]["status"] == "alias"
        assert files.resolve("evidence/secondary.md")["path"] == "evidence/primary.md"
        assert manifest["completeness"]["eligible"]
        assert evaluate_claim_coverage(registry, role, manifest["files"])["eligible"]
    result = aggregate(tmp_path, role_outputs(tmp_path, manifests))
    assert result.status == "PASS"
    assert all(item["valid"] for item in result.evidence_grounding.values())
    assert all(item["eligible"] for item in result.packet_completeness.values())


@pytest.mark.parametrize("fault", ["missing_target", "source_mismatch", "chunk_mismatch", "truncated"])
def test_incomplete_alias_binding_cannot_reach_aggregate_pass(tmp_path, fault):
    packet_project(tmp_path)
    manifests = build_packets(tmp_path)
    paths = role_outputs(tmp_path, manifests)
    manifest = manifests["execution"]
    files = PacketEvidence(manifest["files"])
    alias = files.by_path["evidence/secondary.md"]
    canonical = files.resolve("evidence/secondary.md")
    if fault == "missing_target":
        manifest["files"].remove(canonical)
    elif fault == "source_mismatch":
        alias["sha256"] = "0" * 64
    elif fault == "chunk_mismatch":
        alias["alias_chunk_id"] = "0" * 64
    else:
        canonical["status"] = "truncated"
        canonical["reason"] = "per_file_byte_limit"
        manifest["completeness"] = _completeness(manifest["files"], manifest["completeness"]["requirements"])
        assert not evaluate_claim_coverage(build_claim_registry(tmp_path), "execution", manifest["files"])["eligible"]
    write(tmp_path, "judge_packets/execution/manifest.json", json.dumps(manifest))
    result = aggregate(tmp_path, paths)
    assert result.status == "INDETERMINATE"
    assert not result.packet_completeness["execution"]["eligible"]


def test_alias_does_not_relax_exact_quote_requirement(tmp_path):
    packet_project(tmp_path)
    manifests = build_packets(tmp_path)
    paths = role_outputs(tmp_path, manifests)
    paths["math"].write_text(paths["math"].read_text().replace(QUOTE, "An absent quote."))
    result = aggregate(tmp_path, paths)
    assert result.status == "INDETERMINATE"
    assert result.evidence_grounding["math"]["errors"][0]["code"] == "QUOTE_NOT_FOUND"


def adopted_computation(tmp_path, *, accept_claim=True):
    from tests.support.normal_run import controlled_service
    from scripts.canonical_claims import accept
    from scripts.solver_job_receipt import receipt_paths, build_evidence

    service, project, backend = controlled_service(tmp_path)
    packet_project(project)
    write(project, "data/input.json", '{"first":1,"second":1}')
    script = write(project, "models/03_solve.py", "import json, os\nfrom pathlib import Path\n"
        "from factory_core.json_values import dumps\n"
        "data = json.loads(Path('../data/input.json').read_text())\n"
        "Path('../results/values.json').write_text(dumps({'estimate': data['first'] + data['second'], "
        "'provenance': {'job_id': os.environ['FACTORY_SOLVER_JOB_ID']}}))\n")
    job = service.submit_solver(project, runtime="python", script=script,
        max_time_seconds=10, input_paths=("data/input.json",), output_paths=("results/values.json",))
    job = service.solver_status(project, job["job_id"])
    assert job["status"] == "completed", backend.result.stderr
    submitted, completed = receipt_paths(project / ".factory/solver_receipts", job["job_id"])
    assert build_evidence(project, submitted, completed)["receipt_ready"]
    if accept_claim:
        accept(project, "Q1_ESTIMATE", "results/values.json::estimate",
               completed.relative_to(project).as_posix(), [], "Use the controlled result.")
    else:
        write(project, "results/canonical_results.json", json.dumps(dict(
            project=project.name, primary_method="m1", p1=dict(source="results/values.json"))))
    # No required paths are injected into packet construction or the registry.
    return project, job, submitted, completed


@pytest.mark.parametrize("accept_claim", [True, False])
def test_native_adoption_automatically_selects_the_complete_job_chain(tmp_path, accept_claim):
    project, job, submitted, completed = adopted_computation(tmp_path, accept_claim=accept_claim)
    manifests = build_packets(project)
    manifest = manifests["execution"]
    required = {p for item in manifest["completeness"]["requirements"] for p in item["paths"]}
    assert {"data/input.json", "models/03_solve.py", "results/values.json",
            submitted.relative_to(project).as_posix(), completed.relative_to(project).as_posix(),
            job["result_refs"]["input_closure"]} <= required
    assert manifest["completeness"]["eligible"]
    assert aggregate(project, role_outputs(project, manifests)).status == "PASS"


@pytest.mark.parametrize("condition", ["missing_receipt", "missing_input", "stale_output", "budget"])
def test_automatic_job_selection_reports_missing_or_oversized_evidence(tmp_path, monkeypatch, condition):
    from scripts import judge_packet
    project, _, submitted, _ = adopted_computation(tmp_path)
    if condition == "missing_receipt":
        submitted.rename(submitted.with_suffix(".saved"))
    elif condition == "missing_input":
        (project / "data/input.json").rename(project / "data/input.saved")
    elif condition == "stale_output":
        (project / "results/values.json").write_text('{"estimate":3}')
    else:
        monkeypatch.setattr(judge_packet, "EXECUTION_CONTEXT_BYTES", 500)
    manifests = build_packets(project)
    manifest = manifests["execution"]
    assert not manifest["completeness"]["eligible"]
    chain = [item for item in manifest["completeness"]["requirements"] if item["id"].startswith("solver_chain:")]
    assert chain and not all(item["satisfied"] for item in chain)
    if condition != "budget":
        assert aggregate(project, role_outputs(project, manifests)).status == "INDETERMINATE"
