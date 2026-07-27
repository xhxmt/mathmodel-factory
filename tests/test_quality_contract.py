import json
import subprocess
import sys
from pathlib import Path

import pytest

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
    factory = tmp_path / "factory"
    oracle = factory / "scripts/domain_oracles/pass.py"
    oracle.parent.mkdir(parents=True)
    oracle.write_text("print('oracle pass')\n", encoding="utf-8")
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
                    "level": "factory_oracle",
                    "argv": [sys.executable, str(oracle)],
                }
            ],
        }
    )

    result = evaluate_contract(
        load_contract(write_contract(tmp_path, payload)),
        tmp_path,
        factory_root=factory,
    )

    assert result.passed is True
    assert result.failures == []
    assert result.evidence_results[0].stdout.strip() == "oracle pass"
    assert result.evidence_results[0].evidence_level == "factory_oracle"
    assert result.evidence_results[0].hard_pass_eligible is True


def test_legacy_ungraded_evidence_is_explicitly_downgraded(tmp_path):
    payload = base_contract()
    payload["claims"].append(
        {
            "id": "LEGACY_HARD",
            "severity": "hard",
            "statement": "A legacy command claims to be independent.",
            "evidence": [
                {"type": "independent_oracle", "argv": [sys.executable, "-c", "pass"]}
            ],
        }
    )

    result = evaluate_contract(load_contract(write_contract(tmp_path, payload)), tmp_path)

    assert result.passed is False
    assert {finding.code for finding in result.failures} == {
        "MISSING_TRUSTED_HARD_EVIDENCE"
    }
    assert {finding.code for finding in result.warnings} == {
        "EVIDENCE_LEVEL_DOWNGRADED"
    }
    evidence = result.evidence_results[0]
    assert evidence.declared_level is None
    assert evidence.evidence_level == "self_report"
    assert evidence.qualification == "legacy_missing_level"


def test_project_test_cannot_certify_hard_pass(tmp_path):
    payload = base_contract()
    payload["claims"].append(
        {
            "id": "PROJECT_TEST_ONLY",
            "severity": "hard",
            "statement": "A project-owned test passes.",
            "evidence": [
                {
                    "type": "pytest",
                    "level": "project_test",
                    "argv": [sys.executable, "-c", "pass"],
                }
            ],
        }
    )

    result = evaluate_contract(load_contract(write_contract(tmp_path, payload)), tmp_path)

    assert result.passed is False
    assert result.failures[-1].code == "MISSING_TRUSTED_HARD_EVIDENCE"
    assert result.evidence_results[0].returncode == 0


def test_project_script_cannot_self_declare_factory_oracle(tmp_path):
    script = tmp_path / "models/fake_oracle.py"
    script.parent.mkdir(parents=True)
    script.write_text("print('pass')\n", encoding="utf-8")
    payload = base_contract()
    payload["claims"].append(
        {
            "id": "SPOOFED_ORACLE",
            "severity": "hard",
            "statement": "A project script is not factory-owned.",
            "evidence": [
                {
                    "type": "oracle",
                    "level": "factory_oracle",
                    "argv": [sys.executable, "models/fake_oracle.py"],
                }
            ],
        }
    )

    result = evaluate_contract(load_contract(write_contract(tmp_path, payload)), tmp_path)

    assert result.passed is False
    evidence = result.evidence_results[0]
    assert evidence.evidence_level == "self_report"
    assert evidence.qualification == "factory_oracle_not_in_trusted_allowlist"


def test_factory_oracle_allowlist_checks_invoked_script_not_unused_argument(tmp_path):
    factory = tmp_path / "factory"
    trusted = factory / "scripts/domain_oracles/trusted.py"
    trusted.parent.mkdir(parents=True)
    trusted.write_text("print('trusted')\n", encoding="utf-8")
    fake = tmp_path / "fake.py"
    fake.write_text("print('fake')\n", encoding="utf-8")
    payload = base_contract()
    payload["claims"].append(
        {
            "id": "ARGV_SPOOF",
            "severity": "hard",
            "statement": "An unused trusted path cannot bless a project script.",
            "evidence": [
                {
                    "type": "oracle",
                    "level": "factory_oracle",
                    "argv": [sys.executable, "fake.py", str(trusted)],
                }
            ],
        }
    )

    result = evaluate_contract(
        load_contract(write_contract(tmp_path, payload)),
        tmp_path,
        factory_root=factory,
    )

    assert result.passed is False
    assert result.evidence_results[0].evidence_level == "self_report"


