from __future__ import annotations

import importlib.util
import hashlib
import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from factory_core.submission_bundle import (
    submission_bundle_manifest,
    verify_zip_against_manifest,
)
from scripts.submission_fingerprint import submission_fingerprint_payload
from tests.phase9_delivery_test_support import nonformal_delivery_fence


REPO_ROOT = Path(__file__).resolve().parents[1]


def load_module():
    spec = importlib.util.spec_from_file_location(
        "package_submission", REPO_ROOT / "scripts" / "package_submission.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_bundle_excludes_archived_paper_and_gate_evidence(tmp_path):
    module = load_module()
    base = "demo"

    (tmp_path / f"{base}_paper.pdf").write_bytes(b"final pdf")
    (tmp_path / f"{base}_paper.tex").write_text(
        "\\begin{document}final\\end{document}\n", encoding="utf-8"
    )
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "solve.py").write_text("print('ok')\n", encoding="utf-8")
    (tmp_path / "results").mkdir()
    (tmp_path / "results" / "values.json").write_text("{}\n", encoding="utf-8")

    archive = tmp_path / "paper" / "archive" / "gate2_indeterminate"
    archive.mkdir(parents=True)
    (archive / f"{base}_paper.pdf").write_bytes(b"stale pdf")
    (archive / "judge_evaluation.md").write_text("VERDICT: REVISE\n", encoding="utf-8")

    selected = {arcname for _path, arcname in module.iter_bundle_files(tmp_path, base)}

    assert f"{base}_paper.pdf" in selected
    assert "models/solve.py" in selected
    assert "results/values.json" in selected
    assert not any(name.startswith("paper/archive/") for name in selected)


