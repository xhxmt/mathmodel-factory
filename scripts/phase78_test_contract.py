"""Fail-closed, exact-count test contract for the Phase 7+8 bootstrap.

This contract is deliberately independent from the frozen Phase 3-6 bootstrap
contract.  Each invocation binds both structured representations to a fresh
run identifier and to a digest of this versioned group/file/count contract.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
from typing import Any, NoReturn, Sequence
import xml.etree.ElementTree as ET


CONTRACT_VERSION = "phase78-bootstrap-exact-count-v1"
OUTCOME_SCHEMA = "phase78-pytest-outcomes-v1"
OUTCOME_KEYS = ("passed", "failed", "errors", "skipped", "xfailed", "xpassed")
RUN_ID_PATTERN = re.compile(r"[0-9a-f]{32}\Z")
JUNIT_PREFIX = "phase78_"
SOURCE_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True, slots=True)
class GroupContract:
    key: str
    stem: str
    expected: int
    files: tuple[str, ...]


# Counts are versioned and exact.  Adding or removing a test requires an
# intentional update here; the Phase 3-6 100/274/146/29/108 contract remains
# untouched in scripts/bootstrap_test_contract.py.
GROUPS = (
    GroupContract(
        "unit",
        "pytest-phase78-unit",
        148,
        (
            "tests/test_evidence_grounding.py",
            "tests/test_phase78_config.py",
            "tests/test_phase78_current.py",
            "tests/test_phase78_deadline.py",
            "tests/test_phase78_phase6_access_proof.py",
            "tests/test_phase78_bootstrap_contract.py",
            "tests/test_phase8_data_egress.py",
            "tests/test_phase8_reference_evidence.py",
        ),
    ),
    GroupContract(
        "runtime",
        "pytest-phase78-runtime",
        39,
        (
            "tests/test_phase7_grounding_runtime.py",
            "tests/test_phase8_evidence_egress_runtime.py",
        ),
    ),
    GroupContract(
        "adapters",
        "pytest-phase78-adapters",
        24,
        (
            "tests/test_phase78_cli_gate.py",
            "tests/test_phase78_work_ledger.py",
            "tests/test_phase78_web_api.py",
        ),
    ),
    GroupContract(
        "pdf-cas",
        "pytest-phase78-pdf-cas",
        12,
        ("tests/test_phase8_reference_materializer.py",),
    ),
    GroupContract(
        "e2e",
        "pytest-phase78-e2e",
        31,
        (
            "tests/test_phase2_8_shadow_integration.py",
            "tests/test_phase8_shadow_isolation.py",
            "tests/test_phase78_enabled_e2e.py",
        ),
    ),
)
GROUP_BY_KEY = {group.key: group for group in GROUPS}
EXPECTED_TOTAL = sum(group.expected for group in GROUPS)


class ContractError(ValueError):
    """A structured bootstrap result is missing, stale, or non-exact."""


def _validate_contract_definition(groups: Sequence[GroupContract]) -> None:
    if tuple(group.key for group in groups) != (
        "unit",
        "runtime",
        "adapters",
        "pdf-cas",
        "e2e",
    ):
        raise ContractError("contract must declare the five ordered Phase 7+8 groups")
    seen_keys: set[str] = set()
    seen_stems: set[str] = set()
    seen_files: set[str] = set()
    seen_files_casefold: set[str] = set()
    for group in groups:
        if (
            re.fullmatch(r"[a-z0-9-]+", group.key) is None
            or group.key in seen_keys
            or re.fullmatch(r"pytest-phase78-[a-z0-9-]+", group.stem) is None
            or group.stem in seen_stems
            or type(group.expected) is not int
            or group.expected <= 0
            or not group.files
        ):
            raise ContractError(f"invalid or duplicate contract group: {group!r}")
        seen_keys.add(group.key)
        seen_stems.add(group.stem)
        for raw_path in group.files:
            path = PurePosixPath(raw_path)
            if (
                not raw_path.startswith("tests/test_")
                or path.is_absolute()
                or ".." in path.parts
                or "\\" in raw_path
                or raw_path != path.as_posix()
                or raw_path in seen_files
                or raw_path.casefold() in seen_files_casefold
            ):
                raise ContractError(f"invalid or duplicate test path: {raw_path!r}")
            seen_files.add(raw_path)
            seen_files_casefold.add(raw_path.casefold())


_validate_contract_definition(GROUPS)


def contract_sha256(groups: Sequence[GroupContract] = GROUPS) -> str:
    payload = {
        "contract_version": CONTRACT_VERSION,
        "groups": [
            {
                "expected": group.expected,
                "files": list(group.files),
                "key": group.key,
                "stem": group.stem,
            }
            for group in groups
        ],
    }
    raw = json.dumps(
        payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("ascii")
    return hashlib.sha256(raw).hexdigest()


CONTRACT_SHA256 = contract_sha256()


def validate_contracted_test_files(
    groups: Sequence[GroupContract] = GROUPS,
) -> None:
    for group in groups:
        for relative in group.files:
            path = SOURCE_ROOT / relative
            try:
                info = path.lstat()
            except OSError as error:
                raise ContractError(
                    f"missing contracted test file {relative}: {error}"
                ) from error
            if not stat.S_ISREG(info.st_mode):
                raise ContractError(
                    f"contracted test is not a regular file: {relative}"
                )


def _run_id(value: Any, label: str = "run id") -> str:
    if type(value) is not str or RUN_ID_PATTERN.fullmatch(value) is None:
        raise ContractError(f"{label} must be exactly 32 lowercase hexadecimal digits")
    return value


def _read(path: Path, limit: int) -> bytes:
    try:
        info = path.lstat()
    except OSError as error:
        raise ContractError(f"missing result {path}: {error}") from error
    if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= limit:
        raise ContractError(f"not a bounded non-empty regular result: {path}")
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise ContractError(f"cannot read {path}: {error}") from error
    if len(raw) != info.st_size:
        raise ContractError(f"result changed while reading: {path}")
    return raw


def _uint(value: Any, label: str) -> int:
    if type(value) is not int or value < 0:
        raise ContractError(f"{label} must be a non-negative integer")
    return value


def _xuint(suite: ET.Element, key: str, path: Path) -> int:
    raw = suite.get(key)
    if raw is None or not raw.isascii() or not raw.isdecimal():
        raise ContractError(f"{path}: invalid testsuite {key}")
    return int(raw)


def _timestamp(value: Any, label: str) -> datetime:
    if (
        type(value) is not str
        or re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z", value)
        is None
    ):
        raise ContractError(f"{label} must be a UTC RFC3339 timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise ContractError(f"{label} must be a UTC RFC3339 timestamp") from error
    if parsed.tzinfo != timezone.utc:
        raise ContractError(f"{label} must use UTC")
    return parsed


def _junit(path: Path, group: GroupContract, run_id: str) -> int:
    raw = _read(path, 8 * 1024 * 1024)
    if b"<!doctype" in raw.lower() or b"<!entity" in raw.lower():
        raise ContractError(f"{path}: XML declarations/entities forbidden")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as error:
        raise ContractError(f"{path}: malformed/truncated JUnit XML: {error}") from error
    suites = list(root) if root.tag == "testsuites" else []
    if len(suites) != 1 or suites[0].tag != "testsuite":
        raise ContractError(f"{path}: require exactly one testsuite summary")
    suite = suites[0]
    if (
        suite.get("name") != "pytest"
        or not suite.get("timestamp")
        or not suite.get("hostname")
    ):
        raise ContractError(f"{path}: incomplete pytest metadata")
    try:
        elapsed = float(suite.get("time", "nan"))
    except ValueError as error:
        raise ContractError(f"{path}: invalid time") from error
    if not math.isfinite(elapsed) or elapsed < 0:
        raise ContractError(f"{path}: invalid time")
    counts = {
        key: _xuint(suite, key, path)
        for key in ("tests", "errors", "failures", "skipped")
    }
    cases = [child for child in suite if child.tag == "testcase"]
    allowed_tags = {"properties", "testcase", "system-out", "system-err"}
    if (
        any(child.tag not in allowed_tags for child in suite)
        or len(cases) != counts["tests"]
    ):
        raise ContractError(f"{path}: unexpected/incomplete suite members")

    property_nodes = [child for child in suite if child.tag == "properties"]
    if len(property_nodes) != 1:
        raise ContractError(f"{path}: require exactly one properties summary")
    properties: dict[str, str] = {}
    for item in property_nodes[0]:
        if item.tag != "property" or set(item.attrib) != {"name", "value"}:
            raise ContractError(f"{path}: malformed JUnit property")
        name = item.attrib["name"]
        if name in properties:
            raise ContractError(f"{path}: duplicate JUnit property {name!r}")
        properties[name] = item.attrib["value"]
    required_properties = {
        "phase78_contract_sha256": CONTRACT_SHA256,
        "phase78_contract_version": CONTRACT_VERSION,
        "phase78_group": group.key,
        "phase78_run_id": run_id,
    }
    if any(properties.get(key) != value for key, value in required_properties.items()):
        raise ContractError(f"{path}: stale or mismatched JUnit identity")

    prefix = f"{JUNIT_PREFIX}{run_id}."
    seen: set[tuple[str, str]] = set()
    for case in cases:
        identity = (case.get("classname", ""), case.get("name", ""))
        if (
            not all(identity)
            or not identity[0].startswith(prefix)
            or identity in seen
        ):
            raise ContractError(f"{path}: stale/missing/duplicate testcase {identity!r}")
        seen.add(identity)
        if list(case):
            raise ContractError(
                f"{path}: testcase {identity!r} has unexpected child members"
            )
    required = {
        "tests": group.expected,
        "errors": 0,
        "failures": 0,
        "skipped": 0,
    }
    if counts != required:
        raise ContractError(f"{path}: counts {counts!r}; required {required!r}")
    return counts["tests"]


def _no_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ContractError(f"duplicate JSON key {key!r}")
        value[key] = item
    return value


def _outcomes(path: Path, group: GroupContract, run_id: str) -> dict[str, int]:
    try:
        payload = json.loads(
            _read(path, 4 * 1024 * 1024).decode("utf-8", errors="strict"),
            object_pairs_hook=_no_duplicate_json_keys,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ContractError(f"{path}: malformed outcome JSON: {error}") from error
    keys = {
        "schema",
        "contract_version",
        "contract_sha256",
        "run_id",
        "group",
        "pytest_exitstatus",
        "collected",
        "collection_errors",
        "outcomes",
        "total",
        "nodeids_sha256",
        "started_utc",
        "finished_utc",
    }
    if type(payload) is not dict or set(payload) != keys:
        raise ContractError(f"{path}: wrong outcome schema")
    identity = (
        payload["schema"],
        payload["contract_version"],
        payload["contract_sha256"],
        payload["run_id"],
        payload["group"],
    )
    if identity != (
        OUTCOME_SCHEMA,
        CONTRACT_VERSION,
        CONTRACT_SHA256,
        run_id,
        group.key,
    ):
        raise ContractError(f"{path}: stale or mismatched outcome identity")
    _run_id(payload["run_id"], f"{path}:run_id")
    nodeids_sha256 = payload["nodeids_sha256"]
    if (
        type(nodeids_sha256) is not str
        or re.fullmatch(r"[0-9a-f]{64}", nodeids_sha256) is None
    ):
        raise ContractError(f"{path}: invalid nodeids_sha256")
    started = _timestamp(payload["started_utc"], f"{path}:started_utc")
    finished = _timestamp(payload["finished_utc"], f"{path}:finished_utc")
    if finished < started:
        raise ContractError(f"{path}: finish precedes start")

    raw_outcomes = payload["outcomes"]
    if type(raw_outcomes) is not dict or set(raw_outcomes) != set(OUTCOME_KEYS):
        raise ContractError(f"{path}: incomplete outcome categories")
    got = {
        key: _uint(raw_outcomes[key], f"{path}:{key}") for key in OUTCOME_KEYS
    }
    exitstatus = _uint(payload["pytest_exitstatus"], f"{path}:exit")
    collected = _uint(payload["collected"], f"{path}:collected")
    collection_errors = _uint(
        payload["collection_errors"], f"{path}:collection_errors"
    )
    total = _uint(payload["total"], f"{path}:total")
    required = {
        "passed": group.expected,
        "failed": 0,
        "errors": 0,
        "skipped": 0,
        "xfailed": 0,
        "xpassed": 0,
    }
    if total != sum(got.values()) or collected != total:
        raise ContractError(f"{path}: collected/categorized mismatch")
    if exitstatus or collection_errors or got != required:
        raise ContractError(
            f"{path}: outcomes={got!r} exit={exitstatus} "
            f"collection_errors={collection_errors}; required={required!r}"
        )
    return got


def _expected_report_names(groups: Sequence[GroupContract]) -> set[str]:
    return {
        name
        for group in groups
        for name in (f"{group.stem}.junit.xml", f"{group.stem}.outcomes.json")
    }


def verify_results(
    results: Path,
    run_id: str,
    groups: Sequence[GroupContract] = GROUPS,
) -> dict[str, int]:
    _run_id(run_id)
    _validate_contract_definition(groups)
    try:
        info = results.lstat()
    except OSError as error:
        raise ContractError(f"missing results directory {results}: {error}") from error
    if not stat.S_ISDIR(info.st_mode):
        raise ContractError(f"not a directory: {results}")
    observed = {
        child.name
        for child in results.iterdir()
        if child.name.endswith((".junit.xml", ".outcomes.json"))
    }
    expected_reports = _expected_report_names(groups)
    if observed != expected_reports:
        missing = sorted(expected_reports - observed)
        extra = sorted(observed - expected_reports)
        raise ContractError(f"report set mismatch; missing={missing!r} extra={extra!r}")

    aggregate = {key: 0 for key in OUTCOME_KEYS}
    for group in groups:
        junit_total = _junit(results / f"{group.stem}.junit.xml", group, run_id)
        got = _outcomes(results / f"{group.stem}.outcomes.json", group, run_id)
        if junit_total != sum(got.values()):
            raise ContractError(f"{group.key}: representations disagree")
        for key in OUTCOME_KEYS:
            aggregate[key] += got[key]
        print(
            f"group={group.key} expected={group.expected} "
            f"passed={got['passed']} failed=0 errors=0 skipped=0 "
            "xfailed=0 xpassed=0"
        )
    required = {
        "passed": sum(group.expected for group in groups),
        "failed": 0,
        "errors": 0,
        "skipped": 0,
        "xfailed": 0,
        "xpassed": 0,
    }
    if aggregate != required:
        raise ContractError(f"aggregate={aggregate!r}; required={required!r}")
    print(f"contract_version={CONTRACT_VERSION}")
    print(f"contract_sha256={CONTRACT_SHA256}")
    print(f"run_id={run_id}")
    print(f"groups={len(groups)}")
    for key, value in aggregate.items():
        print(f"{key}={value}")
    return aggregate


def describe() -> None:
    validate_contracted_test_files()
    print(f"contract_version={CONTRACT_VERSION}")
    print(f"contract_sha256={CONTRACT_SHA256}")
    for group in GROUPS:
        print(
            f"group={group.key} expected_passed={group.expected} "
            f"files={len(group.files)}"
        )
        for path in group.files:
            print(f"group_file={group.key}:{path}")
    print(f"expected_total={EXPECTED_TOTAL}")
    print(
        "required_non_pass_counts="
        "failed:0,errors:0,skipped:0,xfailed:0,xpassed:0"
    )


def _absolute_external_directory(path: Path, label: str) -> Path:
    if not path.is_absolute():
        raise ContractError(f"{label} must be absolute")
    try:
        resolved = path.resolve(strict=True)
        info = resolved.lstat()
    except OSError as error:
        raise ContractError(f"{label} is unavailable: {error}") from error
    if not stat.S_ISDIR(info.st_mode):
        raise ContractError(f"{label} is not a directory")
    try:
        resolved.relative_to(SOURCE_ROOT)
    except ValueError:
        return resolved
    raise ContractError(f"{label} must be outside the source tree")


def _exec_pytest(group_key: str, run_id: str, results: Path, basetemp: Path) -> NoReturn:
    _run_id(run_id)
    try:
        group = GROUP_BY_KEY[group_key]
    except KeyError as error:
        raise ContractError(f"unknown bootstrap group: {group_key}") from error
    results = _absolute_external_directory(results, "results directory")
    basetemp_parent = _absolute_external_directory(basetemp.parent, "basetemp parent")
    basetemp = basetemp_parent / basetemp.name
    if basetemp.exists() or basetemp.is_symlink():
        raise ContractError(f"basetemp target already exists: {basetemp}")
    validate_contracted_test_files((group,))
    junit = results / f"{group.stem}.junit.xml"
    outcomes = results / f"{group.stem}.outcomes.json"
    for target in (junit, outcomes):
        if target.exists() or target.is_symlink():
            raise ContractError(f"structured target already exists: {target}")
    arguments = [
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
        f"--junitprefix={JUNIT_PREFIX}{run_id}",
        f"--phase78-bootstrap-group={group.key}",
        f"--phase78-bootstrap-run-id={run_id}",
        f"--phase78-bootstrap-outcomes={outcomes}",
        *group.files,
    ]
    os.chdir(SOURCE_ROOT)
    try:
        os.execv(sys.executable, arguments)
    except OSError as error:
        raise ContractError(f"cannot execute pytest for {group.key}: {error}") from error


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("describe")
    verify = subparsers.add_parser("verify")
    verify.add_argument("--results-dir", required=True, type=Path)
    verify.add_argument("--run-id", required=True)
    run = subparsers.add_parser("run-group")
    run.add_argument("--group", required=True)
    run.add_argument("--run-id", required=True)
    run.add_argument("--results-dir", required=True, type=Path)
    run.add_argument("--basetemp", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "describe":
            describe()
        elif args.command == "verify":
            verify_results(args.results_dir, args.run_id)
        else:
            _exec_pytest(args.group, args.run_id, args.results_dir, args.basetemp)
    except ContractError as error:
        print(f"Phase 7+8 bootstrap contract FAILED: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
