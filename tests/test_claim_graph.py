import json
from pathlib import Path

import pytest

from scripts.claim_graph import (
    REGISTRY_CONTRACT_VERSION,
    build_claim_registry,
    load_declared_registry,
)
from scripts.judge_packet import build_packets


def _write(project: Path, relative: str, content: str) -> Path:
    path = project / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _declared_registry(*, second_artifact: str = "models/q2.py") -> dict:
    return {
        "contract_version": REGISTRY_CONTRACT_VERSION,
        "questions": [
            {
                "id": "Q1",
                "statement": "Question 1",
                "source": {"path": "problem/problem_brief.md", "line": 1},
                "required_roles": ["math"],
            },
            {
                "id": "Q2",
                "statement": "Question 2",
                "source": {"path": "problem/problem_brief.md", "line": 2},
                "required_roles": ["math"],
            },
        ],
        "claims": [
            {
                "id": "Q1_MODEL",
                "statement": "The Q1 model is available.",
                "question_ids": ["Q1"],
                "required_roles": ["math"],
                "artifacts": [{"path": "model.md", "roles": ["math"]}],
            },
            {
                "id": "Q2_IMPLEMENTATION",
                "statement": "The Q2 implementation is available.",
                "question_ids": ["Q2"],
                "required_roles": ["math"],
                "artifacts": [{"path": second_artifact, "roles": ["math"]}],
            },
        ],
        "delivery_requirements": [],
    }


def test_declared_registry_rejects_unsafe_artifact_paths(tmp_path):
    project = tmp_path / "demo"
    project.mkdir()
    registry = _declared_registry(second_artifact="../outside.py")
    _write(project, "claim_registry.json", json.dumps(registry))

    with pytest.raises(ValueError, match="traversal"):
        load_declared_registry(project)


def test_declared_registry_cannot_be_empty(tmp_path):
    project = tmp_path / "demo"
    project.mkdir()
    _write(
        project,
        "claim_registry.json",
        json.dumps(
            {
                "contract_version": REGISTRY_CONTRACT_VERSION,
                "questions": [],
                "claims": [],
                "delivery_requirements": [],
            }
        ),
    )

    with pytest.raises(ValueError, match="at least one question"):
        load_declared_registry(project)


def test_declared_registry_rejects_unknown_question_references(tmp_path):
    project = tmp_path / "demo"
    project.mkdir()
    _write(project, "problem/problem_brief.md", "Question 1\nQuestion 2\n")
    registry = _declared_registry()
    registry["claims"][0]["question_ids"] = ["Q_DOES_NOT_EXIST"]
    _write(project, "claim_registry.json", json.dumps(registry))

    with pytest.raises(ValueError, match="unknown questions"):
        load_declared_registry(project)


def test_declared_registry_cannot_omit_detected_problem_question(tmp_path):
    project = tmp_path / "demo"
    project.mkdir()
    _write(
        project,
        "problem/problem_brief.md",
        "### Question 1: first\n### Question 2: second\n",
    )
    registry = _declared_registry()
    registry["questions"] = registry["questions"][:1]
    registry["claims"] = registry["claims"][:1]
    _write(project, "claim_registry.json", json.dumps(registry))

    with pytest.raises(ValueError, match="missing=Q2"):
        load_declared_registry(project)


def test_declared_registry_cannot_omit_problem_delivery_requirement(tmp_path):
    project = tmp_path / "demo"
    project.mkdir()
    _write(project, "problem/problem_brief.md", "Question 1\nQuestion 2\n")
    _write(
        project,
        "problem/deliverables.json",
        json.dumps(
            {
                "schema_version": 1,
                "attachments": [{"file": "result.xlsx", "problem": "问题1"}],
                "strategy_tables": [],
            }
        ),
    )
    _write(project, "claim_registry.json", json.dumps(_declared_registry()))

    with pytest.raises(ValueError, match="delivery requirements do not match"):
        load_declared_registry(project)


def test_declared_registry_symlink_cannot_escape_project(tmp_path):
    project = tmp_path / "demo"
    project.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps(_declared_registry()), encoding="utf-8")
    (project / "claim_registry.json").symlink_to(outside)

    with pytest.raises(ValueError, match="inside the project"):
        load_declared_registry(project)


def test_packet_reports_each_declared_question_and_missing_claim_artifact(tmp_path):
    project = tmp_path / "demo"
    project.mkdir()
    _write(project, "problem/problem_brief.md", "Question 1\nQuestion 2\n")
    _write(project, "demo_paper.tex", "final paper")
    _write(project, "model.md", "model evidence")
    _write(project, "claim_registry.json", json.dumps(_declared_registry()))

    manifest = build_packets(project, base_name="demo")["math"]

    coverage = manifest["claim_coverage"]
    assert coverage["status"] == "INCOMPLETE"
    assert coverage["missing_question_ids"] == ["Q2"]
    assert coverage["missing_claim_ids"] == ["Q2_IMPLEMENTATION"]
    assert {item["id"]: item["status"] for item in coverage["questions"]} == {
        "Q1": "COVERED",
        "Q2": "MISSING",
    }
    requirements = {
        item["id"]: item for item in manifest["completeness"]["requirements"]
    }
    assert requirements["claim_registry"]["satisfied"] is True
    assert requirements["claim:Q1_MODEL"]["satisfied"] is True
    assert requirements["claim:Q2_IMPLEMENTATION"]["satisfied"] is False
    assert manifest["completeness"]["eligible"] is False


