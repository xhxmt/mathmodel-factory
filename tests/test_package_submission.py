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


def test_zip_members_exactly_match_bundle_manifest(tmp_path):
    _complete_bundle_project(tmp_path)
    output = tmp_path.parent / "submission.zip"
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

    assert result.returncode == 0, result.stdout + result.stderr
    manifest = json.loads(
        (tmp_path / ".factory/finalization/submission_bundle_manifest.json").read_text(
            encoding="utf-8"
        )
    )
    verify_zip_against_manifest(output, manifest)
    with zipfile.ZipFile(output) as archive:
        assert set(archive.namelist()) == {
            item["archive_path"] for item in manifest["members"]
        }


def test_release_zip_is_reproducible_for_identical_manifest(tmp_path):
    _complete_bundle_project(tmp_path)
    first = tmp_path.parent / "first.zip"
    second = tmp_path.parent / "second.zip"
    command = [
        sys.executable,
        str(REPO_ROOT / "scripts/package_submission.py"),
        str(tmp_path),
        "demo",
    ]

    first_result = subprocess.run(
        [*command, str(first)], cwd=REPO_ROOT, capture_output=True, text=True
    )
    assert first_result.returncode == 0, first_result.stdout + first_result.stderr
    for path in tmp_path.rglob("*"):
        if path.is_file():
            path.touch()
    second_result = subprocess.run(
        [*command, str(second)], cwd=REPO_ROOT, capture_output=True, text=True
    )
    assert second_result.returncode == 0, second_result.stdout + second_result.stderr

    assert hashlib.sha256(first.read_bytes()).digest() == hashlib.sha256(
        second.read_bytes()
    ).digest()


def _write_solver_submission_receipt_for_coverage(project, input_path):
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
        job_id="coverage-job",
        backend="local",
        runtime="python",
        script=script,
        workdir=script.parent,
        argv=(),
        max_time_seconds=30,
        requested_at=1,
        input_paths=(input_path,),
        output_paths=(output,),
        seeds=(7,),
    )
    submitted, _completed = receipt_paths(
        project / ".factory" / "solver_receipts", "coverage-job"
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
        "data/intermediate/calibration.parquet",
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
