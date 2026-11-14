from __future__ import annotations

import importlib.util
import hashlib
import json
import sqlite3
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

    selected = {item["archive_path"] for item in module.submission_bundle_manifest(tmp_path.resolve(), base)["members"]}

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

    selected = {item["archive_path"] for item in module.submission_bundle_manifest(tmp_path.resolve(), base)["members"]}

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
    assert manifest["artifact_ownership_schema"] == "factory-native-artifact-ownership-v3"
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


def test_zip_members_exactly_match_bundle_manifest(tmp_path):
    _complete_bundle_project(tmp_path)
    output = tmp_path.parent / "submission.zip"
    module = load_module()
    manifest = module.package_submission(
        tmp_path,
        "demo",
        output,
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


def test_stage_only_package_writes_zip_without_project_manifest(tmp_path):
    project = tmp_path / "demo"
    project.mkdir()
    _complete_bundle_project(project)
    output = tmp_path / "private-staging" / "submission.zip"
    module = load_module()

    manifest = module.package_submission(
        project,
        "demo",
        output,
        stage_only=True,
    )

    assert output.is_file()
    verify_zip_against_manifest(output, manifest)
    assert not (
        project / ".factory/finalization/submission_bundle_manifest.json"
    ).exists()


def test_phase9_transition_after_manifest_blocks_zip_commit(
    tmp_path, monkeypatch
):
    project = tmp_path / "demo"
    project.mkdir()
    _complete_bundle_project(project)
    output = tmp_path / "submission.zip"
    module = load_module()
    original_manifest = module.submission_bundle_manifest
    switched = False

    def manifest_then_phase9(project_arg, base_arg):
        nonlocal switched
        manifest = original_manifest(project_arg, base_arg)
        if not switched:
            switched = True
            database = project / ".factory" / "state.db"
            database.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(database)
            try:
                connection.executescript(
                    """
                    CREATE TABLE authority_production_run_generations (
                        project_id TEXT NOT NULL,
                        workflow_id TEXT NOT NULL,
                        run_generation TEXT NOT NULL,
                        run_mode TEXT NOT NULL,
                        PRIMARY KEY (workflow_id, run_generation)
                    );
                    CREATE TABLE authority_production_run_generation_current (
                        workflow_id TEXT PRIMARY KEY,
                        run_generation TEXT NOT NULL
                    );
                    INSERT INTO authority_production_run_generations VALUES (
                        'demo', 'workflow:demo', 'run-generation:current',
                        'FORENSIC_REPLAY'
                    );
                    INSERT INTO authority_production_run_generation_current VALUES (
                        'workflow:demo', 'run-generation:current'
                    );
                    """
                )
                connection.commit()
            finally:
                connection.close()
        return manifest

    monkeypatch.setattr(module, "submission_bundle_manifest", manifest_then_phase9)

    with pytest.raises(ValueError, match="NATIVE_WORKFLOW_REQUIRED"):
        module.package_submission(project, "demo", output)

    assert switched is True
    assert not output.exists()
    assert not (
        project / ".factory/finalization/submission_bundle_manifest.json"
    ).exists()


def test_release_zip_is_reproducible_for_identical_manifest(tmp_path):
    _complete_bundle_project(tmp_path)
    first = tmp_path.parent / "first.zip"
    second = tmp_path.parent / "second.zip"
    module = load_module()
    module.package_submission(
        tmp_path,
        "demo",
        first,
    )
    for path in tmp_path.rglob("*"):
        if path.is_file():
            path.touch()
    module.package_submission(
        tmp_path,
        "demo",
        second,
    )

    assert hashlib.sha256(first.read_bytes()).digest() == hashlib.sha256(
        second.read_bytes()
    ).digest()


def test_standalone_submission_preserves_non_phase9_cli_contract(tmp_path):
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

    assert result.returncode == 0, result.stderr
    assert output.is_file()
    assert (
        tmp_path / ".factory/finalization/submission_bundle_manifest.json"
    ).is_file()


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
    assert "NATIVE_WORKFLOW_REQUIRED" in result.stderr
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
def test_solver_declared_unowned_input_is_included_in_submission_bundle(
    tmp_path, relative
):
    from factory_core.submission_bundle import submission_bundle_paths

    _write_minimal_active_paper(tmp_path)
    input_path = tmp_path / relative
    input_path.parent.mkdir(parents=True, exist_ok=True)
    input_path.write_bytes(b"solver-input-fixture")
    _write_solver_submission_receipt_for_coverage(tmp_path, input_path)

    bundle_paths = submission_bundle_paths(tmp_path, tmp_path.name, require_pdf=False)
    assert input_path.resolve() in bundle_paths


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




def test_solver_submission_receipt_input_drift_is_recorded_not_blocking(tmp_path):
    from factory_core.finalization import build_final_input_manifest

    _write_minimal_active_paper(tmp_path)
    input_path = tmp_path / "data" / "raw" / "config.yaml"
    input_path.parent.mkdir(parents=True)
    input_path.write_text("alpha: 1\n", encoding="utf-8")
    _write_solver_submission_receipt_for_coverage(tmp_path, input_path)
    input_path.write_text("alpha: 2\n", encoding="utf-8")

    snapshot = build_final_input_manifest(tmp_path)
    assert "data/raw/config.yaml" in {item["path"] for item in snapshot.manifest["files"]}
    [drift] = snapshot.manifest["solver_input_drifts"]
    assert (drift["path"], drift["kind"]) == ("data/raw/config.yaml", "content")
    assert drift["receipts"][0]["path"].endswith("coverage-job.submitted.json")




def test_missing_solver_input_is_recorded_with_its_receipts(tmp_path):
    from factory_core.solver_input_coverage import solver_declared_input_coverage

    input_path = tmp_path / "data" / "intermediate" / "m2_spectra.npz"
    input_path.parent.mkdir(parents=True)
    input_path.write_bytes(b"original solver input")
    _write_solver_submission_receipt_for_coverage(tmp_path, input_path)
    input_path.unlink()

    coverage = solver_declared_input_coverage(tmp_path)
    [drift] = coverage.drifts
    assert drift["kind"] == "missing"
    assert drift["current"] == {"exists": False}
    assert input_path.resolve() not in coverage.included_paths
    receipt = tmp_path / drift["receipts"][0]["path"]
    assert receipt.resolve() in coverage.evidence_paths


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


def test_solver_submission_receipt_latest_rerun_drift_is_still_recorded(tmp_path):
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

    drifts = build_final_input_manifest(tmp_path).manifest["solver_input_drifts"]
    assert [item["path"] for item in drifts] == ["data/raw/config.yaml"]


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


def test_solver_input_drift_after_completed_output_is_still_recorded(tmp_path):
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

    coverage = solver_declared_input_coverage(tmp_path)
    assert [item["path"] for item in coverage.drifts] == ["data/intermediate/candidate.json"]
    assert input_path.resolve() in coverage.included_paths


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






@pytest.mark.parametrize(
    "relative",
    [
        "step5_results_gap_report.md",
        "step5_bounded_repair_report.md",
        "m1_solver_evidence_failed.json",
        "paper/appendix_sources/pro01/anything.npz",
    ],
)
def test_a_registered_but_unowned_unrouted_input_is_included(tmp_path, relative):
    """Every solver-declared input is delivered, whatever the registry says of it."""

    from factory_core.artifact_policy import artifact_policy
    from factory_core.submission_bundle import submission_bundle_paths

    policy = artifact_policy(relative)
    assert policy is not None, "precondition: the path must be registered"
    assert policy.ownership_rule is None, "precondition: it must have no owner"
    assert policy.route_stage is None and policy.route_flag is None, (
        "precondition: it must have no explicit route"
    )

    _write_minimal_active_paper(tmp_path)
    input_path = tmp_path / relative
    input_path.parent.mkdir(parents=True, exist_ok=True)
    input_path.write_bytes(b"solver-input-fixture")
    _write_solver_submission_receipt_for_coverage(tmp_path, input_path)

    bundle_paths = submission_bundle_paths(tmp_path, tmp_path.name, require_pdf=False)
    assert input_path.resolve() in bundle_paths


def test_a_registered_and_routed_input_is_still_admitted(tmp_path):
    """The control: the fix must not refuse an input that really is accountable.

    Without this, "refuse more" would look like a pass on its own.
    """

    from factory_core.artifact_policy import artifact_policy
    from factory_core.submission_bundle import submission_bundle_paths

    relative = "tables.tex"
    policy = artifact_policy(relative)
    assert policy is not None, "precondition: registered"
    assert policy.ownership_rule is not None or policy.route_stage is not None, (
        "precondition: it must confer ownership or a route"
    )

    _write_minimal_active_paper(tmp_path)
    input_path = tmp_path / relative
    input_path.parent.mkdir(parents=True, exist_ok=True)
    input_path.write_bytes(b"solver-input-fixture")
    _write_solver_submission_receipt_for_coverage(tmp_path, input_path)

    selected = submission_bundle_paths(tmp_path, tmp_path.name, require_pdf=False)
    assert any(str(path).endswith(relative) for path in selected), (
        "an accountable solver-declared input must still reach the bundle"
    )


def test_an_unowned_tracked_file_is_delivered_as_an_unowned_member(tmp_path):
    """No registry rule owns it and nothing routes it: it is included, not refused."""
    from factory_core.artifact_policy import artifact_policy
    from factory_core.submission_bundle import submission_bundle_manifest

    _write_minimal_active_paper(tmp_path)
    notes = tmp_path / "extra_analysis_notes.md"
    notes.write_text("authored but unregistered\n", encoding="utf-8")
    assert artifact_policy("extra_analysis_notes.md") is None, "precondition: unowned"

    members = {
        item["archive_path"]: item
        for item in submission_bundle_manifest(tmp_path, tmp_path.name, require_pdf=False)["members"]
    }
    member = members["extra_analysis_notes.md"]
    assert (member["owner_stage"], member["semantic_domain"]) == (None, "unowned")
