"""Explicit pytest plugin publishing exact Phase4-6 outcome categories."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from scripts.bootstrap_test_contract import (
    CONTRACT_VERSION,
    GROUP_BY_KEY,
    OUTCOME_KEYS,
    OUTCOME_SCHEMA,
)


_ACTIVE: dict[str, Any] | None = None


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("phase46-bootstrap")
    group.addoption("--phase46-bootstrap-group", dest="phase46_bootstrap_group")
    group.addoption(
        "--phase46-bootstrap-outcomes", dest="phase46_bootstrap_outcomes"
    )


def pytest_configure(config: pytest.Config) -> None:
    global _ACTIVE
    group = config.getoption("phase46_bootstrap_group")
    output = config.getoption("phase46_bootstrap_outcomes")
    if bool(group) != bool(output):
        raise pytest.UsageError(
            "bootstrap group and outcomes path must be supplied together"
        )
    if not group:
        _ACTIVE = None
        return
    if group not in GROUP_BY_KEY:
        raise pytest.UsageError(f"unknown bootstrap group: {group}")
    target = Path(output)
    if target.exists() or target.is_symlink():
        raise pytest.UsageError(f"outcome target already exists: {target}")
    if not target.parent.is_dir():
        raise pytest.UsageError(f"outcome parent is missing: {target.parent}")
    _ACTIVE = {
        "group": group,
        "target": target,
        "reports": {},
        "collection_errors": 0,
    }


def pytest_collectreport(report: pytest.CollectReport) -> None:
    if _ACTIVE is not None and report.failed:
        _ACTIVE["collection_errors"] += 1


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    if _ACTIVE is not None:
        _ACTIVE["reports"].setdefault(report.nodeid, []).append(report)


def _category(reports: list[pytest.TestReport]) -> str:
    if any(report.failed for report in reports):
        return (
            "xpassed"
            if any(
                getattr(report, "wasxfail", None)
                for report in reports
                if report.failed
            )
            else "failed"
        )
    # pytest can resolve an xfail from a fixture/setup hook before producing a
    # call report.  Check explicit skipped-xfail reports across every stage
    # before a successful call report can classify the item as a pass.
    if any(
        report.skipped and getattr(report, "wasxfail", None)
        for report in reports
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
    payload = {
        "schema": OUTCOME_SCHEMA,
        "contract_version": CONTRACT_VERSION,
        "group": state["group"],
        "pytest_exitstatus": int(exitstatus),
        "collected": int(session.testscollected),
        "collection_errors": int(state["collection_errors"]),
        "outcomes": outcomes,
        "total": sum(outcomes.values()),
    }
    target = state["target"]
    temporary = target.with_name(target.name + f".tmp-{os.getpid()}")
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


def pytest_unconfigure(config: pytest.Config) -> None:
    global _ACTIVE
    _ACTIVE = None
