from __future__ import annotations

import importlib.util
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