def test_bundle_includes_declared_top_level_attachment(tmp_path):
    module = load_module()
    base = "demo"
    (tmp_path / f"{base}_paper.pdf").write_bytes(b"final pdf")
    (tmp_path / f"{base}_paper.tex").write_text(
        "\\begin{document}final\\end{document}\n", encoding="utf-8"
    )
    (tmp_path / "models").mkdir()
    (tmp_path / "models/solve.py").write_text("pass\n", encoding="utf-8")
    (tmp_path / "results").mkdir()
    (tmp_path / "results/values.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "problem").mkdir()
    (tmp_path / "problem/deliverables.json").write_text(
        '{"attachments":[{"file":"result.csv"}]}\n', encoding="utf-8"
    )
    (tmp_path / "result.csv").write_text("value\n1\n", encoding="utf-8")

    selected = {name for _path, name in module.iter_bundle_files(tmp_path, base)}

    assert "result.csv" in selected


def _complete_bundle_project(project: Path, base: str = "demo") -> None:
    paper = project / "paper/paper.tex"
    section = project / "paper/sections/main.tex"
    bibliography = project / "paper/refs/library.bib"
    section.parent.mkdir(parents=True)
    bibliography.parent.mkdir(parents=True)
    paper.write_text(
        "\\documentclass{article}\n"
        "\\addbibresource{refs/library.bib}\n"
        "\\begin{document}\\input{sections/main}\\end{document}\n",
        encoding="utf-8",
    )
    section.write_text("approved content\n", encoding="utf-8")
    bibliography.write_text("@article{x,title={X}}\n", encoding="utf-8")
    (project / "paper/draft.tex").write_text("unapproved draft\n", encoding="utf-8")
    (project / f"{base}_paper.pdf").write_bytes(b"final pdf")
    (project / "models").mkdir()
    (project / "models/solve.py").write_text("pass\n", encoding="utf-8")
    (project / "results").mkdir()
    (project / "results/values.json").write_text("{}\n", encoding="utf-8")


def test_unreferenced_paper_draft_is_not_packaged(tmp_path):
    _complete_bundle_project(tmp_path)

    manifest = submission_bundle_manifest(tmp_path, "demo")
    names = {item["archive_path"] for item in manifest["members"]}

    assert "paper/paper.tex" in names
    assert "paper/sections/main.tex" in names
    assert "paper/refs/library.bib" in names
    assert "paper/draft.tex" not in names


def test_shared_registry_drives_submission_members_and_owner_metadata(tmp_path):
    _complete_bundle_project(tmp_path)
    (tmp_path / "entry_gate.md").write_text("VERDICT: PASS\n", encoding="utf-8")
    final_data = tmp_path / "data/final/adopted.csv"
    final_data.parent.mkdir(parents=True)
    final_data.write_text("value\n1\n", encoding="utf-8")
    (tmp_path / "judge_evaluation.md").write_text(
        "VERDICT: PASS\n", encoding="utf-8"
    )

    manifest = submission_bundle_manifest(tmp_path, "demo")
    members = {item["archive_path"]: item for item in manifest["members"]}

    assert manifest["schema_version"] == "submission-bundle-manifest-v2"
    assert manifest["artifact_ownership_schema"] == "factory-artifact-ownership-v1"
    assert members["entry_gate.md"]["owner_stage"] == 6
    assert members["data/final/adopted.csv"]["owner_stage"] == 4
    assert members["results/values.json"]["owner_stage"] == 4
    assert "judge_evaluation.md" not in members


def test_package_manifest_matches_final_submission_fingerprint(tmp_path):
    _complete_bundle_project(tmp_path)

    manifest = submission_bundle_manifest(tmp_path, "demo")
    payload = submission_fingerprint_payload(tmp_path, "demo")

    assert payload["submission_bundle"] == manifest


def test_submission_package_rejects_paper_symlink(tmp_path):
    _complete_bundle_project(tmp_path)
    outside = tmp_path.parent / "outside-paper.tex"
    outside.write_text("outside\n", encoding="utf-8")
    (tmp_path / "paper/paper.tex").unlink()
    (tmp_path / "paper/paper.tex").symlink_to(outside)

    with pytest.raises(ValueError, match="LaTeX|root|symlink"):
        submission_bundle_manifest(tmp_path, "demo")


def test_submission_package_rejects_owned_directory_symlink(tmp_path):
    _complete_bundle_project(tmp_path)
    outside = tmp_path.parent / "outside-models"
    outside.mkdir()
    (outside / "secret.py").write_text("secret = True\n", encoding="utf-8")
    (tmp_path / "models/external").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink"):
        submission_bundle_manifest(tmp_path, "demo")


def test_zip_members_exactly_match_bundle_manifest(tmp_path, monkeypatch):
    _complete_bundle_project(tmp_path)
    output = tmp_path.parent / "submission.zip"
    module = load_module()
    monkeypatch.setattr(
        module,
        "require_phase9_delivery_authority",
        nonformal_delivery_fence,
    )
    manifest = module.package_submission(
        tmp_path,
        "demo",
        output,
        workflow_id="test-fixture:workflow",
        run_generation="test-fixture:generation",
    )

    on_disk = json.loads(
        (tmp_path / ".factory/finalization/submission_bundle_manifest.json")
        .read_text(encoding="utf-8")
    )
    assert on_disk == manifest
    verify_zip_against_manifest(output, on_disk)
    with zipfile.ZipFile(output) as archive:
        assert set(archive.namelist()) == {
            item["archive_path"] for item in on_disk["members"]
        }


def test_release_zip_is_reproducible_for_identical_manifest(tmp_path, monkeypatch):
    _complete_bundle_project(tmp_path)
    first = tmp_path.parent / "first.zip"
    second = tmp_path.parent / "second.zip"
    module = load_module()
    monkeypatch.setattr(
        module,
        "require_phase9_delivery_authority",
        nonformal_delivery_fence,
    )

    module.package_submission(
        tmp_path,
        "demo",
        first,
        workflow_id="test-fixture:workflow",
        run_generation="test-fixture:generation",
    )
    for path in tmp_path.rglob("*"):
        if path.is_file():
            path.touch()
    module.package_submission(
        tmp_path,
        "demo",
        second,
        workflow_id="test-fixture:workflow",
        run_generation="test-fixture:generation",
    )

    assert hashlib.sha256(first.read_bytes()).digest() == hashlib.sha256(
        second.read_bytes()
    ).digest()


def test_standalone_submission_requires_explicit_authority_coordinate(tmp_path):
    _complete_bundle_project(tmp_path)
    output = tmp_path.parent / "missing-coordinate.zip"

    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts/package_submission.py"),
            str(tmp_path),
            "demo",
            str(output),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert not output.exists()
    assert not (
        tmp_path / ".factory/finalization/submission_bundle_manifest.json"
    ).exists()


def test_standalone_submission_missing_authority_has_zero_side_effects(tmp_path):
    _complete_bundle_project(tmp_path)
    output = tmp_path.parent / "never-created" / "disabled.zip"

    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts/package_submission.py"),
            str(tmp_path),
            "demo",
            str(output),
            "--workflow-id",
            "wf-phase9",
            "--run-generation",
            "generation-phase9",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "Authority" in result.stderr or "database" in result.stderr
    assert not output.exists()
    assert not output.parent.exists()
    assert not (
        tmp_path / ".factory/finalization/submission_bundle_manifest.json"
    ).exists()


def _write_solver_submission_receipt_for_coverage(
    project, input_path, *, job_id="coverage-job", requested_at=1
):
    import json
    from scripts.solver_job_receipt import (
        build_submission_receipt,
        receipt_paths,
        write_receipt,
    )

    script = project / "models" / "solve.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("print('ok')\n", encoding="utf-8")
    output = project / "results" / "answer.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    receipt = build_submission_receipt(
        project_dir=project,
        job_id=job_id,
        backend="local",
        runtime="python",
        script=script,
        workdir=script.parent,
        argv=(),
        max_time_seconds=30,
        requested_at=requested_at,
        input_paths=(input_path,),
        output_paths=(output,),
        seeds=(7,),
    )
    submitted, _completed = receipt_paths(
        project / ".factory" / "solver_receipts", job_id
    )
    write_receipt(submitted, receipt)
    return receipt