def test_declared_claim_artifact_is_selected_and_can_complete_coverage(tmp_path):
    project = tmp_path / "demo"
    project.mkdir()
    _write(project, "problem/problem_brief.md", "Question 1\nQuestion 2\n")
    _write(project, "demo_paper.tex", "final paper")
    _write(project, "model.md", "model evidence")
    _write(project, "models/q2.py", "def solve(): return 2\n")
    _write(project, "claim_registry.json", json.dumps(_declared_registry()))

    manifest = build_packets(project, base_name="demo")["math"]

    assert manifest["claim_coverage"]["status"] == "COMPLETE"
    assert manifest["claim_coverage"]["missing_question_ids"] == []
    assert manifest["completeness"]["eligible"] is True
    by_path = {item["path"]: item for item in manifest["files"]}
    assert by_path["models/q2.py"]["status"] == "included"


def test_derived_registry_exposes_unregistered_question_claims(tmp_path):
    project = tmp_path / "demo"
    project.mkdir()
    _write(
        project,
        "problem/problem_brief.md",
        "### 问题 1：基准模型\n内容\n### 问题 2：优化模型\n内容\n",
    )
    _write(project, "demo_paper.tex", "问题一回答\n问题二回答\n")
    _write(project, "models/q1.py", "def solve(): return 1\n")
    _write(
        project,
        "quality_contract.json",
        json.dumps(
            {
                "version": 1,
                "claims": [
                    {
                        "id": "P1_MODEL",
                        "severity": "hard",
                        "statement": "Q1 uses the registered model.",
                        "source": "problem/problem_brief.md#问题-1",
                        "implementation": ["models/q1.py::solve"],
                        "evidence": [],
                    }
                ],
                "anomaly_checks": [],
            }
        ),
    )

    registry = build_claim_registry(project, "demo")
    manifests = build_packets(project, "demo")

    assert registry["source"]["mode"] == "derived"
    assert registry["source"]["declared_required"] is False
    assert "semantic answer correctness" in registry["source"]["limitations"][0]
    assert [question["id"] for question in registry["questions"]] == ["Q1", "Q2"]
    assert manifests["paper"]["claim_coverage"]["status"] == "DERIVED_ONLY"
    assert manifests["paper"]["claim_coverage"]["eligible"] is False
    assert manifests["math"]["claim_coverage"]["status"] == "INCOMPLETE"
    assert manifests["math"]["claim_coverage"]["missing_question_ids"] == ["Q2"]
    missing_requirement = next(
        item
        for item in manifests["math"]["completeness"]["requirements"]
        if item["id"] == "question:Q2:registered_claim"
    )
    assert missing_requirement["paths"] == []
    assert missing_requirement["satisfied"] is False


def test_v2_quality_contract_requires_declared_claim_registry(tmp_path):
    project = tmp_path / "demo"
    project.mkdir()
    _write(project, "problem/problem_brief.md", "### 问题 1：基准模型\n")
    _write(project, "demo_paper.tex", "问题一回答\n")
    _write(
        project,
        "quality_contract.json",
        json.dumps({"version": 2, "claims": [], "anomaly_checks": []}),
    )

    registry = build_claim_registry(project, "demo")
    manifest = build_packets(project, "demo")["paper"]

    assert registry["source"]["mode"] == "derived"
    assert registry["source"]["declared_required"] is True
    assert registry["source"]["declared_missing"] is True
    assert manifest["claim_coverage"]["status"] == "INCOMPLETE"
    requirement = next(
        item
        for item in manifest["completeness"]["requirements"]
        if item["id"] == "claim_registry"
    )
    assert requirement["paths"] == []
    assert requirement["satisfied"] is False


def test_derived_registry_reads_delivery_requirements(tmp_path):
    project = tmp_path / "demo"
    project.mkdir()
    _write(project, "problem/problem_brief.md", "### 问题 1：结果\n")
    _write(project, "demo_paper.tex", "final result table")
    _write(
        project,
        "problem/deliverables.json",
        json.dumps(
            {
                "schema_version": 1,
                "attachments": [],
                "strategy_tables": [
                    {"problem": "问题1", "description": "Q1 result table", "fields": ["x"]}
                ],
            }
        ),
    )

    registry = build_claim_registry(project, "demo")
    manifest = build_packets(project, "demo")["paper"]

    assert registry["delivery_requirements"][0]["question_ids"] == ["Q1"]
    delivery = next(
        item
        for item in manifest["claim_coverage"]["claims"]
        if item["id"] == "DELIVERY_TABLE_1"
    )
    assert delivery["status"] == "COVERED"


def test_derived_quality_claim_can_cover_multiple_referenced_questions(tmp_path):
    project = tmp_path / "demo"
    project.mkdir()
    _write(
        project,
        "problem/problem_brief.md",
        "### 问题 1：基准\n### 问题 3：扩展\n",
    )
    _write(project, "models/shared.py", "def solve(): return 1\n")
    _write(
        project,
        "quality_contract.json",
        json.dumps(
            {
                "version": 1,
                "claims": [
                    {
                        "id": "SHARED_MODEL",
                        "severity": "hard",
                        "statement": "One predicate supports Q1 and Q3.",
                        "source": "problem/problem_brief.md#问题-1-与问题-3",
                        "implementation": ["models/shared.py::solve"],
                        "evidence": [],
                    }
                ],
                "anomaly_checks": [],
            }
        ),
    )

    registry = build_claim_registry(project, "demo")

    claim = next(item for item in registry["claims"] if item["id"] == "SHARED_MODEL")
    assert claim["question_ids"] == ["Q1", "Q3"]
