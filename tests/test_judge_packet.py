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
    math_requirements = {
        item["id"]: item for item in manifests["math"]["completeness"]["requirements"]
    }
    assert math_requirements["final_paper"]["paths"] == ["demo_paper.tex"]
    assert math_requirements["final_paper"]["satisfied"] is True

    execution_paths = {item["path"] for item in manifests["execution"]["files"]}
    assert "demo_paper.tex" in execution_paths
    assert "solve_log.md" in execution_paths
    assert "code_review.md" not in execution_paths
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
    assert first_manifest["version"] == 2
    assert first_manifest["files"][0]["status"] == "included"
    assert first_manifest["status_counts"] == {
        "included": 1,
        "truncated": 0,
        "omitted": 0,
    }
    assert first_manifest["completeness"]["contract_version"] == "judge-packet-completeness-v1"
    assert first_manifest["completeness"]["status"] == "INCOMPLETE"
    assert first_manifest["completeness"]["requirements"][1]["id"] == "problem_statement"


def test_packet_omits_symlink_targets_outside_project_root(tmp_path):
    project = tmp_path / "demo"
    project.mkdir()
    secret = tmp_path / "outside-secret.tex"
    secret.write_text("OUTSIDE_PROJECT_SECRET", encoding="utf-8")
    (project / "demo_paper.tex").symlink_to(secret)

    manifests = build_packets(project, base_name="demo")

    for role in ("paper", "math", "execution"):
        manifest = manifests[role]
        item = next(entry for entry in manifest["files"] if entry["path"] == "demo_paper.tex")
        context = (project / "judge_packets" / role / "context.txt").read_text(
            encoding="utf-8"
        )
        assert item == {
            "path": "demo_paper.tex",
            "status": "omitted",
            "reason": "outside_project_root",
        }
        assert "OUTSIDE_PROJECT_SECRET" not in context


def test_packet_allows_symlink_targets_that_remain_inside_project_root(tmp_path):
    project = tmp_path / "demo"
    project.mkdir()
    _write(project, "paper/source.tex", "IN_PROJECT_PAPER")
    (project / "demo_paper.tex").symlink_to(project / "paper/source.tex")

    manifest = build_packets(project, base_name="demo")["paper"]
    item = next(entry for entry in manifest["files"] if entry["path"] == "demo_paper.tex")
    context = (project / "judge_packets/paper/context.txt").read_text(encoding="utf-8")

    assert item["status"] == "included"
    assert item["sha256"] == hashlib.sha256(b"IN_PROJECT_PAPER").hexdigest()
    assert "IN_PROJECT_PAPER" in context


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


def test_execution_packet_excludes_delivery_and_self_authored_status(tmp_path):
    project = tmp_path / "demo"
    project.mkdir()
    _write(
        project,
        "delivery_manifest.json",
        json.dumps({"project": {"base": "old_project"}, "status": "LEGACY_DELIVERED"}),
    )
    _write(project, "results/canonical_results.json", '{"project": "demo"}')
    _write(project, "evaluation/score.json", '{"score": 100}')
    _write(project, "archive/old/results.json", '{"old": true}')
    _write(project, "judge_evaluation.md", "VERDICT: PASS")
    _write(project, "code_review.md", "VERDICT: PASS")

    manifest = build_packets(project, base_name="demo")["execution"]
    paths = {item["path"] for item in manifest["files"]}
    assert "delivery_manifest.json" not in paths
    assert "evaluation/score.json" not in paths
    assert "archive/old/results.json" not in paths
    assert "judge_evaluation.md" not in paths
    assert "code_review.md" not in paths


def test_execution_packet_includes_claim_ledger_and_paper_before_results(tmp_path):
    project = tmp_path / "demo"
    project.mkdir()
    _write(project, "demo_paper.tex", "FINAL PAPER CLAIM: objective is 42")
    _write(project, "claim_ledger.json", '{"claim_id": "objective", "value": 42}')
    _write(project, "results/canonical_results.json", '{"objective": 42}')

    manifest = build_packets(project, base_name="demo")["execution"]
    usable = [item["path"] for item in manifest["files"] if item["status"] != "omitted"]
    context = (project / "judge_packets/execution/context.txt").read_text(encoding="utf-8")

    assert usable[:2] == ["claim_ledger.json", "demo_paper.tex"]
    assert "FINAL PAPER CLAIM" in context
    assert '"claim_id": "objective"' in context


