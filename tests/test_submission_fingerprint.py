from __future__ import annotations

import os
import json
from pathlib import Path

import pytest

import scripts.submission_fingerprint as submission_fingerprint_module
from scripts.judge_packet import build_packets
from scripts.submission_fingerprint import (
    evaluator_contract_payload,
    final_judge_is_current,
    submission_fingerprint,
    submission_fingerprint_payload,
)


def write_file(path: Path, content: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")


def make_submission(project: Path) -> None:
    base = project.name
    for relative, content in {
        f"{base}_paper.tex": "\\begin{document}\nfinal paper\n\\end{document}\n",
        f"{base}_paper.pdf": b"%PDF reviewed bytes\n",
        "problem/problem_brief.md": "problem\n",
        "references.bib": "@article{a,title={A}}\n",
        "tables/main.csv": "x,y\n1,2\n",
        "figures/main.png": b"PNG bytes",
        "model.md": "model definition\n",
        "models/solve.py": "print('solve')\n",
        "results/canonical_results.json": '{"objective": 1}\n',
        "logs/solver.log": "OPTIMAL\n",
        "claim_ledger.json": '{"claims": []}\n',
        "numbers_manifest.json": '{"numbers": []}\n',
        "number_verification.latest.json": '{"verdict": "PASS"}\n',
    }.items():
        write_file(project / relative, content)


@pytest.mark.parametrize(
    "relative",
    [
        "demo_paper.tex",
        "demo_paper.pdf",
        "references.bib",
        "tables/main.csv",
        "figures/main.png",
        "model.md",
        "models/solve.py",
        "results/canonical_results.json",
        "logs/solver.log",
        "claim_ledger.json",
        "numbers_manifest.json",
        "number_verification.latest.json",
    ],
)
def test_final_fingerprint_changes_for_every_review_or_delivery_input(
    tmp_path: Path, relative: str
) -> None:
    project = tmp_path / "demo"
    make_submission(project)
    before = submission_fingerprint(project, "demo")

    path = project / relative
    path.write_bytes(path.read_bytes() + b"changed\n")

    assert submission_fingerprint(project, "demo") != before


def test_fingerprint_recomputes_role_packets_instead_of_trusting_stale_manifests(
    tmp_path: Path,
) -> None:
    project = tmp_path / "demo"
    make_submission(project)
    build_packets(project, "demo")
    manifest_before = (project / "judge_packets/execution/manifest.json").read_bytes()
    fingerprint_before = submission_fingerprint(project, "demo")

    write_file(project / "results/canonical_results.json", '{"objective": 2}\n')

    assert (project / "judge_packets/execution/manifest.json").read_bytes() == manifest_before
    assert submission_fingerprint(project, "demo") != fingerprint_before


def test_final_judge_current_requires_the_same_nonempty_pdf(tmp_path: Path) -> None:
    project = tmp_path / "demo"
    make_submission(project)
    write_file(
        project / "judge_outputs/final_submission.sha256",
        submission_fingerprint(project, "demo") + "\n",
    )
    assert final_judge_is_current(project, "demo") is True

    write_file(project / "demo_paper.pdf", b"")
    assert final_judge_is_current(project, "demo") is False


def test_external_submission_asset_symlinks_are_not_hashed(tmp_path: Path) -> None:
    project = tmp_path / "demo"
    make_submission(project)
    external = tmp_path / "outside"
    write_file(external / "references.bib", "outside refs\n")
    write_file(external / "table.csv", "outside table\n")
    write_file(external / "figure.png", b"outside figure")

    (project / "references.bib").unlink()
    (project / "tables/main.csv").unlink()
    (project / "figures/main.png").unlink()
    os.symlink(external / "references.bib", project / "references.bib")
    os.symlink(external / "table.csv", project / "tables/main.csv")
    os.symlink(external / "figure.png", project / "figures/main.png")

    payload = submission_fingerprint_payload(project, "demo")
    assert [asset["path"] for asset in payload["submission_assets"]] == ["demo_paper.tex"]
    before = submission_fingerprint(project, "demo")
    write_file(external / "references.bib", "changed outside refs\n")
    write_file(external / "table.csv", "changed outside table\n")
    write_file(external / "figure.png", b"changed outside figure")
    assert submission_fingerprint(project, "demo") == before


def make_evaluator_factory(root: Path) -> None:
    for relative in (
        "run_paper.sh",
        "scripts/judge_packet.py",
        "scripts/aggregate_judges.py",
        "scripts/llm_judge_call.py",
        "scripts/api_agent_run.py",
        "scripts/model_dispatch_config.py",
        "prompts/judges/math_auditor.txt",
        "prompts/judges/execution_auditor.txt",
        "prompts/judges/paper_reviewer.txt",
    ):
        write_file(root / relative, f"versioned {relative}\n")
    write_file(
        root / "web/model_config.json",
        json.dumps({"_default": {"step_13": {"primary": "judge-a", "fallback": "judge-b"}}}),
    )
    write_file(
        root / "web/model_registry.json",
        json.dumps(
            {
                "models": [
                    {"id": "judge-a", "backend": "openai", "model": "model-a"},
                    {"id": "judge-b", "backend": "gemini", "model": "model-b"},
                ]
            }
        ),
    )


def test_evaluator_contract_records_prompt_implementation_and_registry_selection(
    tmp_path: Path,
) -> None:
    factory = tmp_path / "factory"
    make_evaluator_factory(factory)

    contract = evaluator_contract_payload("demo", factory)

    assert contract["prompts"]["prompts/judges/paper_reviewer.txt"]["sha256"]
    assert contract["implementation"]["scripts/aggregate_judges.py"]["sha256"]
    dispatch = contract["model_dispatch"]
    assert dispatch["selection_source"] == "model_config"
    assert dispatch["selection"]["primary_id"] == "judge-a"
    assert dispatch["selection"]["primary"]["model"] == "model-a"
    assert dispatch["selection"]["fallback_id"] == "judge-b"
    assert dispatch["selection"]["fallback"]["backend"] == "gemini"


@pytest.mark.parametrize(
    "relative",
    [
        "prompts/judges/paper_reviewer.txt",
        "scripts/aggregate_judges.py",
        "scripts/judge_packet.py",
        "scripts/llm_judge_call.py",
        "web/model_config.json",
        "web/model_registry.json",
    ],
)
def test_final_fingerprint_changes_when_evaluator_contract_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, relative: str
) -> None:
    project = tmp_path / "demo"
    make_submission(project)
    factory = tmp_path / "factory"
    make_evaluator_factory(factory)
    monkeypatch.setattr(submission_fingerprint_module, "FACTORY_ROOT", factory)
    before = submission_fingerprint(project, "demo")

    path = factory / relative
    path.write_bytes(path.read_bytes() + b"changed\n")

    assert submission_fingerprint(project, "demo") != before
