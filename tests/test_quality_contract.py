import json
import subprocess
import sys
from pathlib import Path

from scripts.quality_contract import evaluate_contract, load_contract


def write_contract(project: Path, payload: dict) -> Path:
    path = project / "quality_contract.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def base_contract() -> dict:
    return {"version": 1, "claims": [], "anomaly_checks": []}


def test_hard_claim_requires_independent_evidence(tmp_path):
    payload = base_contract()
    payload["claims"].append(
        {
            "id": "P1_GEOMETRY",
            "severity": "hard",
            "statement": "The fixed strategy uses exact segment-sphere geometry.",
            "source": "problem/source.md#problem-1",
            "implementation": ["models/m1/02_model.py::intersects"],
            "evidence": [],
        }
    )

    result = evaluate_contract(load_contract(write_contract(tmp_path, payload)), tmp_path)

    assert result.passed is False
    assert result.failures[0].code == "MISSING_INDEPENDENT_EVIDENCE"


def test_failed_hard_evidence_vetoes_contract(tmp_path):
    payload = base_contract()
    payload["claims"].append(
        {
            "id": "P1_GEOMETRY",
            "severity": "hard",
            "statement": "The fixed strategy uses exact segment-sphere geometry.",
            "source": "problem/source.md#problem-1",
            "implementation": ["models/m1/02_model.py::intersects"],
            "evidence": [
                {
                    "type": "oracle",
                    "argv": [sys.executable, "-c", "raise SystemExit(7)"],
                }
            ],
        }
    )

    result = evaluate_contract(load_contract(write_contract(tmp_path, payload)), tmp_path)

    assert result.passed is False
    assert result.failures[0].code == "EVIDENCE_FAILED"
    assert result.evidence_results[0].returncode == 7


def test_passing_hard_evidence_allows_contract(tmp_path):
    payload = base_contract()
    payload["claims"].append(
        {
            "id": "P1_GEOMETRY",
            "severity": "hard",
            "statement": "The fixed strategy uses exact segment-sphere geometry.",
            "source": "problem/source.md#problem-1",
            "implementation": ["models/m1/02_model.py::intersects"],
            "evidence": [
                {
                    "type": "oracle",
                    "argv": [sys.executable, "-c", "print('oracle pass')"],
                }
            ],
        }
    )

    result = evaluate_contract(load_contract(write_contract(tmp_path, payload)), tmp_path)

    assert result.passed is True
    assert result.failures == []
    assert result.evidence_results[0].stdout.strip() == "oracle pass"


def test_anomaly_rule_is_advisory_without_problem_specific_hardening(tmp_path):
    payload = base_contract()
    payload["anomaly_checks"].append(
        {
            "id": "EVERY_RESOURCE_CONTRIBUTES",
            "type": "nonzero_each",
            "hard": False,
            "justification": "",
            "status": "failed",
            "detail": "one resource has zero marginal contribution",
        }
    )

    result = evaluate_contract(load_contract(write_contract(tmp_path, payload)), tmp_path)

    assert result.passed is True
    assert result.warnings[0].code == "ANOMALY_DETECTED"


def test_hard_anomaly_requires_problem_specific_justification(tmp_path):
    payload = base_contract()
    payload["anomaly_checks"].append(
        {
            "id": "STRICT_RESOURCE_GAIN",
            "type": "gt_strict",
            "hard": True,
            "justification": "",
            "status": "failed",
        }
    )

    result = evaluate_contract(load_contract(write_contract(tmp_path, payload)), tmp_path)

    assert result.passed is False
    assert result.failures[0].code == "UNJUSTIFIED_HARD_ANOMALY"


def test_cli_writes_machine_and_human_reports(tmp_path):
    payload = base_contract()
    payload["claims"].append(
        {
            "id": "P1_GEOMETRY",
            "severity": "hard",
            "statement": "Exact geometry is independently checked.",
            "source": "problem/source.md#problem-1",
            "implementation": ["models/m1/02_model.py::intersects"],
            "evidence": [
                {"type": "oracle", "argv": [sys.executable, "-c", "print('ok')"]}
            ],
        }
    )
    write_contract(tmp_path, payload)
    json_out = tmp_path / "quality.latest.json"
    text_out = tmp_path / "quality.latest.txt"

    completed = subprocess.run(
        [
            sys.executable,
            "scripts/verify_quality_contract.py",
            str(tmp_path),
            "--json-out",
            str(json_out),
            "--text-out",
            str(text_out),
        ],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(json_out.read_text(encoding="utf-8"))["passed"] is True
    assert "VERDICT: PASS" in text_out.read_text(encoding="utf-8")