def _write_minimal_active_paper(project):
    paper = project / f"{project.name}_paper.tex"
    paper.write_text(
        "\\documentclass{article}\n\\begin{document}ok\\end{document}\n",
        encoding="utf-8",
    )
    return paper


@pytest.mark.parametrize(
    "relative",
    [
        "replication/private_seed.npy",
        "custom/coefficients.npz",
        "config/model.mat",
        "config/model.yaml",
        "config/model.toml",
        "solver_input.py",
    ],
)
def test_solver_declared_unowned_input_blocks_submission_bundle(
    tmp_path, relative
):
    from factory_core.submission_bundle import submission_bundle_paths

    _write_minimal_active_paper(tmp_path)
    input_path = tmp_path / relative
    input_path.parent.mkdir(parents=True, exist_ok=True)
    input_path.write_bytes(b"solver-input-fixture")
    _write_solver_submission_receipt_for_coverage(tmp_path, input_path)

    with pytest.raises(
        ValueError, match="solver-declared input lacks ownership.*" + input_path.name
    ):
        submission_bundle_paths(tmp_path, tmp_path.name, require_pdf=False)


def test_solver_declared_intermediate_is_routed_without_including_scratch(tmp_path):
    from factory_core.finalization import build_final_input_manifest
    from factory_core.submission_bundle import submission_bundle_paths

    _write_minimal_active_paper(tmp_path)
    input_path = tmp_path / "data" / "intermediate" / "calibration.parquet"
    input_path.parent.mkdir(parents=True)
    input_path.write_bytes(b"PAR1solver-input")
    scratch_path = input_path.parent / "scratch.cache"
    scratch_path.write_bytes(b"not-declared")
    _write_solver_submission_receipt_for_coverage(tmp_path, input_path)

    bundle_paths = submission_bundle_paths(tmp_path, tmp_path.name, require_pdf=False)
    assert input_path.resolve() in bundle_paths
    assert scratch_path.resolve() not in bundle_paths
    snapshot = build_final_input_manifest(tmp_path)
    final_paths = {item["path"] for item in snapshot.manifest["files"]}
    assert "data/intermediate/calibration.parquet" in final_paths
    assert "data/intermediate/scratch.cache" not in final_paths


def test_solver_declared_owned_input_is_in_submission_and_final_identity(tmp_path):
    from factory_core.finalization import build_final_input_manifest
    from factory_core.submission_bundle import submission_bundle_paths

    _write_minimal_active_paper(tmp_path)
    input_path = tmp_path / "data" / "raw" / "calibration.parquet"
    input_path.parent.mkdir(parents=True)
    input_path.write_bytes(b"PAR1fixture")
    _write_solver_submission_receipt_for_coverage(tmp_path, input_path)

    bundle_paths = submission_bundle_paths(tmp_path, tmp_path.name, require_pdf=False)
    assert input_path.resolve() in bundle_paths
    snapshot = build_final_input_manifest(tmp_path)
    final_paths = {item["path"] for item in snapshot.manifest["files"]}
    assert "data/raw/calibration.parquet" in final_paths
    assert any(path.endswith("coverage-job.submitted.json") for path in final_paths)


