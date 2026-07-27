from __future__ import annotations

import json
from pathlib import Path

from scripts.build_objective_evidence import build_objective_evidence
from scripts.objective_evidence import canonical_hash


def _project(root: Path, *, with_sources: bool = True) -> Path:
    project = root / "demo"
    project.mkdir()
    if with_sources:
        (project / "demo_paper.tex").write_text(
            r"\begin{document} result 12.5 \end{document}\n", encoding="utf-8"
        )
        (project / "symbol_table.md").write_text("| $x$ | value |\n", encoding="utf-8")
        (project / "problem").mkdir()
        (project / "results").mkdir()
        (project / "models").mkdir()
        (project / "logs").mkdir()
        (project / "logs" / "solve.log").write_text("12.5\n", encoding="utf-8")
    return project


def test_bundle_is_hash_bound_and_marks_quality_unavailable(tmp_path: Path) -> None:
    project = _project(tmp_path)
    payload = build_objective_evidence(project, "demo")

    unsigned = dict(payload)
    declared = unsigned.pop("bundle_sha256")
    assert declared == canonical_hash(unsigned)
    assert payload["schema_version"] == "objective-evidence-v1"
    assert payload["decision_semantics"] == "EVIDENCE_COLLECTION_ONLY"
    assert payload["quality_verdict"] == "UNAVAILABLE"
    assert payload["input_fingerprint"]
    assert {item["finding_id"] for item in payload["findings"]} == {
        "numbers.traceability",
        "symbols.consistency",
        "execution.artifact_presence",
    }


def test_missing_sources_are_unknown_not_pass(tmp_path: Path) -> None:
    payload = build_objective_evidence(_project(tmp_path, with_sources=False), "demo")
    by_id = {item["finding_id"]: item for item in payload["findings"]}
    assert by_id["numbers.traceability"]["status"] == "UNKNOWN"
    assert by_id["symbols.consistency"]["status"] == "UNKNOWN"
    assert by_id["execution.artifact_presence"]["status"] == "UNKNOWN"
