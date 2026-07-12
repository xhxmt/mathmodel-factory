import hashlib
import json
from pathlib import Path


from scripts.judge_packet import build_packets


def _write(project: Path, relative: str, text: str) -> None:
    path = project / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_packets_separate_paper_math_and_execution_contexts(tmp_path):
    project = tmp_path / "demo"
    project.mkdir()
    _write(project, "demo_paper.tex", "paper body")
    _write(project, "problem/problem_brief.md", "problem statement")
    _write(project, "model.md", "mathematical model")
    _write(project, "models/m1/02_model.py", "def solve(): return 1")
    _write(project, "solve_log.md", "solver command and output")
    _write(project, "code_review.md", "VERDICT: PASS")
    _write(project, "quality_contract_verification.latest.json", '{"passed": true}')
    for forbidden in (
        "evaluation.md",
        "judge_evaluation.md",
        "review_comments.md",
        "revision_summary.md",
    ):
        _write(project, forbidden, f"self-authored {forbidden}")

    manifests = build_packets(project, base_name="demo")

    paper_paths = {item["path"] for item in manifests["paper"]["files"]}
    assert "demo_paper.tex" in paper_paths
    assert not paper_paths.intersection(
        {"evaluation.md", "judge_evaluation.md", "review_comments.md", "revision_summary.md"}
    )

    math_paths = {item["path"] for item in manifests["math"]["files"]}
    assert "problem/problem_brief.md" in math_paths
    assert "model.md" in math_paths
    assert "models/m1/02_model.py" in math_paths

    execution_paths = {item["path"] for item in manifests["execution"]["files"]}
    assert "solve_log.md" in execution_paths
    assert "code_review.md" in execution_paths
    assert "quality_contract_verification.latest.json" in execution_paths


def test_packet_manifest_has_stable_hashes_and_context(tmp_path):
    project = tmp_path / "demo"
    project.mkdir()
    _write(project, "demo_paper.tex", "same paper")

    first = build_packets(project, base_name="demo")
    second = build_packets(project, base_name="demo")

    first_manifest = first["paper"]
    assert first_manifest == second["paper"]
    assert first_manifest["files"][0]["sha256"] == hashlib.sha256(
        b"same paper"
    ).hexdigest()

    manifest_path = project / "judge_packets/paper/manifest.json"
    context_path = project / "judge_packets/paper/context.txt"
    assert json.loads(manifest_path.read_text(encoding="utf-8")) == first_manifest
    assert "same paper" in context_path.read_text(encoding="utf-8")


def test_execution_context_prioritizes_results_before_large_model_code(tmp_path):
    project = tmp_path / "demo"
    project.mkdir()
    _write(project, "models/m1/02_model.py", "x" * 2_050_000)
    _write(project, "results/canonical_results.json", '{"marker": "CANONICAL_EVIDENCE"}')
    _write(project, "solve_log.md", "SOLVER_LOG_EVIDENCE")

    build_packets(project, base_name="demo")

    context = (project / "judge_packets/execution/context.txt").read_text(encoding="utf-8")
    assert "CANONICAL_EVIDENCE" in context
    assert "SOLVER_LOG_EVIDENCE" in context


def test_execution_packet_includes_solver_logs_but_not_step_agent_logs(tmp_path):
    project = tmp_path / "demo"
    project.mkdir()
    _write(project, "logs/full_solver.log", "real solver stdout")
    _write(project, "logs/step_5_codex.log", "agent transcript")
    _write(project, "models/m1/full_solve.log", "model-owned solver stdout")

    manifests = build_packets(project, base_name="demo")
    manifest = manifests["execution"]
    paths = {item["path"] for item in manifest["files"]}
    assert "logs/full_solver.log" in paths
    assert "models/m1/full_solve.log" in paths
    assert "logs/step_5_codex.log" not in paths
    assert "models/m1/full_solve.log" not in {
        item["path"] for item in manifests["math"]["files"]
    }


def test_execution_packet_excludes_delivery_manifest_for_other_project(tmp_path):
    project = tmp_path / "demo"
    project.mkdir()
    _write(
        project,
        "delivery_manifest.json",
        json.dumps({"project": {"base": "old_project"}, "status": "LEGACY_DELIVERED"}),
    )
    _write(project, "results/canonical_results.json", '{"project": "demo"}')

    manifest = build_packets(project, base_name="demo")["execution"]
    paths = {item["path"] for item in manifest["files"]}
    assert "delivery_manifest.json" not in paths


def test_context_fits_api_limit_and_preserves_priority_math_evidence(tmp_path):
    project = tmp_path / "demo"
    project.mkdir()
    _write(project, "demo_paper.tex", "PAPER_PRIORITY\n" + "p" * 100_000)
    _write(project, "model.md", "MODEL_PRIORITY\n" + "m" * 100_000)
    _write(project, "models/m1/02_model.py", "CODE_EVIDENCE\n" + "c" * 300_000)

    build_packets(project, base_name="demo")

    context_path = project / "judge_packets/math/context.txt"
    context = context_path.read_text(encoding="utf-8")
    assert context_path.stat().st_size <= 200_000
    assert "PAPER_PRIORITY" in context
    assert "MODEL_PRIORITY" in context
    assert "CODE_EVIDENCE" in context
