from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

import pytest

from scripts.bootstrap_test_contract import (
    CONTRACT_VERSION,
    GROUPS,
    OUTCOME_KEYS,
    OUTCOME_SCHEMA,
)


ROOT = Path(__file__).resolve().parents[1]


def _write_group(root: Path, group, mode: str = "pass") -> None:
    outcomes = {key: 0 for key in OUTCOME_KEYS}
    outcomes["passed"] = group.expected
    junit_skipped = 0
    if mode in {"skip", "xfail", "xpass"}:
        outcomes["passed"] -= 1
        outcomes[{"skip": "skipped", "xfail": "xfailed", "xpass": "xpassed"}[mode]] = 1
        junit_skipped = int(mode != "xpass")
    if mode == "drift":
        outcomes["passed"] -= 1
    total = sum(outcomes.values())
    suites = ET.Element("testsuites", {"name": "pytest tests"})
    suite = ET.SubElement(
        suites,
        "testsuite",
        {
            "name": "pytest",
            "errors": "0",
            "failures": "0",
            "skipped": str(junit_skipped),
            "tests": str(total),
            "time": "0.001",
            "timestamp": "2026-08-30T00:00:00+00:00",
            "hostname": "test-host",
        },
    )
    for index in range(total):
        case = ET.SubElement(
            suite,
            "testcase",
            {
                "classname": f"synthetic.{group.key}",
                "name": f"test_{index}",
                "time": "0.000",
            },
        )
        if junit_skipped and index == total - 1:
            ET.SubElement(
                case,
                "skipped",
                {"type": "pytest.xfail" if mode == "xfail" else "pytest.skip"},
            )
        if mode == "unknown-child" and index == total - 1:
            ET.SubElement(case, "unexpected")
        if mode == "nested-child" and index == total - 1:
            output = ET.SubElement(case, "system-out")
            ET.SubElement(output, "unexpected")
    if mode == "duplicate":
        suites.append(ET.fromstring(ET.tostring(suite)))
    junit = root / f"{group.stem}.junit.xml"
    junit.write_bytes(ET.tostring(suites, encoding="utf-8", xml_declaration=True))
    payload = {
        "schema": OUTCOME_SCHEMA,
        "contract_version": CONTRACT_VERSION,
        "group": group.key,
        "pytest_exitstatus": 0,
        "collected": total,
        "collection_errors": 0,
        "outcomes": outcomes,
        "total": total,
    }
    (root / f"{group.stem}.outcomes.json").write_text(
        json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8"
    )
    if mode == "truncated":
        junit.write_text("<testsuites><testsuite", encoding="utf-8")


def _fixture(root: Path, mode: str = "pass") -> None:
    root.mkdir()
    for group in GROUPS:
        if mode == "missing" and group.key == "phase6-web":
            continue
        _write_group(root, group, mode if group.key == "phase6-web" else "pass")


def _verify(root: Path) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.bootstrap_test_contract",
            "verify",
            "--results-dir",
            str(root),
        ],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def test_exact_current_contract_passes_and_ignores_terminal_text_format(
    tmp_path: Path,
) -> None:
    results = tmp_path / "results"
    _fixture(results)
    (results / "pytest-phase3.log").write_text(
        "human output deliberately reformatted; 100 successes\n", encoding="utf-8"
    )
    run = _verify(results)
    assert run.returncode == 0, run.stderr
    assert "passed=657" in run.stdout
    assert "skipped=0" in run.stdout
    assert f"contract_version={CONTRACT_VERSION}" in run.stdout


@pytest.mark.parametrize(
    "mode",
    [
        "skip",
        "xfail",
        "xpass",
        "drift",
        "duplicate",
        "truncated",
        "missing",
        "unknown-child",
        "nested-child",
    ],
)
def test_non_exact_or_incomplete_result_closure_fails_closed(
    tmp_path: Path, mode: str
) -> None:
    results = tmp_path / mode
    _fixture(results, mode)
    run = _verify(results)
    assert run.returncode == 2, (run.stdout, run.stderr)
    assert "FAILED" in run.stderr


def test_duplicate_or_unparseable_outcome_json_fails_closed(tmp_path: Path) -> None:
    results = tmp_path / "results"
    _fixture(results)
    target = results / "pytest-phase6-web.outcomes.json"
    target.write_text('{"schema":"a","schema":"b"}\n', encoding="utf-8")
    assert _verify(results).returncode == 2


def test_real_pytest_plugin_distinguishes_xfail_and_non_strict_xpass(
    tmp_path: Path,
) -> None:
    inner = tmp_path / "test_inner.py"
    inner.write_text(
        "import pytest\n"
        "@pytest.fixture\n"
        "def setup_xfail(): pytest.xfail('fixture unavailable')\n"
        "def test_setup_xfail(setup_xfail): pass\n"
        "@pytest.mark.xfail(reason='expected')\n"
        "def test_xfail(): assert False\n"
        "@pytest.mark.xfail(reason='unexpected')\n"
        "def test_xpass(): pass\n",
        encoding="utf-8",
    )
    outcomes = tmp_path / "outcomes.json"
    junit = tmp_path / "junit.xml"
    basetemp = tmp_path / "base"
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONPATH"] = str(ROOT)
    run = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "-p",
            "scripts.bootstrap_pytest_outcomes",
            f"--basetemp={basetemp}",
            f"--junitxml={junit}",
            "--phase46-bootstrap-group=phase6-web",
            f"--phase46-bootstrap-outcomes={outcomes}",
            str(inner),
        ],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert run.returncode == 0, (run.stdout, run.stderr)
    payload = json.loads(outcomes.read_text(encoding="utf-8"))
    assert payload["collected"] == payload["total"] == 3
    assert payload["outcomes"] == {
        "passed": 0,
        "failed": 0,
        "skipped": 0,
        "xfailed": 2,
        "xpassed": 1,
    }
