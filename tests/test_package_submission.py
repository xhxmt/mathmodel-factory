from __future__ import annotations

import importlib.util
from pathlib import Path


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