def test_solver_declared_input_exclusion_receipt_is_bound_to_final_identity(tmp_path):
    from factory_core.finalization import build_final_input_manifest
    from factory_core.solver_input_coverage import (
        build_solver_input_exclusion_receipt,
        write_solver_input_exclusion_receipt,
    )
    from factory_core.submission_bundle import submission_bundle_paths

    _write_minimal_active_paper(tmp_path)
    input_path = tmp_path / "replication" / "private_seed.npy"
    input_path.parent.mkdir(parents=True)
    input_path.write_bytes(b"NUMPYfixture")
    submitted = _write_solver_submission_receipt_for_coverage(tmp_path, input_path)
    record = submitted["inputs"][0]
    exclusion = build_solver_input_exclusion_receipt(
        relative_path=record["path"],
        input_sha256=record["sha256"],
        reason="licensed input cannot be redistributed; receipt preserves exact identity",
    )
    exclusion_path = write_solver_input_exclusion_receipt(tmp_path, exclusion)

    bundle_paths = submission_bundle_paths(tmp_path, tmp_path.name, require_pdf=False)
    assert input_path.resolve() not in bundle_paths
    snapshot = build_final_input_manifest(tmp_path)
    final_paths = {item["path"] for item in snapshot.manifest["files"]}
    assert exclusion_path.relative_to(tmp_path).as_posix() in final_paths
    assert "replication/private_seed.npy" not in final_paths


def test_solver_submission_receipt_input_drift_blocks_finalization(tmp_path):
    import pytest
    from factory_core.finalization import build_final_input_manifest

    _write_minimal_active_paper(tmp_path)
    input_path = tmp_path / "data" / "raw" / "config.yaml"
    input_path.parent.mkdir(parents=True)
    input_path.write_text("alpha: 1\n", encoding="utf-8")
    _write_solver_submission_receipt_for_coverage(tmp_path, input_path)
    input_path.write_text("alpha: 2\n", encoding="utf-8")

    with pytest.raises(ValueError, match="solver input content drift"):
        build_final_input_manifest(tmp_path)