def test_execution_completeness_selects_solver_code_not_readme_or_config(tmp_path):
    project = tmp_path / "demo"
    project.mkdir()
    _write(project, "demo_paper.tex", "FINAL PAPER")
    _write(project, "results/canonical_results.json", '{"objective": 42}')
    _write(project, "solve_log.md", "solver completed")
    _write(project, "models/a/README.md", "documentation only")
    _write(project, "models/a/config.json", '{"method": "demo"}')
    _write(project, "models/a/01_data.py", "print('preprocess')")
    _write(project, "models/a/02_model.py", "def solve(): return 42")

    manifest = build_packets(project, base_name="demo")["execution"]
    requirements = {
        item["id"]: item for item in manifest["completeness"]["requirements"]
    }

    assert requirements["implementation"]["paths"] == ["models/a/02_model.py"]
    assert requirements["implementation"]["satisfied"] is True


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
    assert "MODEL_PRIORITY" in context
    assert "CODE_EVIDENCE" in context
    by_path = {
        item["path"]: item
        for item in json.loads(
            (project / "judge_packets/math/manifest.json").read_text(encoding="utf-8")
        )["files"]
    }
    assert by_path["demo_paper.tex"]["status"] == "omitted"
    assert by_path["model.md"]["status"] == "included"
    assert by_path["models/m1/02_model.py"]["status"] == "truncated"
    manifest = json.loads(
        (project / "judge_packets/math/manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["completeness"]["status"] == "INCOMPLETE"


def test_manifest_marks_files_omitted_by_total_context_limit(tmp_path):
    project = tmp_path / "demo"
    project.mkdir()
    for index in range(5):
        _write(project, f"models/m{index}/02_model.py", f"MARKER_{index}\n" + "x" * 80_000)

    manifest = build_packets(project, base_name="demo")["execution"]

    statuses = {item["status"] for item in manifest["files"]}
    assert "truncated" in statuses
    assert "omitted" in statuses
    assert manifest["status_counts"]["omitted"] > 0


def test_role_completeness_contracts_require_full_primary_evidence(tmp_path):
    project = tmp_path / "demo"
    project.mkdir()
    _write(project, "demo_paper.tex", "final paper")
    _write(project, "problem/problem_brief.md", "problem statement")
    _write(project, "model.md", "mathematical exposition")
    _write(project, "results/canonical_results.json", '{"objective": 42}')
    _write(project, "models/m1/02_model.py", "print(42)")
    _write(project, "solve_log.md", "python models/m1/02_model.py\n42")

    manifests = build_packets(project, base_name="demo")

    assert all(
        manifest["completeness"]["status"] == "COMPLETE"
        for manifest in manifests.values()
    )
    assert {
        requirement["id"]
        for requirement in manifests["math"]["completeness"]["requirements"]
    } == {"problem_statement", "final_paper", "mathematical_exposition"}
    assert {
        requirement["id"]
        for requirement in manifests["execution"]["completeness"]["requirements"]
    } == {"final_paper", "primary_results", "implementation", "execution_trace"}


def test_oversized_critical_paper_makes_roles_incomplete_instead_of_truncated(tmp_path):
    project = tmp_path / "demo"
    project.mkdir()
    _write(project, "demo_paper.tex", "P" * 200_000)
    _write(project, "problem/problem_brief.md", "problem statement")
    _write(project, "model.md", "model")

    manifests = build_packets(project, base_name="demo")

    for role in ("paper", "math", "execution"):
        paper = next(item for item in manifests[role]["files"] if item["path"] == "demo_paper.tex")
        assert paper["status"] == "omitted"
        assert manifests[role]["completeness"]["status"] == "INCOMPLETE"
        assert "final_paper" in {
            requirement["id"]
            for requirement in manifests[role]["completeness"]["requirements"]
            if not requirement["satisfied"]
        }


def test_noncritical_large_code_is_disclosed_without_blocking_execution_role(tmp_path):
    project = tmp_path / "demo"
    project.mkdir()
    _write(project, "demo_paper.tex", "final paper")
    _write(project, "results/canonical_results.json", '{"objective": 42}')
    _write(project, "models/a_primary/02_model.py", "print(42)")
    _write(project, "models/z_appendix/extra.py", "x" * 100_000)
    _write(project, "solve_log.md", "python models/a_primary/02_model.py\n42")

    manifest = build_packets(project, base_name="demo")["execution"]

    assert manifest["completeness"]["status"] == "COMPLETE"
    limitation = next(
        item for item in manifest["completeness"]["limitations"]
        if item["path"] == "models/z_appendix/extra.py"
    )
    assert limitation["status"] == "truncated"
    assert limitation["critical"] is False