def test_distinct_safe_dual_implementations_can_certify_hard_pass(tmp_path):
    first = tmp_path / "models/a.py"
    first.parent.mkdir(parents=True, exist_ok=True)
    first.write_text("def answer(): return 42\n", encoding="utf-8")
    (tmp_path / "models/b.py").write_text(
        "def answer():\n    return 6 * 7\n", encoding="utf-8"
    )
    comparator = tmp_path / "models/compare.py"
    comparator.write_text("print('implementations agree')\n", encoding="utf-8")
    payload = base_contract()
    payload["claims"].append(
        {
            "id": "DUAL_IMPL",
            "severity": "hard",
            "statement": "Two implementations agree.",
            "evidence": [
                {
                    "type": "cross_check",
                    "level": "dual_impl",
                    "implementations": ["models/a.py::answer", "models/b.py::answer"],
                    "argv": [sys.executable, "models/compare.py"],
                }
            ],
        }
    )

    result = evaluate_contract(load_contract(write_contract(tmp_path, payload)), tmp_path)

    assert result.passed is True
    assert result.evidence_results[0].evidence_level == "dual_impl"


def test_byte_identical_dual_implementations_are_downgraded(tmp_path):
    for relative in ("models/a.py", "models/b.py"):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("def answer(): return 42\n", encoding="utf-8")
    (tmp_path / "models/compare.py").write_text("pass\n", encoding="utf-8")
    payload = base_contract()
    payload["claims"].append(
        {
            "id": "COPIED_DUAL_IMPL",
            "severity": "hard",
            "statement": "Copied files do not establish implementation independence.",
            "evidence": [
                {
                    "type": "cross_check",
                    "level": "dual_impl",
                    "implementations": ["models/a.py", "models/b.py"],
                    "argv": [sys.executable, "models/compare.py"],
                }
            ],
        }
    )

    result = evaluate_contract(load_contract(write_contract(tmp_path, payload)), tmp_path)

    assert result.passed is False
    assert result.evidence_results[0].qualification == "dual_impl_files_are_byte_identical"


def test_unsafe_dual_implementation_path_is_downgraded(tmp_path):
    inside = tmp_path / "models/a.py"
    inside.parent.mkdir(parents=True)
    inside.write_text("pass\n", encoding="utf-8")
    (tmp_path / "models/compare.py").write_text("pass\n", encoding="utf-8")
    outside = tmp_path.parent / "outside.py"
    outside.write_text("pass\n", encoding="utf-8")
    payload = base_contract()
    payload["claims"].append(
        {
            "id": "UNSAFE_DUAL_IMPL",
            "severity": "hard",
            "statement": "Traversal must not qualify as independent evidence.",
            "evidence": [
                {
                    "type": "cross_check",
                    "level": "dual_impl",
                    "implementations": ["models/a.py", "../outside.py"],
                    "argv": [sys.executable, "models/compare.py"],
                }
            ],
        }
    )

    result = evaluate_contract(load_contract(write_contract(tmp_path, payload)), tmp_path)

    assert result.passed is False
    assert result.evidence_results[0].evidence_level == "project_test"
    assert result.evidence_results[0].hard_pass_eligible is False


def test_v2_contract_requires_explicit_evidence_level(tmp_path):
    payload = base_contract()
    payload["version"] = 2
    payload["claims"].append(
        {
            "id": "V2_HARD",
            "severity": "hard",
            "statement": "v2 has an explicit trust contract.",
            "evidence": [{"type": "test", "argv": [sys.executable, "-c", "pass"]}],
        }
    )

    with pytest.raises(ValueError, match="must declare level"):
        load_contract(write_contract(tmp_path, payload))


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
    factory = tmp_path / "factory"
    oracle = factory / "scripts/domain_oracles/contract_oracle.py"
    oracle.parent.mkdir(parents=True)
    oracle.write_text("print('ok')\n", encoding="utf-8")
    payload = base_contract()
    payload["claims"].append(
        {
            "id": "P1_GEOMETRY",
            "severity": "hard",
            "statement": "Exact geometry is independently checked.",
            "source": "problem/source.md#problem-1",
            "implementation": ["models/m1/02_model.py::intersects"],
            "evidence": [
                {
                    "type": "domain_oracle",
                    "level": "factory_oracle",
                    "argv": [
                        sys.executable,
                        "__FACTORY__/scripts/domain_oracles/contract_oracle.py",
                    ],
                }
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
            "--factory-root",
            str(factory),
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
    report = json.loads(json_out.read_text(encoding="utf-8"))
    assert report["passed"] is True
    assert report["evidence_results"][0]["evidence_level"] == "factory_oracle"
    assert "level=factory_oracle (hard-pass)" in text_out.read_text(encoding="utf-8")
    assert "VERDICT: PASS" in text_out.read_text(encoding="utf-8")
