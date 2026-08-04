import hashlib
import json
import sys
from pathlib import Path

from scripts.verify_derived_artifacts import artifact_digest, verify_manifest
from scripts.create_derived_manifest import build_manifest


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def setup_project(tmp_path: Path):
    canonical = tmp_path / "results/canonical_results.json"
    generator = tmp_path / "models/generate_derived.py"
    actual_json = tmp_path / "results/summary.json"
    actual_tex = tmp_path / "tables/results.tex"
    canonical.parent.mkdir(parents=True)
    generator.parent.mkdir(parents=True)
    actual_tex.parent.mkdir(parents=True)
    canonical.write_text('{"objective": 42}\n', encoding="utf-8")
    generator.write_text(
        """import argparse, json
from pathlib import Path
p = argparse.ArgumentParser()
p.add_argument('--canonical', required=True)
p.add_argument('--output-dir', required=True)
a = p.parse_args()
value = json.loads(Path(a.canonical).read_text())['objective']
root = Path(a.output_dir)
(root / 'results').mkdir(parents=True, exist_ok=True)
(root / 'tables').mkdir(parents=True, exist_ok=True)
(root / 'results/summary.json').write_text(json.dumps({'objective': value}, sort_keys=True) + '\\n')
(root / 'tables/results.tex').write_text(f'objective={value}\\n')
""",
        encoding="utf-8",
    )
    actual_json.write_text('{"objective": 42}\n', encoding="utf-8")
    actual_tex.write_text("objective=42\n", encoding="utf-8")
    manifest = {
        "schema": "canonical-derived-artifacts-v1",
        "canonical_results": "results/canonical_results.json",
        "canonical_results_sha256": sha256(canonical),
        "generator": {
            "path": "models/generate_derived.py",
            "sha256": sha256(generator),
            "argv": [
                "__PYTHON__",
                "models/generate_derived.py",
                "--canonical",
                "__CANONICAL_RESULTS__",
                "--output-dir",
                "__OUTPUT_DIR__",
            ],
        },
        "outputs": [
            {
                "path": "results/summary.json",
                "comparison": "json_canonical",
                "sha256": artifact_digest(actual_json, "json_canonical"),
            },
            {
                "path": "tables/results.tex",
                "comparison": "bytes",
                "sha256": artifact_digest(actual_tex, "bytes"),
            },
        ],
    }
    manifest_path = tmp_path / "results/derived_artifacts.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return manifest, manifest_path, canonical, generator, actual_json, actual_tex


def test_deterministic_regeneration_matches_declared_outputs(tmp_path):
    manifest, _path, *_ = setup_project(tmp_path)

    report = verify_manifest(tmp_path, manifest)

    assert report["passed"] is True
    assert report["undeclared_generated_outputs"] == []
    assert all(item["matches_regenerated"] for item in report["outputs"])


def test_hand_edited_derived_output_fails_diff(tmp_path):
    manifest, _path, _canonical, _generator, actual_json, _actual_tex = setup_project(tmp_path)
    actual_json.write_text('{"objective": 43}\n', encoding="utf-8")

    report = verify_manifest(tmp_path, manifest)

    assert report["passed"] is False
    assert "ACTUAL_DIGEST_MISMATCH" in report["failures"]


def test_changed_canonical_or_generator_is_rejected_before_regeneration(tmp_path):
    manifest, _path, canonical, generator, *_ = setup_project(tmp_path)
    canonical.write_text('{"objective": 41}\n', encoding="utf-8")
    report = verify_manifest(tmp_path, manifest)
    assert report["passed"] is False
    assert "CANONICAL_HASH_MISMATCH" in report["failures"]

    manifest, _path, _canonical, generator, *_ = setup_project(tmp_path / "generator")
    generator.write_text("raise SystemExit('changed')\n", encoding="utf-8")
    report = verify_manifest(tmp_path / "generator", manifest)
    assert report["passed"] is False
    assert "GENERATOR_HASH_MISMATCH" in report["failures"]


def test_manifest_builder_pins_canonical_generator_and_semantic_outputs(tmp_path):
    _manifest, _path, canonical, generator, actual_json, actual_tex = setup_project(tmp_path)

    manifest = build_manifest(
        tmp_path,
        canonical_results=canonical,
        generator=generator,
        outputs=[
            (actual_json, "json_canonical"),
            (actual_tex, "bytes"),
        ],
    )

    assert manifest["canonical_results"] == "results/canonical_results.json"
    assert manifest["canonical_results_sha256"] == sha256(canonical)
    assert manifest["generator"]["path"] == "models/generate_derived.py"
    assert manifest["generator"]["sha256"] == sha256(generator)
    assert verify_manifest(tmp_path, manifest)["passed"] is True
