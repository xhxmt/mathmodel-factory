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
    assert first_manifest["files"][0]["included_sha256"] == hashlib.sha256(
        b"same paper"
    ).hexdigest()
    assert first_manifest["files"][0]["source_line_start"] == 1
    assert first_manifest["files"][0]["source_line_end"] == 1
    assert len(first_manifest["files"][0]["chunk_id"]) == 64

    manifest_path = project / "judge_packets/paper/manifest.json"
    context_path = project / "judge_packets/paper/context.txt"
    assert json.loads(manifest_path.read_text(encoding="utf-8")) == first_manifest
    assert "same paper" in context_path.read_text(encoding="utf-8")
    assert first_manifest["version"] == 3
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


def test_chunk_ids_bind_role_path_and_exact_included_text(tmp_path):
    project = tmp_path / "demo"
    project.mkdir()
    content = "line one\nline two\n"
    _write(project, "demo_paper.tex", content)
    _write(project, "problem/problem_brief.md", "problem\n")

    first = build_packets(project, base_name="demo")
    second = build_packets(project, base_name="demo")
    paper_item = next(
        item for item in first["paper"]["files"] if item["path"] == "demo_paper.tex"
    )
    math_item = next(
        item for item in first["math"]["files"] if item["path"] == "demo_paper.tex"
    )
    repeated = next(
        item for item in second["paper"]["files"] if item["path"] == "demo_paper.tex"
    )

    included_sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
    expected_chunk = hashlib.sha256(
        f"paper\0demo_paper.tex\0{included_sha}".encode("utf-8")
    ).hexdigest()
    assert paper_item["included_sha256"] == included_sha
    assert paper_item["chunk_id"] == expected_chunk
    assert repeated["chunk_id"] == expected_chunk
    assert math_item["chunk_id"] != paper_item["chunk_id"]
    assert paper_item["source_line_start"] == 1
    assert paper_item["source_line_end"] == 2


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

    assert usable[:2] == ["demo_paper.tex", "results/canonical_results.json"]
    assert "claim_ledger.json" in usable
    assert "FINAL PAPER CLAIM" in context
    assert '"claim_id": "objective"' in context


def test_packets_do_not_duplicate_mirrored_final_paper(tmp_path):
    project = tmp_path / "demo"
    project.mkdir()
    _write(project, "demo_paper.tex", "FINAL PAPER")
    _write(project, "paper/paper.tex", "FINAL PAPER")
    _write(project, "problem/source.md", "PROBLEM")
    _write(project, "results/canonical_results.json", '{"objective": 42}')

    manifests = build_packets(project, base_name="demo")

    for role in ("paper", "math", "execution"):
        selected = [item["path"] for item in manifests[role]["files"]]
        assert "demo_paper.tex" in selected
        assert "paper/paper.tex" not in selected


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


def test_execution_packet_places_required_implementation_before_secondary_evidence(tmp_path):
    project = tmp_path / "demo"
    project.mkdir()
    _write(project, "demo_paper.tex", "FINAL PAPER")
    _write(project, "claim_ledger.json", '{"claim": "headline"}')
    _write(project, "results/canonical_results.json", '{"objective": 42}')
    _write(project, "deliverables_verification.latest.txt", "VERDICT: PASS")
    for index in range(5):
        _write(project, f"results/secondary_{index}.json", "x" * 50_000)
    _write(project, "models/a/02_model.py", "def solve(): return 42")

    manifest = build_packets(project, base_name="demo")["execution"]
    requirements = {
        item["id"]: item for item in manifest["completeness"]["requirements"]
    }
    usable = [item["path"] for item in manifest["files"] if item["status"] != "omitted"]

    assert "claim_ledger.json" in usable
    assert requirements["implementation"]["satisfied"] is True
    assert usable.index("models/a/02_model.py") < usable.index("results/secondary_0.json")


def test_execution_packet_prioritizes_solver_trace_and_machine_reports(tmp_path):
    project = tmp_path / "demo"
    project.mkdir()
    _write(project, "demo_paper.tex", "FINAL PAPER")
    _write(project, "results/canonical_results.json", '{"objective": 42}')
    _write(project, "results/secondary.json", "x" * 100_000)
    _write(project, "models/a/02_model.py", "def predict(): return 42")
    _write(project, "models/a/03_solve.py", "from .02_model import predict")
    _write(project, "solve_log.md", "solver completed")
    _write(project, "number_chain_verification.latest.txt", "VERDICT: PASS")
    _write(project, "provenance_verification.latest.txt", "VERDICT: PASS")

    manifest = build_packets(project, base_name="demo")["execution"]
    requirements = {
        item["id"]: item for item in manifest["completeness"]["requirements"]
    }
    usable = [item["path"] for item in manifest["files"] if item["status"] != "omitted"]

    assert requirements["implementation"]["paths"] == ["models/a/03_solve.py"]
    assert requirements["model_definition"]["paths"] == ["models/a/02_model.py"]
    assert requirements["execution_trace"]["paths"] == ["solve_log.md"]
    assert requirements["number_chain"]["satisfied"] is True
    assert requirements["provenance"]["satisfied"] is True
    assert usable.index("models/a/03_solve.py") < usable.index("results/secondary.json")
    assert usable.index("number_chain_verification.latest.txt") < usable.index("results/secondary.json")


