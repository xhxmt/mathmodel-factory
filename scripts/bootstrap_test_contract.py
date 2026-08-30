"""Fail-closed structured result gate for the Phase 3-6 bootstrap."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import math
from pathlib import Path
import stat
import sys
from typing import Any, Sequence
import xml.etree.ElementTree as ET


CONTRACT_VERSION = "phase46-bootstrap-exact-count-v1"
OUTCOME_SCHEMA = "phase46-pytest-outcomes-v1"
OUTCOME_KEYS = ("passed", "failed", "skipped", "xfailed", "xpassed")


@dataclass(frozen=True, slots=True)
class GroupContract:
    key: str
    stem: str
    expected: int


GROUPS = (
    GroupContract("phase3", "pytest-phase3", 100),
    GroupContract("phase45", "pytest-phase45", 274),
    GroupContract("phase6-core", "pytest-phase6-core", 146),
    GroupContract("phase6-web", "pytest-phase6-web", 29),
    GroupContract("payload-policy", "pytest-payload-policy", 108),
)
GROUP_BY_KEY = {group.key: group for group in GROUPS}
EXPECTED_TOTAL = sum(group.expected for group in GROUPS)


class ContractError(ValueError):
    """A structured bootstrap result is missing, ambiguous, or non-exact."""


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


def _junit(path: Path, group: GroupContract) -> int:
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
    seen: set[tuple[str, str]] = set()
    for case in cases:
        identity = (case.get("classname", ""), case.get("name", ""))
        if not all(identity) or identity in seen:
            raise ContractError(f"{path}: missing/duplicate testcase {identity!r}")
        seen.add(identity)
        # A clean bootstrap result has no per-test status or output children.
        # Reject every child, rather than only known failure tags, so an
        # unknown or nested extension cannot hide a non-pass from this exact
        # count gate.
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


def _outcomes(path: Path, group: GroupContract) -> dict[str, int]:
    try:
        payload = json.loads(
            _read(path, 2 * 1024 * 1024).decode("utf-8", errors="strict"),
            object_pairs_hook=_no_duplicate_json_keys,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ContractError(f"{path}: malformed outcome JSON: {error}") from error
    keys = {
        "schema",
        "contract_version",
        "group",
        "pytest_exitstatus",
        "collected",
        "collection_errors",
        "outcomes",
        "total",
    }
    if type(payload) is not dict or set(payload) != keys:
        raise ContractError(f"{path}: wrong outcome schema")
    identity = (payload["schema"], payload["contract_version"], payload["group"])
    if identity != (OUTCOME_SCHEMA, CONTRACT_VERSION, group.key):
        raise ContractError(f"{path}: outcome identity mismatch")
    raw_outcomes = payload["outcomes"]
    if type(raw_outcomes) is not dict or set(raw_outcomes) != set(OUTCOME_KEYS):
        raise ContractError(f"{path}: incomplete outcome categories")
    got = {
        key: _uint(raw_outcomes[key], f"{path}:{key}")
        for key in OUTCOME_KEYS
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


def verify_results(
    results: Path, groups: Sequence[GroupContract] = GROUPS
) -> dict[str, int]:
    try:
        info = results.lstat()
    except OSError as error:
        raise ContractError(f"missing results directory {results}: {error}") from error
    if not stat.S_ISDIR(info.st_mode):
        raise ContractError(f"not a directory: {results}")
    aggregate = {key: 0 for key in OUTCOME_KEYS}
    seen: set[tuple[str, str]] = set()
    for group in groups:
        identity = (group.key, group.stem)
        if identity in seen:
            raise ContractError("duplicate contract group")
        seen.add(identity)
        junit_total = _junit(results / f"{group.stem}.junit.xml", group)
        got = _outcomes(results / f"{group.stem}.outcomes.json", group)
        if junit_total != sum(got.values()):
            raise ContractError(f"{group.key}: representations disagree")
        for key in OUTCOME_KEYS:
            aggregate[key] += got[key]
        print(
            f"group={group.key} expected={group.expected} "
            f"passed={got['passed']} failed=0 skipped=0 xfailed=0 xpassed=0"
        )
    expected = sum(group.expected for group in groups)
    required = {
        "passed": expected,
        "failed": 0,
        "skipped": 0,
        "xfailed": 0,
        "xpassed": 0,
    }
    if aggregate != required:
        raise ContractError(f"aggregate={aggregate!r}; required={required!r}")
    print(f"contract_version={CONTRACT_VERSION}")
    print(f"groups={len(groups)}")
    for key, value in aggregate.items():
        print(f"{key}={value}")
    return aggregate


def describe() -> None:
    print(f"contract_version={CONTRACT_VERSION}")
    for group in GROUPS:
        print(f"group={group.key} expected_passed={group.expected}")
    print(f"expected_total={EXPECTED_TOTAL}")
    print("required_non_pass_counts=failed:0,skipped:0,xfailed:0,xpassed:0")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("describe")
    verify = subparsers.add_parser("verify")
    verify.add_argument("--results-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "describe":
            describe()
        else:
            verify_results(args.results_dir)
    except ContractError as error:
        print(f"bootstrap count contract FAILED: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
