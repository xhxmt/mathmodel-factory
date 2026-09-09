from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

import pytest

from scripts.phase78_test_contract import (
    CONTRACT_SHA256,
    CONTRACT_VERSION,
    EXPECTED_TOTAL,
    GROUPS,
    JUNIT_PREFIX,
    OUTCOME_KEYS,
    OUTCOME_SCHEMA,
)


ROOT = Path(__file__).resolve().parents[1]
RUN_ID = "0123456789abcdef0123456789abcdef"
FROZEN_CONTRACT_SHA256 = (
    "5d02aa5501eb317a632f5688092ecdffa3fc9eafb2eb78d8d7d6c3a1d88dc082"
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _write_group(root: Path, group, mode: str = "pass") -> None:
    outcomes = {key: 0 for key in OUTCOME_KEYS}
    outcomes["passed"] = group.expected
    junit_skipped = 0
    junit_errors = 0
    junit_failures = 0
    if mode in {"skip", "xfail", "xpass"}:
        outcomes["passed"] -= 1
        outcomes[{"skip": "skipped", "xfail": "xfailed", "xpass": "xpassed"}[mode]] = 1
        junit_skipped = int(mode != "xpass")
    if mode == "drift":
        outcomes["passed"] -= 1
    if mode == "error":
        outcomes["passed"] -= 1
        outcomes["errors"] = 1
        junit_errors = 1
    if mode == "failure":
        outcomes["passed"] -= 1
        outcomes["failed"] = 1
        junit_failures = 1
    total = sum(outcomes.values())
    suites = ET.Element("testsuites", {"name": "pytest tests"})
    suite = ET.SubElement(
        suites,
        "testsuite",
        {
            "name": "pytest",
            "errors": str(junit_errors),
            "failures": str(junit_failures),
            "skipped": str(junit_skipped),
            "tests": str(total),
            "time": "0.001",
            "timestamp": "2026-08-30T00:00:00+00:00",
            "hostname": "test-host",
        },
    )
    properties = ET.SubElement(suite, "properties")
    for name, value in (
        ("phase78_contract_sha256", CONTRACT_SHA256),
        ("phase78_contract_version", CONTRACT_VERSION),
        ("phase78_group", group.key),
        ("phase78_run_id", "f" * 32 if mode == "stale" else RUN_ID),
    ):
        ET.SubElement(properties, "property", {"name": name, "value": value})
    for index in range(total):
        case = ET.SubElement(
            suite,
            "testcase",
            {
                "classname": f"{JUNIT_PREFIX}{RUN_ID}.synthetic.{group.key}",
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
        if junit_errors and index == total - 1:
            ET.SubElement(case, "error", {"type": "RuntimeError"})
        if junit_failures and index == total - 1:
            ET.SubElement(case, "failure", {"type": "AssertionError"})
        if mode == "unknown-child" and index == total - 1:
            ET.SubElement(case, "unexpected")
        if mode == "nested-child" and index == total - 1:
            output = ET.SubElement(case, "system-out")
            ET.SubElement(output, "unexpected")
        if mode == "wrong-prefix" and index == total - 1:
            case.set("classname", "old_run.synthetic")
    if mode == "duplicate-summary":
        suites.append(ET.fromstring(ET.tostring(suite)))
    junit = root / f"{group.stem}.junit.xml"
    junit.write_bytes(ET.tostring(suites, encoding="utf-8", xml_declaration=True))

    timestamp = _now()
    payload = {
        "schema": OUTCOME_SCHEMA,
        "contract_version": CONTRACT_VERSION,
        "contract_sha256": CONTRACT_SHA256,
        "run_id": "e" * 32 if mode == "stale-outcome" else RUN_ID,
        "group": group.key,
        "pytest_exitstatus": 0,
        "collected": total,
        "collection_errors": 0,
        "outcomes": outcomes,
        "total": total,
        "nodeids_sha256": hashlib.sha256(b"synthetic\n").hexdigest(),
        "started_utc": timestamp,
        "finished_utc": timestamp,
    }
    (root / f"{group.stem}.outcomes.json").write_text(
        json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8"
    )
    if mode == "truncated-junit":
        junit.write_text("<testsuites><testsuite", encoding="utf-8")
    if mode == "truncated-outcome":
        (root / f"{group.stem}.outcomes.json").write_text(
            '{"schema":', encoding="utf-8"
        )


def _fixture(root: Path, mode: str = "pass") -> None:
    root.mkdir()
    for group in GROUPS:
        if mode == "missing" and group.key == "adapters":
            continue
        _write_group(root, group, mode if group.key == "adapters" else "pass")
    if mode == "extra-report":
        (root / "pytest-phase78-copy.outcomes.json").write_text("{}\n", encoding="utf-8")


def _verify(root: Path, run_id: str = RUN_ID) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONPATH"] = str(ROOT)
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.phase78_test_contract",
            "verify",
            "--results-dir",
            str(root),
            "--run-id",
            run_id,
        ],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def test_exact_current_contract_passes_and_is_separate_from_phase46(
    tmp_path: Path,
) -> None:
    results = tmp_path / "results"
    _fixture(results)
    (results / "human-terminal.log").write_text(
        "arbitrary human summary formatting\n", encoding="utf-8"
    )
    run = _verify(results)
    assert run.returncode == 0, run.stderr
    assert f"passed={EXPECTED_TOTAL}" in run.stdout
    assert "skipped=0" in run.stdout
    assert f"contract_version={CONTRACT_VERSION}" in run.stdout
    assert f"contract_sha256={CONTRACT_SHA256}" in run.stdout
    assert "phase46-bootstrap-exact-count-v1" not in run.stdout


def test_contract_has_five_disjoint_nonempty_groups_and_versioned_exact_counts() -> None:
    assert [group.key for group in GROUPS] == [
        "unit",
        "runtime",
        "adapters",
        "pdf-cas",
        "e2e",
    ]
    assert tuple(group.expected for group in GROUPS) == (148, 60, 29, 12, 35)
    paths = [path for group in GROUPS for path in group.files]
    assert len(paths) == len(set(paths))
    assert all((ROOT / path).is_file() for path in paths)
    assert EXPECTED_TOTAL == sum(group.expected for group in GROUPS) == 284
    assert "tests/test_phase78_enabled_e2e.py" in GROUPS[-1].files
    assert CONTRACT_SHA256 == FROZEN_CONTRACT_SHA256


@pytest.mark.parametrize(
    "mode",
    [
        "skip",
        "error",
        "failure",
        "xfail",
        "xpass",
        "drift",
        "duplicate-summary",
        "truncated-junit",
        "truncated-outcome",
        "missing",
        "unknown-child",
        "nested-child",
        "stale",
        "stale-outcome",
        "wrong-prefix",
        "extra-report",
    ],
)
def test_nonexact_duplicate_truncated_stale_or_missing_reports_fail_closed(
    tmp_path: Path, mode: str
) -> None:
    results = tmp_path / mode
    _fixture(results, mode)
    run = _verify(results)
    assert run.returncode == 2, (run.stdout, run.stderr)
    assert "FAILED" in run.stderr


def test_duplicate_or_malformed_outcome_json_fails_closed(tmp_path: Path) -> None:
    results = tmp_path / "results"
    _fixture(results)
    target = results / "pytest-phase78-adapters.outcomes.json"
    target.write_text('{"schema":"a","schema":"b"}\n', encoding="utf-8")
    assert _verify(results).returncode == 2


def test_invalid_requested_run_identity_fails_closed(tmp_path: Path) -> None:
    results = tmp_path / "results"
    _fixture(results)
    run = _verify(results, "not-a-run-id")
    assert run.returncode == 2
    assert "FAILED" in run.stderr


def test_real_pytest_plugin_binds_junit_and_distinguishes_nonpass_categories(
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
        "def test_xpass(): pass\n"
        "@pytest.fixture\n"
        "def setup_error(): raise RuntimeError('setup failed')\n"
        "def test_setup_error(setup_error): pass\n"
        "def test_failure(): assert False\n",
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
            "scripts.phase78_pytest_outcomes",
            f"--basetemp={basetemp}",
            f"--junitxml={junit}",
            f"--junitprefix={JUNIT_PREFIX}{RUN_ID}",
            "--phase78-bootstrap-group=adapters",
            f"--phase78-bootstrap-run-id={RUN_ID}",
            f"--phase78-bootstrap-outcomes={outcomes}",
            str(inner),
        ],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert run.returncode == 1, (run.stdout, run.stderr)
    payload = json.loads(outcomes.read_text(encoding="utf-8"))
    assert payload["run_id"] == RUN_ID
    assert payload["contract_sha256"] == CONTRACT_SHA256
    assert payload["collected"] == payload["total"] == 5
    assert payload["outcomes"] == {
        "passed": 0,
        "failed": 1,
        "errors": 1,
        "skipped": 0,
        "xfailed": 2,
        "xpassed": 1,
    }
    suite = ET.parse(junit).getroot().find("testsuite")
    assert suite is not None
    property_node = suite.find("properties")
    assert property_node is not None
    properties = {
        item.attrib["name"]: item.attrib["value"]
        for item in property_node
    }
    assert properties["phase78_run_id"] == RUN_ID
    assert properties["phase78_contract_sha256"] == CONTRACT_SHA256


def test_bootstrap_uses_external_cache_and_new_contract_only() -> None:
    script = (ROOT / "bootstrap_phase78.sh").read_text(encoding="utf-8")
    assert "PHASE78_BOOTSTRAP_TMPDIR" in script
    assert "PYTHONPYCACHEPREFIX" in script
    assert "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1" in script
    assert '"no:cacheprovider"' in (
        ROOT / "scripts/phase78_test_contract.py"
    ).read_text(encoding="utf-8")
    assert "scripts.phase78_test_contract" in script
    assert "scripts.bootstrap_test_contract" not in script
    assert "pip install" not in script
    assert "curl " not in script
    assert "wget " not in script
    frozen_bootstrap = (ROOT / "bootstrap.sh").read_text(encoding="utf-8")
    assert "scripts.phase78_test_contract" not in frozen_bootstrap


def test_bootstrap_rejects_a_temporary_base_inside_source_tree() -> None:
    environment = os.environ.copy()
    environment["PYTHON_BIN"] = sys.executable
    environment["PHASE78_BOOTSTRAP_TMPDIR"] = str(ROOT)
    run = subprocess.run(
        ["bash", str(ROOT / "bootstrap_phase78.sh")],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert run.returncode == 2
    assert "temporary base must be outside the source tree" in run.stderr
    assert "RUN: preflight" not in run.stdout