def test_solver_input_drift_requires_exact_expiring_technical_authorization(
    tmp_path, monkeypatch
):
    import time

    from factory_core.solver_input_coverage import (
        SolverInputDriftError,
        TECHNICAL_SOLVER_DRIFT_AUTHORIZATION_ENV,
        TECHNICAL_SOLVER_DRIFT_AUTHORIZATION_SCHEMA,
        TECHNICAL_SOLVER_DRIFT_AUTHORIZATION_SHA_ENV,
        solver_declared_input_coverage,
    )
    from factory_core.human_decisions import build_decision_request

    input_path = tmp_path / "data" / "raw" / "config.yaml"
    input_path.parent.mkdir(parents=True)
    input_path.write_text("alpha: 1\n", encoding="utf-8")
    _write_solver_submission_receipt_for_coverage(tmp_path, input_path)
    input_path.write_text("alpha: 2\n", encoding="utf-8")

    with pytest.raises(SolverInputDriftError) as captured:
        solver_declared_input_coverage(tmp_path)
    drift = captured.value.to_dict()
    _write_minimal_active_paper(tmp_path)
    request = build_decision_request(
        project_id=tmp_path.name,
        requested_revision=2,
        project_dir=tmp_path,
        action={"type": "approval", "gate": "content_freeze"},
        reason="review the drifted content snapshot",
    )
    assert request.metadata["solver_input_drift"] == drift
    authorization = {
        "schema_version": TECHNICAL_SOLVER_DRIFT_AUTHORIZATION_SCHEMA,
        "project_id": tmp_path.name,
        "project_path": str(tmp_path.resolve()),
        "scope": "run4_downstream_bug_validation",
        "expires_at": int(time.time()) + 3_600,
        "drifts": [drift],
        "quality_pass_fabricated": False,
        "content_freeze_approved": False,
        "delivery_allowed": False,
    }
    authorization_path = (
        tmp_path / ".factory/technical_flow/stale_solver_inputs.json"
    )
    authorization_path.parent.mkdir(parents=True)
    encoded = (
        json.dumps(authorization, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n"
    ).encode("utf-8")
    authorization_path.write_bytes(encoded)
    monkeypatch.setenv(
        TECHNICAL_SOLVER_DRIFT_AUTHORIZATION_ENV, str(authorization_path)
    )
    monkeypatch.setenv(
        TECHNICAL_SOLVER_DRIFT_AUTHORIZATION_SHA_ENV,
        hashlib.sha256(encoded).hexdigest(),
    )

    coverage = solver_declared_input_coverage(tmp_path)
    assert input_path.resolve() in coverage.included_paths
    assert coverage.authorized_drifts == (drift,)
    assert authorization_path.resolve() in coverage.evidence_paths
    assert any(
        path.name == "coverage-job.submitted.json"
        for path in coverage.evidence_paths
    )

    input_path.write_text("alpha: 3\n", encoding="utf-8")
    with pytest.raises(SolverInputDriftError):
        solver_declared_input_coverage(tmp_path)


def test_missing_solver_input_requires_exact_technical_authorization(
    tmp_path, monkeypatch
):
    import time

    from factory_core.solver_input_coverage import (
        SolverInputDriftError,
        TECHNICAL_SOLVER_DRIFT_AUTHORIZATION_ENV,
        TECHNICAL_SOLVER_DRIFT_AUTHORIZATION_SCHEMA,
        TECHNICAL_SOLVER_DRIFT_AUTHORIZATION_SHA_ENV,
        solver_declared_input_coverage,
    )

    input_path = tmp_path / "data" / "intermediate" / "m2_spectra.npz"
    input_path.parent.mkdir(parents=True)
    input_path.write_bytes(b"original solver input")
    _write_solver_submission_receipt_for_coverage(tmp_path, input_path)
    input_path.unlink()

    with pytest.raises(SolverInputDriftError) as captured:
        solver_declared_input_coverage(tmp_path)
    drift = captured.value.to_dict()
    assert drift["kind"] == "missing"
    assert drift["current"] == {"exists": False}

    authorization = {
        "schema_version": TECHNICAL_SOLVER_DRIFT_AUTHORIZATION_SCHEMA,
        "project_id": tmp_path.name,
        "project_path": str(tmp_path.resolve()),
        "scope": "run4_downstream_bug_validation",
        "expires_at": int(time.time()) + 3_600,
        "drifts": [drift],
        "quality_pass_fabricated": False,
        "content_freeze_approved": False,
        "delivery_allowed": False,
    }
    authorization_path = (
        tmp_path / ".factory/technical_flow/stale_solver_inputs.json"
    )
    authorization_path.parent.mkdir(parents=True)
    encoded = (
        json.dumps(authorization, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n"
    ).encode("utf-8")
    authorization_path.write_bytes(encoded)
    monkeypatch.setenv(
        TECHNICAL_SOLVER_DRIFT_AUTHORIZATION_ENV, str(authorization_path)
    )
    monkeypatch.setenv(
        TECHNICAL_SOLVER_DRIFT_AUTHORIZATION_SHA_ENV,
        hashlib.sha256(encoded).hexdigest(),
    )

    coverage = solver_declared_input_coverage(tmp_path)
    assert input_path.resolve() not in coverage.included_paths
    assert coverage.authorized_drifts == (drift,)
    assert authorization_path.resolve() in coverage.evidence_paths
    assert any(
        path.name == "coverage-job.submitted.json"
        for path in coverage.evidence_paths
    )

    input_path.write_bytes(b"different replacement")
    with pytest.raises(SolverInputDriftError):
        solver_declared_input_coverage(tmp_path)


def test_solver_submission_receipt_superseded_rerun_uses_latest_inputs(tmp_path):
    from factory_core.finalization import build_final_input_manifest
    from factory_core.solver_input_coverage import solver_declared_input_coverage

    _write_minimal_active_paper(tmp_path)
    input_path = tmp_path / "data" / "raw" / "config.yaml"
    input_path.parent.mkdir(parents=True)
    input_path.write_text("alpha: 1\n", encoding="utf-8")
    _write_solver_submission_receipt_for_coverage(
        tmp_path, input_path, job_id="coverage-job-old", requested_at=1
    )
    input_path.write_text("alpha: 2\n", encoding="utf-8")
    _write_solver_submission_receipt_for_coverage(
        tmp_path, input_path, job_id="coverage-job-new", requested_at=2
    )

    snapshot = build_final_input_manifest(tmp_path)
    final_paths = {item["path"] for item in snapshot.manifest["files"]}
    assert "data/raw/config.yaml" in final_paths
    assert any(path.endswith("coverage-job-new.submitted.json") for path in final_paths)
    evidence_paths = {
        path.relative_to(tmp_path).as_posix()
        for path in solver_declared_input_coverage(tmp_path).evidence_paths
    }
    assert any(path.endswith("coverage-job-new.submitted.json") for path in evidence_paths)
    assert not any(
        path.endswith("coverage-job-old.submitted.json") for path in evidence_paths
    )


def test_solver_submission_receipt_latest_rerun_drift_still_blocks(tmp_path):
    import pytest
    from factory_core.finalization import build_final_input_manifest

    _write_minimal_active_paper(tmp_path)
    input_path = tmp_path / "data" / "raw" / "config.yaml"
    input_path.parent.mkdir(parents=True)
    input_path.write_text("alpha: 1\n", encoding="utf-8")
    _write_solver_submission_receipt_for_coverage(
        tmp_path, input_path, job_id="coverage-job-old", requested_at=1
    )
    input_path.write_text("alpha: 2\n", encoding="utf-8")
    _write_solver_submission_receipt_for_coverage(
        tmp_path, input_path, job_id="coverage-job-new", requested_at=2
    )
    input_path.write_text("alpha: 3\n", encoding="utf-8")

    with pytest.raises(ValueError, match="solver input content drift"):
        build_final_input_manifest(tmp_path)


def _write_completed_job(
    project: Path,
    *,
    job_id: str,
    input_paths: tuple[Path, ...],
    output_path: Path,
    output_content: str,
    requested_at: int,
    finished_at: int,
) -> tuple[Path, Path]:
    from scripts.solver_job_receipt import (
        build_completion_receipt,
        build_submission_receipt,
        receipt_paths,
        write_receipt,
    )

    script = project / "models" / f"{job_id}.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("print('produce')\n", encoding="utf-8")
    receipt = build_submission_receipt(
        project_dir=project,
        job_id=job_id,
        backend="local",
        runtime="python",
        script=script,
        workdir=script.parent,
        argv=(),
        max_time_seconds=30,
        requested_at=requested_at,
        input_paths=input_paths,
        output_paths=(output_path,),
        seeds=(7,),
    )
    submitted, completed = receipt_paths(
        project / ".factory" / "solver_receipts", job_id
    )
    write_receipt(submitted, receipt)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(output_content, encoding="utf-8")
    completion = build_completion_receipt(
        project_dir=project,
        submission_path=submitted,
        status="COMPLETED",
        finished_at=finished_at,
        result_refs={},
    )
    write_receipt(completed, completion)
    return submitted, completed


def test_solver_input_later_completed_output_supersedes_historical_version(tmp_path):
    from factory_core.solver_input_coverage import solver_declared_input_coverage

    input_path = tmp_path / "data" / "intermediate" / "candidate.json"
    input_path.parent.mkdir(parents=True)
    input_path.write_text("alpha: 1\n", encoding="utf-8")
    answer_path = tmp_path / "results" / "answer.json"
    old_submitted, _old_completed = _write_completed_job(
        tmp_path,
        job_id="coverage-job-old-consumer",
        input_paths=(input_path,),
        output_path=answer_path,
        output_content='{"answer": 1}\n',
        requested_at=1,
        finished_at=2,
    )
    input_path.write_text("alpha: 2\n", encoding="utf-8")
    current_input = tmp_path / "data" / "raw" / "current.yaml"
    current_input.parent.mkdir(parents=True)
    current_input.write_text("source: stable\n", encoding="utf-8")
    new_submitted, _new_completed = _write_completed_job(
        tmp_path,
        job_id="coverage-job-new-consumer",
        input_paths=(current_input,),
        output_path=answer_path,
        output_content='{"answer": 2}\n',
        requested_at=3,
        finished_at=4,
    )

    coverage = solver_declared_input_coverage(tmp_path)
    assert input_path.resolve() not in coverage.included_paths
    evidence = set(coverage.evidence_paths)
    assert old_submitted.resolve() not in evidence
    assert new_submitted.resolve() in evidence


def test_solver_input_identical_later_output_supersedes_historical_version(tmp_path):
    from factory_core.solver_input_coverage import solver_declared_input_coverage

    input_path = tmp_path / "models" / "figure_generator.py"
    input_path.parent.mkdir(parents=True)
    input_path.write_text("print('old')\n", encoding="utf-8")
    figure_path = tmp_path / "figures" / "deterministic.pdf"
    old_submitted, _old_completed = _write_completed_job(
        tmp_path,
        job_id="coverage-job-old-figure",
        input_paths=(input_path,),
        output_path=figure_path,
        output_content="byte-identical figure\n",
        requested_at=1,
        finished_at=2,
    )
    input_path.write_text("print('current')\n", encoding="utf-8")
    current_source = tmp_path / "data" / "raw" / "figure.yaml"
    current_source.parent.mkdir(parents=True)
    current_source.write_text("source: stable\n", encoding="utf-8")
    new_submitted, _new_completed = _write_completed_job(
        tmp_path,
        job_id="coverage-job-new-figure",
        input_paths=(current_source,),
        output_path=figure_path,
        output_content="byte-identical figure\n",
        requested_at=3,
        finished_at=4,
    )

    coverage = solver_declared_input_coverage(tmp_path)
    evidence = set(coverage.evidence_paths)
    assert input_path.resolve() not in coverage.included_paths
    assert old_submitted.resolve() not in evidence
    assert new_submitted.resolve() in evidence


def test_solver_input_later_same_script_can_retire_obsolete_missing_output(tmp_path):
    from factory_core.solver_input_coverage import solver_declared_input_coverage
    from scripts.solver_job_receipt import (
        build_completion_receipt,
        build_submission_receipt,
        receipt_paths,
        write_receipt,
    )

    script = tmp_path / "models" / "projection.py"
    script.parent.mkdir(parents=True)
    script.write_text("print('project')\n", encoding="utf-8")
    historical_input = tmp_path / "data" / "intermediate" / "historical.json"
    historical_input.parent.mkdir(parents=True)
    historical_input.write_text('{"version": 1}\n', encoding="utf-8")
    answer = tmp_path / "results" / "answer.json"
    obsolete = tmp_path / "tables" / "obsolete.tex"
    old = build_submission_receipt(
        project_dir=tmp_path,
        job_id="coverage-job-old-projection",
        backend="local",
        runtime="python",
        script=script,
        workdir=script.parent,
        argv=(),
        max_time_seconds=30,
        requested_at=1,
        input_paths=(historical_input,),
        output_paths=(answer, obsolete),
        seeds=(),
    )
    old_submitted, old_completed = receipt_paths(
        tmp_path / ".factory" / "solver_receipts", old["job_id"]
    )
    write_receipt(old_submitted, old)
    answer.parent.mkdir(parents=True)
    obsolete.parent.mkdir(parents=True)
    answer.write_text('{"answer": 1}\n', encoding="utf-8")
    obsolete.write_text("old table\n", encoding="utf-8")
    write_receipt(
        old_completed,
        build_completion_receipt(
            project_dir=tmp_path,
            submission_path=old_submitted,
            status="COMPLETED",
            finished_at=2,
            result_refs={},
        ),
    )

    historical_input.write_text('{"version": 2}\n', encoding="utf-8")
    obsolete.unlink()
    current_input = tmp_path / "data" / "raw" / "current.json"
    current_input.parent.mkdir(parents=True)
    current_input.write_text('{"source": "current"}\n', encoding="utf-8")
    new = build_submission_receipt(
        project_dir=tmp_path,
        job_id="coverage-job-new-projection",
        backend="local",
        runtime="python",
        script=script,
        workdir=script.parent,
        argv=(),
        max_time_seconds=30,
        requested_at=3,
        input_paths=(current_input,),
        output_paths=(answer,),
        seeds=(),
    )
    new_submitted, new_completed = receipt_paths(
        tmp_path / ".factory" / "solver_receipts", new["job_id"]
    )
    write_receipt(new_submitted, new)
    answer.write_text('{"answer": 2}\n', encoding="utf-8")
    write_receipt(
        new_completed,
        build_completion_receipt(
            project_dir=tmp_path,
            submission_path=new_submitted,
            status="COMPLETED",
            finished_at=4,
            result_refs={},
        ),
    )

    coverage = solver_declared_input_coverage(tmp_path)
    evidence = set(coverage.evidence_paths)
    assert historical_input.resolve() not in coverage.included_paths
    assert old_submitted.resolve() not in evidence
    assert new_submitted.resolve() in evidence


def test_solver_input_drift_after_completed_output_still_blocks(tmp_path):
    from factory_core.solver_input_coverage import solver_declared_input_coverage

    input_path = tmp_path / "data" / "intermediate" / "candidate.json"
    input_path.parent.mkdir(parents=True)
    input_path.write_text("alpha: 1\n", encoding="utf-8")
    answer_path = tmp_path / "results" / "answer.json"
    _write_completed_job(
        tmp_path,
        job_id="coverage-job-current-consumer",
        input_paths=(input_path,),
        output_path=answer_path,
        output_content='{"answer": 1}\n',
        requested_at=1,
        finished_at=2,
    )
    source = tmp_path / "data" / "raw" / "producer.yaml"
    source.parent.mkdir(parents=True)
    source.write_text("source: stable\n", encoding="utf-8")
    _write_completed_job(
        tmp_path,
        job_id="coverage-job-input-producer",
        input_paths=(source,),
        output_path=input_path,
        output_content="alpha: 2\n",
        requested_at=3,
        finished_at=4,
    )

    with pytest.raises(ValueError, match="solver input content drift"):
        solver_declared_input_coverage(tmp_path)


def test_solver_input_from_explicitly_failed_job_is_not_final_evidence(tmp_path):
    from factory_core.solver_input_coverage import solver_declared_input_coverage
    from scripts.solver_job_receipt import (
        build_completion_receipt,
        receipt_paths,
        write_receipt,
    )

    input_path = tmp_path / "model.md"
    input_path.write_text("old model\n", encoding="utf-8")
    submitted = _write_solver_submission_receipt_for_coverage(
        tmp_path,
        input_path,
        job_id="coverage-job-failed",
        requested_at=1,
    )
    submitted_path, completed_path = receipt_paths(
        tmp_path / ".factory" / "solver_receipts", submitted["job_id"]
    )
    completion = build_completion_receipt(
        project_dir=tmp_path,
        submission_path=submitted_path,
        status="FAILED",
        finished_at=2,
        result_refs={},
    )
    write_receipt(completed_path, completion)
    input_path.write_text("current model with changed size\n", encoding="utf-8")

    coverage = solver_declared_input_coverage(tmp_path)
    assert input_path.resolve() not in coverage.included_paths
    assert submitted_path.resolve() not in coverage.evidence_paths


def test_solver_input_exclusion_receipt_is_append_only(tmp_path):
    from factory_core.solver_input_coverage import (
        build_solver_input_exclusion_receipt,
        write_solver_input_exclusion_receipt,
    )

    first = build_solver_input_exclusion_receipt(
        relative_path="replication/private_seed.npy",
        input_sha256="a" * 64,
        reason="licensed input",
    )
    path = write_solver_input_exclusion_receipt(tmp_path, first)
    assert write_solver_input_exclusion_receipt(tmp_path, first) == path

    changed = build_solver_input_exclusion_receipt(
        relative_path="replication/private_seed.npy",
        input_sha256="a" * 64,
        reason="different reason",
    )
    with pytest.raises(ValueError, match="immutable.*already differs"):
        write_solver_input_exclusion_receipt(tmp_path, changed)


def test_solver_input_exclusion_receipt_path_binds_input_hash(tmp_path):
    from factory_core.solver_input_coverage import (
        build_solver_input_exclusion_receipt,
        write_solver_input_exclusion_receipt,
    )

    first = build_solver_input_exclusion_receipt(
        relative_path="config/model.toml",
        input_sha256="a" * 64,
        reason="first version",
    )
    second = build_solver_input_exclusion_receipt(
        relative_path="config/model.toml",
        input_sha256="b" * 64,
        reason="second version",
    )
    first_path = write_solver_input_exclusion_receipt(tmp_path, first)
    second_path = write_solver_input_exclusion_receipt(tmp_path, second)

    assert first_path != second_path
    assert first_path.is_file() and second_path.is_file()
