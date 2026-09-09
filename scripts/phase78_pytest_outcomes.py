"""Pytest plugin publishing exact Phase 7+8 outcome categories."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Iterator

import pytest

from scripts.phase78_test_contract import (
    CONTRACT_SHA256,
    CONTRACT_VERSION,
    GROUP_BY_KEY,
    OUTCOME_KEYS,
    OUTCOME_SCHEMA,
    RUN_ID_PATTERN,
)


_ACTIVE: dict[str, Any] | None = None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("phase78-bootstrap")
    group.addoption("--phase78-bootstrap-group", dest="phase78_bootstrap_group")
    group.addoption("--phase78-bootstrap-run-id", dest="phase78_bootstrap_run_id")
    group.addoption(
        "--phase78-bootstrap-outcomes", dest="phase78_bootstrap_outcomes"
    )


def pytest_configure(config: pytest.Config) -> None:
    global _ACTIVE
    group = config.getoption("phase78_bootstrap_group")
    run_id = config.getoption("phase78_bootstrap_run_id")
    output = config.getoption("phase78_bootstrap_outcomes")
    supplied = (bool(group), bool(run_id), bool(output))
    if len(set(supplied)) != 1:
        raise pytest.UsageError(
            "bootstrap group, run id, and outcomes path must be supplied together"
        )
    if not group:
        _ACTIVE = None
        return
    if group not in GROUP_BY_KEY:
        raise pytest.UsageError(f"unknown Phase 7+8 bootstrap group: {group}")
    if RUN_ID_PATTERN.fullmatch(run_id) is None:
        raise pytest.UsageError("invalid Phase 7+8 bootstrap run id")
    target = Path(output)
    if not target.is_absolute():
        raise pytest.UsageError("Phase 7+8 outcome target must be absolute")
    if target.exists() or target.is_symlink():
        raise pytest.UsageError(f"outcome target already exists: {target}")
    if not target.parent.is_dir():
        raise pytest.UsageError(f"outcome parent is missing: {target.parent}")
    _ACTIVE = {
        "group": group,
        "run_id": run_id,
        "target": target,
        "reports": {},
        "collection_errors": 0,
        "started_utc": _utc_now(),
    }


@pytest.fixture(scope="session", autouse=True)
def _phase78_bootstrap_junit_identity(
    request: pytest.FixtureRequest,
) -> Iterator[None]:
    state = _ACTIVE
    if state is not None:
        record = request.getfixturevalue("record_testsuite_property")
        record("phase78_contract_sha256", CONTRACT_SHA256)
        record("phase78_contract_version", CONTRACT_VERSION)
        record("phase78_group", state["group"])
        record("phase78_run_id", state["run_id"])
    yield


def pytest_collectreport(report: pytest.CollectReport) -> None:
    if _ACTIVE is not None and report.failed:
        _ACTIVE["collection_errors"] += 1


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    if _ACTIVE is not None:
        _ACTIVE["reports"].setdefault(report.nodeid, []).append(report)


def _category(reports: list[pytest.TestReport]) -> str:
    if any(report.failed for report in reports):
        failed_reports = [report for report in reports if report.failed]
        if any(getattr(report, "wasxfail", None) for report in failed_reports):
            return "xpassed"
        if any(report.when != "call" for report in failed_reports):
            return "errors"
        return "failed"
    if any(
        report.skipped and getattr(report, "wasxfail", None) for report in reports
    ):
        return "xfailed"
    call = next((report for report in reports if report.when == "call"), None)
    if call is not None:
        if call.skipped:
            return "xfailed" if getattr(call, "wasxfail", None) else "skipped"
        if call.passed:
            return "xpassed" if getattr(call, "wasxfail", None) else "passed"
    if any(report.skipped for report in reports):
        return "skipped"
    return "failed"


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    state = _ACTIVE
    if state is None:
        return
    outcomes = {key: 0 for key in OUTCOME_KEYS}
    for reports in state["reports"].values():
        outcomes[_category(reports)] += 1
    nodeids = sorted(state["reports"])
    nodeids_sha256 = hashlib.sha256(
        "".join(f"{nodeid}\n" for nodeid in nodeids).encode("utf-8")
    ).hexdigest()
    payload = {
        "schema": OUTCOME_SCHEMA,
        "contract_version": CONTRACT_VERSION,
        "contract_sha256": CONTRACT_SHA256,
        "run_id": state["run_id"],
        "group": state["group"],
        "pytest_exitstatus": int(exitstatus),
        "collected": int(session.testscollected),
        "collection_errors": int(state["collection_errors"]),
        "outcomes": outcomes,
        "total": sum(outcomes.values()),
        "nodeids_sha256": nodeids_sha256,
        "started_utc": state["started_utc"],
        "finished_utc": _utc_now(),
    }
    target = state["target"]
    temporary = target.with_name(target.name + f".tmp-{os.getpid()}")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(
                payload,
                stream,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        directory_fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def pytest_unconfigure(config: pytest.Config) -> None:
    global _ACTIVE
    _ACTIVE = None