def test_math_packet_keeps_each_model_and_solver_entry_before_large_appendices(tmp_path):
    project = tmp_path / "demo"
    project.mkdir()
    _write(project, "problem/problem_brief.md", "problem")
    _write(project, "demo_paper.tex", "paper")
    _write(project, "model.md", "model exposition")
    _write(project, "models/a/02_model.py", "A_MODEL")
    _write(project, "models/a/03_solve.py", "A_SOLVER")
    _write(project, "models/a/04_postprocess.py", "X" * 150_000)
    _write(project, "models/b/02_model.py", "B_MODEL")
    _write(project, "models/b/03_solve.py", "B_SOLVER")

    manifest = build_packets(project, base_name="demo")["math"]
    by_path = {item["path"]: item for item in manifest["files"]}

    assert all(
        by_path[path]["status"] == "included"
        for path in (
            "models/a/02_model.py",
            "models/a/03_solve.py",
            "models/b/02_model.py",
            "models/b/03_solve.py",
        )
    )
    assert by_path["models/a/04_postprocess.py"]["status"] in {"truncated", "omitted"}


def test_execution_packet_reserves_budget_for_registered_question_evidence(tmp_path):
    project = tmp_path / "demo"
    project.mkdir()
    _write(project, "problem/problem_brief.md", "Question 1\nQuestion 2\nQuestion 3\nQuestion 4")
    _write(project, "demo_paper.tex", "P" * 50_000)
    _write(project, "results/canonical_results.json", "C" * 70_000)
    _write(project, "models/a/02_model.py", "M" * 20_000)
    _write(project, "models/a/03_solve.py", "S" * 30_000)
    _write(project, "solve_log.md", "L" * 17_000)
    for index in range(1, 5):
        _write(project, f"results/problem{index}/values.json", f"Q{index}" * 20_000)
        _write(project, f"results/problem{index}/solver.log", f"solver {index}")
    _write(project, "results/secondary.json", "X" * 200_000)
    registry = {
        "contract_version": "claim-registry-v1",
        "questions": [
            {
                "id": f"Q{index}",
                "statement": f"Question {index}",
                "source": {"path": "problem/problem_brief.md", "line": index},
                "required_roles": ["execution"],
            }
            for index in range(1, 5)
        ],
        "claims": [
            {
                "id": f"Q{index}_RESULT",
                "statement": f"Question {index} result",
                "question_ids": [f"Q{index}"],
                "required_roles": ["execution"],
                "artifacts": [
                    {"path": f"results/problem{index}/values.json", "roles": ["execution"]}
                ],
            }
            for index in range(1, 5)
        ],
        "delivery_requirements": [],
    }
    _write(project, "claim_registry.json", json.dumps(registry))

    manifest = build_packets(project, base_name="demo")["execution"]
    by_path = {item["path"]: item for item in manifest["files"]}

    assert manifest["limits"]["context_bytes"] == 360_000
    assert manifest["context"]["size"] <= 360_000
    assert manifest["completeness"]["status"] == "COMPLETE"
    assert manifest["claim_coverage"]["status"] == "COMPLETE"
    assert by_path["models/a/03_solve.py"]["status"] == "included"
    assert all(
        by_path[f"results/problem{index}/values.json"]["status"] == "included"
        for index in range(1, 5)
    )
    assert by_path["results/secondary.json"]["status"] in {"truncated", "omitted"}


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
    for index in range(9):
        _write(project, f"models/m{index}/02_model.py", f"MARKER_{index}\n" + "x" * 80_000)

    manifest = build_packets(project, base_name="demo")["execution"]

    statuses = {item["status"] for item in manifest["files"]}
    assert "truncated" in statuses
    assert "omitted" in statuses
    assert manifest["status_counts"]["omitted"] > 0
    for item in manifest["files"]:
        chunk_keys = {
            "chunk_id",
            "included_sha256",
            "source_line_start",
            "source_line_end",
        }
        if item["status"] == "omitted":
            assert chunk_keys.isdisjoint(item)
        else:
            assert chunk_keys <= item.keys()


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

    for role in ("paper", "math"):
        paper = next(item for item in manifests[role]["files"] if item["path"] == "demo_paper.tex")
        assert paper["status"] == "omitted"
        assert manifests[role]["completeness"]["status"] == "INCOMPLETE"
        assert "final_paper" in {
            requirement["id"]
            for requirement in manifests[role]["completeness"]["requirements"]
            if not requirement["satisfied"]
        }

    execution_paper = next(
        item
        for item in manifests["execution"]["files"]
        if item["path"] == "demo_paper.tex"
    )
    assert execution_paper["status"] == "included"


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
