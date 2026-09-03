"""Typed, source-bound evidence for the nine Phase9 P0 acceptance gates.

The formal producer in this module is intentionally narrow: it derives the
candidate from a clean Git checkout, runs one fixed pytest command, retains the
complete output and JUnit report, and emits a closed evidence tree.  The
validator accepts only that formal domain.  Test-fixture receipts use a
different domain and can never satisfy the Phase9 entry gate.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import shutil
import sqlite3
import stat
import subprocess
import sys
import time
import tomllib
from typing import Mapping, Sequence
import xml.etree.ElementTree as ET

from .canonical import canonical_bytes, canonical_sha256
from .phase9_run_generation import (
    EXECUTION_CONTEXT_EVIDENCE_SCHEMA,
    GIT_TRACKED_SOURCE_ENTRY_SCHEMA,
    GIT_TRACKED_SOURCE_INVENTORY_SCHEMA,
    read_current_git_source_snapshot,
)
from tools.trusted_pytest_reporter import (
    TRUSTED_PYTEST_EVENT_SCHEMA,
    validate_trusted_pytest_events,
)


PHASE9_P0_RECEIPT_SCHEMA = "phase9-candidate-p0-receipt-v5"
PHASE9_P0_COMMAND_RECORD_SCHEMA = "phase9-p0-command-record-v4"
PHASE9_P0_TEST_OUTCOME_SCHEMA = "phase9-p0-test-outcome-v3"
PHASE9_P0_SPEC_SCHEMA = "phase9-p0-acceptance-spec-v3"
PHASE9_P0_SOURCE_ATTESTATION_SCHEMA = "phase9-p0-source-attestation-v1"
PHASE9_P0_ENVIRONMENT_SCHEMA = "phase9-p0-runner-environment-v3"
PHASE9_P0_EVIDENCE_ROOT_SCHEMA = "phase9-p0-evidence-root-v4"
PHASE9_P0_AUTHORITY_RUNNER_EVIDENCE_SCHEMA = (
    "authority-phase9-p0-runner-consumption-evidence-v2"
)
PHASE9_P0_RUNNER_AUTHORIZATION_SCHEMA = (
    "authority-phase9-p0-runner-authorization-v2"
)
PHASE9_P0_RUNNER_CONSUMPTION_SCHEMA = (
    "authority-phase9-p0-runner-authorization-consumption-v1"
)
PHASE9_P0_RUNNER_ATTESTATION_SCHEMA = (
    "authority-phase9-p0-runner-execution-attestation-v2"
)
PHASE9_P0_FORMAL_DOMAIN = "FORMAL_CANDIDATE_ACCEPTANCE"
PHASE9_P0_TEST_FIXTURE_DOMAIN = "TEST_FIXTURE"
PHASE9_P0_SUITE_ID = "phase9-p0-fixed-acceptance-v1"
PHASE9_P0_RUNNER_AUTHORIZATION_TTL_SECONDS = 300
PHASE9_P0_PRODUCER_TYPE = "AUTHORITY_DB_BACKED_BWRAP_PYTEST"
PHASE9_P0_PRODUCER_VERSION = "1"
PHASE9_P0_PRODUCER_SOURCE_PATH = "factory_core/phase9_p0_evidence.py"
PHASE9_P0_TRUSTED_REPORTER_SOURCE_PATH = "tools/trusted_pytest_reporter.py"
PHASE9_P0_EXECUTION_CONTEXT_BINDING_SCHEMA = (
    "phase9-p0-execution-context-runtime-binding-v1"
)
PHASE9_P0_RUNTIME_ENVIRONMENT_DESCRIPTOR_SCHEMA = (
    "phase9-p0-runtime-environment-descriptor-v1"
)
PHASE9_P0_LAUNCHER_DESCRIPTOR_SCHEMA = "phase9-p0-launcher-descriptor-v1"
P0_REQUIREMENTS = (
    "AR_007_DELIVERY_BYPASS",
    "HUMAN_DECISION_SINGLE_WRITER",
    "PACKET_ZERO_DISPATCH_EFFECTIVE_VERDICT",
    "COMMAND_READ_SET_CAS",
    "WORKER_OUTBOX_PROCESS_TREE_RECEIPTS",
    "OWNER_CHECKPOINT_REATTEST",
    "REVISION_ATOMIC_SNAPSHOT",
    "RUN_MODE_GENERATION_DELIVERY_PINS",
    "OFFICIAL_INPUT_EXECUTION_CONTEXT",
)

# This is the reviewed command surface.  A caller can select neither another
# file nor another pytest node while asking for a FORMAL receipt.
PHASE9_P0_TEST_NODES: Mapping[str, tuple[str, ...]] = {
    "AR_007_DELIVERY_BYPASS": (
        "tests/test_phase9_delivery_fence.py::test_phase9_modes_capability_and_override_can_never_authorize_delivery[FORENSIC_REPLAY-LEGACY_NOT_APPLICABLE-DISABLED-TECHNICAL]",
        "tests/test_phase9_delivery_fence.py::test_phase9_modes_capability_and_override_can_never_authorize_delivery[FORENSIC_REPLAY-LEGACY_NOT_APPLICABLE-DISABLED-ABLATE_NO_JUDGE]",
        "tests/test_phase9_delivery_fence.py::test_phase9_modes_capability_and_override_can_never_authorize_delivery[NORMAL_DELIVERY_RUN-ACTIVE-ENABLED-TECHNICAL]",
        "tests/test_phase9_delivery_fence.py::test_phase9_modes_capability_and_override_can_never_authorize_delivery[FORENSIC_REPLAY-LEGACY_NOT_APPLICABLE-ENABLED-TECHNICAL]",
    ),
    "HUMAN_DECISION_SINGLE_WRITER": (
        "tests/test_atomic_release.py::test_no_judge_ablation_cannot_replace_current_release",
    ),
    "PACKET_ZERO_DISPATCH_EFFECTIVE_VERDICT": (
        "tests/test_phase9_p0_evidence.py::test_p0_probe_effective_verdict_is_fail_closed",
        "tests/test_phase9_acceptance_probes.py::test_ac_packet_002",
    ),
    "COMMAND_READ_SET_CAS": (
        "tests/test_phase9_run_generation.py::test_same_key_different_canonical_request_conflicts_without_mutation",
    ),
    "WORKER_OUTBOX_PROCESS_TREE_RECEIPTS": (
        "tests/test_authority_outbox_delivery.py::test_claim_crash_window_moves_to_reconciliation_not_blind_resend",
    ),
    "OWNER_CHECKPOINT_REATTEST": (
        "tests/test_phase78_work_ledger.py::test_normal_submit_claim_checkpoint_complete_replay_and_restart",
    ),
    "REVISION_ATOMIC_SNAPSHOT": (
        "tests/test_phase6_project_snapshot_ui.py::test_mixed_section_coordinate_fails_closed_and_suppresses_sections",
    ),
    "RUN_MODE_GENERATION_DELIVERY_PINS": (
        "tests/test_phase9_run_generation.py::test_default_off_fence_rejects_enabled_writer",
    ),
    "OFFICIAL_INPUT_EXECUTION_CONTEXT": (
        "tests/test_phase9_run_generation.py::test_create_reads_real_official_bytes_and_rejects_wrong_or_extra_files",
    ),
}

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_GIT_OID = re.compile(r"[0-9a-f]{40}\Z")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,191}\Z")
_TEST_LINE = re.compile(
    r"^(tests/[^\s]+::[^\s]+)\s+(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)(?:\s|$)"
)
_COLLECTED_LINE = re.compile(r"(?:^|.*\s)collected ([0-9]+) items?$")
_SUMMARY_LINE = re.compile(r"^=+\s+(.+?)\s+in [0-9]+(?:\.[0-9]+)?s\s+=+$")
_SUMMARY_PART = re.compile(
    r"([0-9]+) (passed|failed|error|errors|skipped|xfailed|xpassed|warning|warnings)"
)
_CAPABILITIES = {
    "network_access": False,
    "provider_call": False,
    "outbox_dispatch": False,
    "delivery": False,
    "release": False,
    "migration": False,
    "deployment": False,
    "cutover": False,
}
_OUTCOME_KEYS = (
    "collected",
    "passed",
    "failed",
    "errors",
    "skipped",
    "xfailed",
    "xpassed",
    "warnings",
)
_BLOCKED_ENVIRONMENT_NAMES = frozenset(
    {
        "PHASE78_ENABLED",
        "PHASE9_ENABLED",
        "DATABASE_URL",
        "AUTHORITY_DATABASE",
        "AUTHORITY_DB",
        "PRODUCTION_DATABASE",
        "PRODUCTION_DB",
        "PROVIDER_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "SOLVER_API_KEY",
        "CLOUD_SOLVER_URL",
        "DEPLOYMENT_ENV",
        "PRODUCTION_RELEASE",
        "PRODUCTION_OUTBOX",
    }
)
_BLOCKED_ENVIRONMENT_FRAGMENTS = (
    "API_KEY", "CREDENTIAL", "DATABASE", "DEPLOY", "OUTBOX", "PASSWORD",
    "PROVIDER", "RELEASE", "SECRET", "SOLVER", "TOKEN",
)


class Phase9P0EvidenceError(RuntimeError):
    """The formal P0 run or its evidence is not independently valid."""


@dataclass(frozen=True)
class Phase9P0EvidenceBundle:
    evidence_root: Path
    evidence_root_sha256: str
    candidate: dict[str, str]
    coordinate: dict[str, str]
    source_inventory_sha256: str
    receipt_paths: dict[str, Path]
    receipts: dict[str, dict[str, object]]
    authority_attestation_sha256: str


@dataclass(frozen=True)
class ValidatedPhase9P0Evidence:
    receipt_sha256s: dict[str, str]
    authority_runner: dict[str, object]
    consumption_receipt_sha256: str
    authority_runner_evidence_sha256: str
    command_record_sha256: str
    raw_log_byte_length: int
    raw_log_sha256: str
    junit_byte_length: int
    junit_sha256: str
    outcome_sha256: str
    started_at: int
    finished_at: int

    @property
    def receipt_set_sha256(self) -> str:
        return canonical_sha256(self.receipt_sha256s)


def _text(value: object, path: str, *, identifier: bool = False) -> str:
    if type(value) is not str or not value:
        raise Phase9P0EvidenceError(f"{path} must be a non-empty plain string")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise Phase9P0EvidenceError(f"{path} must be valid UTF-8") from exc
    if identifier and _IDENTIFIER.fullmatch(value) is None:
        raise Phase9P0EvidenceError(f"{path} must be a bounded identifier")
    return value


def _sha(value: object, path: str) -> str:
    result = _text(value, path)
    if _SHA256.fullmatch(result) is None:
        raise Phase9P0EvidenceError(f"{path} must be lowercase SHA-256")
    return result


def _git_oid(value: object, path: str) -> str:
    result = _text(value, path)
    if _GIT_OID.fullmatch(result) is None:
        raise Phase9P0EvidenceError(f"{path} must be a lowercase Git object id")
    return result


def _integer(value: object, path: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise Phase9P0EvidenceError(f"{path} must be an integer >= {minimum}")
    return value


def _validate_capabilities(value: object, path: str) -> None:
    item = _mapping(value, path, set(_CAPABILITIES))
    if any(type(item[name]) is not bool or item[name] is not expected for name, expected in _CAPABILITIES.items()):
        raise Phase9P0EvidenceError(f"{path} must contain the exact boolean capability fence")


def _mapping(value: object, path: str, keys: set[str]) -> Mapping[str, object]:
    if type(value) is not dict:
        raise Phase9P0EvidenceError(f"{path} must be a plain object")
    if set(value) != keys:
        raise Phase9P0EvidenceError(
            f"{path} keys differ: missing={sorted(keys-set(value))!r} "
            f"extra={sorted(set(value)-keys)!r}"
        )
    return value


def _canonical_json(raw: bytes, path: str) -> dict[str, object]:
    import json

    try:
        value = json.loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise Phase9P0EvidenceError(f"{path} is not strict UTF-8 JSON") from exc
    if type(value) is not dict or canonical_bytes(value) != raw:
        raise Phase9P0EvidenceError(f"{path} is not canonical JSON")
    return value


def _self_hash(value: Mapping[str, object], field: str, path: str) -> str:
    claimed = _sha(value.get(field), f"{path}.{field}")
    body = dict(value)
    body.pop(field, None)
    if canonical_sha256(body) != claimed:
        raise Phase9P0EvidenceError(f"{path} self-hash differs")
    return claimed


def phase9_p0_acceptance_spec() -> dict[str, object]:
    body: dict[str, object] = {
        "schema": PHASE9_P0_SPEC_SCHEMA,
        "evidence_domain": PHASE9_P0_FORMAL_DOMAIN,
        "suite_id": PHASE9_P0_SUITE_ID,
        "runner": "tools/run_phase9_p0_evidence.py",
        "authority_runner_schema": PHASE9_P0_AUTHORITY_RUNNER_EVIDENCE_SCHEMA,
        "authority_attestation_required": True,
        "producer": {
            "producer_type": PHASE9_P0_PRODUCER_TYPE,
            "producer_version": PHASE9_P0_PRODUCER_VERSION,
            "source_path": PHASE9_P0_PRODUCER_SOURCE_PATH,
            "trusted_reporter_source_path": PHASE9_P0_TRUSTED_REPORTER_SOURCE_PATH,
            "trusted_reporter_schema": TRUSTED_PYTEST_EVENT_SCHEMA,
        },
        "sandbox_contract": {
            "binary": "/usr/bin/bwrap",
            "network_namespace": "UNSHARED",
            "host_root": "READ_ONLY",
            "candidate_source": "READ_ONLY",
            "authority_database": "READ_ONLY",
            "python_site_initialization": "DISABLED",
            "candidate_pytest_configuration": "DISABLED",
            "launcher": "EXTERNAL_TRACKED_TRUSTED_PYTEST_REPORTER",
            "interpreter_flags": ["-I", "-S", "-B"],
        },
        "requirements": [
            {"requirement": name, "test_nodes": list(PHASE9_P0_TEST_NODES[name])}
            for name in P0_REQUIREMENTS
        ],
    }
    body["spec_sha256"] = canonical_sha256(body)
    return body


def phase9_p0_spec_sha256() -> str:
    return str(phase9_p0_acceptance_spec()["spec_sha256"])


def phase9_p0_test_nodes() -> tuple[str, ...]:
    return tuple(
        node for requirement in P0_REQUIREMENTS
        for node in PHASE9_P0_TEST_NODES[requirement]
    )


def formal_p0_paths() -> frozenset[str]:
    return frozenset(
        {
            "attestations/authority_runner.json",
            "attestations/environment.json",
            "attestations/p0_spec.json",
            "attestations/source_inventory.json",
            "attestations/trusted_pytest_reporter.py",
            "command_records/p0_suite.json",
            "test_results/p0_suite.log",
            "test_reports/p0_suite.xml",
            "test_events/p0_suite.jsonl",
            "test_outcomes/p0_suite.json",
        }
        | {f"receipts/{name}.json" for name in P0_REQUIREMENTS}
    )


def _expected_directories(paths: Sequence[str]) -> list[str]:
    result: set[str] = set()
    for value in paths:
        parent = Path(value).parent
        while parent != Path("."):
            result.add(parent.as_posix())
            parent = parent.parent
    return sorted(result, key=lambda item: item.encode("utf-8"))


def evidence_root_sha256_from_files(files: Mapping[str, bytes]) -> str:
    paths = sorted(files, key=lambda item: item.encode("utf-8"))
    inventory = {
        "schema": PHASE9_P0_EVIDENCE_ROOT_SCHEMA,
        "directories": _expected_directories(paths),
        "files": [
            {
                "path": path,
                "byte_length": len(files[path]),
                "raw_bytes_sha256": hashlib.sha256(files[path]).hexdigest(),
            }
            for path in paths
        ],
    }
    return canonical_sha256(inventory)


def _pytest_outcomes(raw: bytes, expected_nodes: Sequence[str]) -> dict[str, object]:
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise Phase9P0EvidenceError("raw pytest log is not UTF-8") from exc
    if not text or not text.endswith("\n"):
        raise Phase9P0EvidenceError("raw pytest log is empty or truncated")
    if "\x1b" in text:
        raise Phase9P0EvidenceError("raw pytest log contains terminal control bytes")
    collected: int | None = None
    summary: dict[str, int] | None = None
    summary_line_number: int | None = None
    observed: dict[str, str] = {}
    lines = text.splitlines()
    for line_number, raw_line in enumerate(lines):
        line = raw_line.strip()
        match = _COLLECTED_LINE.fullmatch(line)
        if match:
            if collected is not None:
                raise Phase9P0EvidenceError("pytest log has duplicate collection summaries")
            collected = int(match.group(1))
        match = _TEST_LINE.match(line)
        if match:
            node, status = match.groups()
            if node in observed:
                raise Phase9P0EvidenceError("pytest log repeats one test outcome")
            observed[node] = status
        match = _SUMMARY_LINE.fullmatch(line)
        if match:
            if summary is not None:
                raise Phase9P0EvidenceError("pytest log has duplicate terminal summaries")
            counts = {name: 0 for name in _OUTCOME_KEYS if name != "collected"}
            for count, label in _SUMMARY_PART.findall(match.group(1)):
                key = "errors" if label in {"error", "errors"} else (
                    "warnings" if label in {"warning", "warnings"} else label
                )
                counts[key] += int(count)
            if any(counts.values()):
                summary = counts
                summary_line_number = line_number
    expected = list(expected_nodes)
    if collected != len(expected) or list(observed) != expected:
        raise Phase9P0EvidenceError(
            "pytest log collection or exact ordered test-node inventory differs"
        )
    if any(value != "PASSED" for value in observed.values()):
        raise Phase9P0EvidenceError("pytest log contains a non-PASS test outcome")
    expected_summary = {
        "passed": len(expected), "failed": 0, "errors": 0, "skipped": 0,
        "xfailed": 0, "xpassed": 0, "warnings": 0,
    }
    if summary != expected_summary:
        raise Phase9P0EvidenceError("pytest terminal summary is absent or non-PASS")
    if summary_line_number != max(
        index for index, line in enumerate(lines) if line.strip()
    ):
        raise Phase9P0EvidenceError("pytest terminal summary is not the final log record")
    return {
        "collected": len(expected),
        **expected_summary,
        "node_outcomes": [
            {"test_node": node, "outcome": observed[node]} for node in expected
        ],
    }


def _junit_case_for_node(node: str) -> tuple[str, str]:
    parts = node.split("::")
    if len(parts) < 2 or not parts[0].endswith(".py"):
        raise Phase9P0EvidenceError("fixed pytest node cannot map to JUnit identity")
    module = parts[0][:-3].replace("/", ".")
    classname = ".".join((module, *parts[1:-1]))
    return classname, parts[-1]


def _validate_junit(raw: bytes, expected_nodes: Sequence[str]) -> None:
    if not raw or b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise Phase9P0EvidenceError("JUnit evidence is empty or contains a DTD/entity")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise Phase9P0EvidenceError("JUnit evidence is malformed XML") from exc
    suites = root.findall(".//testsuite") if root.tag != "testsuite" else [root]
    if len(suites) != 1:
        raise Phase9P0EvidenceError("JUnit evidence must contain exactly one suite")
    suite = suites[0]
    expected = [_junit_case_for_node(node) for node in expected_nodes]
    cases = suite.findall("testcase")
    observed = [(case.get("classname"), case.get("name")) for case in cases]
    if observed != expected:
        raise Phase9P0EvidenceError(
            "JUnit exact ordered test-node inventory differs"
        )
    numeric = {name: suite.get(name) for name in ("tests", "errors", "failures", "skipped")}
    if numeric != {
        "tests": str(len(expected)),
        "errors": "0",
        "failures": "0",
        "skipped": "0",
    }:
        raise Phase9P0EvidenceError("JUnit suite outcome totals differ")
    if any(
        case.find(kind) is not None
        for case in cases for kind in ("failure", "error", "skipped")
    ):
        raise Phase9P0EvidenceError("JUnit evidence contains a non-PASS result")


def _file_reference(path: str, raw: bytes, *, kind: str | None = None) -> dict[str, object]:
    result: dict[str, object] = {
        "path": path,
        "byte_length": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }
    if kind is not None:
        result["kind"] = kind
    return result


def _write_new(path: Path, raw: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(raw)


def _python_identity(path_value: str | Path) -> dict[str, object]:
    requested = Path(os.path.abspath(os.fspath(path_value)))
    if not requested.is_absolute():
        raise Phase9P0EvidenceError("python executable must be absolute")
    try:
        resolved = requested.resolve(strict=True)
        before = resolved.lstat()
    except OSError as exc:
        raise Phase9P0EvidenceError(
            "python executable is missing or cannot be resolved safely"
        ) from exc
    if not stat.S_ISREG(before.st_mode) or not os.access(resolved, os.X_OK):
        raise Phase9P0EvidenceError("python executable must resolve to an executable file")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(resolved, flags)
        with os.fdopen(descriptor, "rb") as stream:
            opened = os.fstat(stream.fileno())
            raw = stream.read()
            after = os.fstat(stream.fileno())
    except OSError as exc:
        raise Phase9P0EvidenceError("python executable cannot be read safely") from exc
    identity = lambda item: (
        item.st_dev, item.st_ino, item.st_mode, item.st_nlink,
        item.st_size, item.st_mtime_ns,
    )
    if identity(before) != identity(opened) or identity(opened) != identity(after):
        raise Phase9P0EvidenceError("python executable changed while being read")
    return {
        "requested_path": str(requested),
        "resolved_path": str(resolved),
        "byte_length": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def _external_git_object_mount(source_root: Path) -> dict[str, str] | None:
    """Return the read-only common-dir needed by a linked worktree sandbox."""

    try:
        completed = subprocess.run(
            [
                "git", "-c", "core.hooksPath=/dev/null",
                "-c", "core.fsmonitor=false", "-c", "submodule.recurse=false",
                "rev-parse", "--path-format=absolute", "--git-common-dir",
            ],
            cwd=source_root,
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            env={
                "PATH": "/usr/bin:/bin", "LC_ALL": "C",
                "GIT_OPTIONAL_LOCKS": "0", "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": "/dev/null",
            },
        )
        common = Path(completed.stdout.decode("utf-8", errors="strict").strip())
        common = common.resolve(strict=True)
        metadata = common.lstat()
    except (OSError, UnicodeError, subprocess.SubprocessError) as exc:
        raise Phase9P0EvidenceError(
            "candidate Git common directory cannot be resolved"
        ) from exc
    if not common.is_absolute() or not stat.S_ISDIR(metadata.st_mode) or common.is_symlink():
        raise Phase9P0EvidenceError("candidate Git common directory is not ordinary")
    try:
        common.relative_to(source_root)
        return None
    except ValueError:
        return {"path": str(common), "access": "READ_ONLY"}


def _stable_runtime_file(path: Path, label: str) -> bytes:
    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode):
            raise Phase9P0EvidenceError(f"{label} is not a regular file")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            opened = os.fstat(descriptor)
            chunks: list[bytes] = []
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        final = path.lstat()
    except OSError as exc:
        raise Phase9P0EvidenceError(f"{label} cannot be read stably") from exc
    identity = lambda value: (
        value.st_dev, value.st_ino, value.st_mode, value.st_nlink,
        value.st_size, value.st_mtime_ns, value.st_ctime_ns,
    )
    if not (
        identity(before) == identity(opened) == identity(after) == identity(final)
    ):
        raise Phase9P0EvidenceError(f"{label} changed while read")
    return b"".join(chunks)


def _loaded_p0_source_binding(snapshot: object) -> dict[str, object]:
    """Prove already-imported formal producer modules equal candidate bytes."""

    package = sys.modules.get("factory_core")
    package_file = getattr(package, "__file__", None)
    package_paths = getattr(package, "__path__", None)
    if type(package_file) is not str or package_paths is None:
        raise Phase9P0EvidenceError("loaded formal producer package origin is unavailable")
    try:
        loaded_root = Path(__file__).resolve(strict=True).parent.parent
        expected_package = loaded_root / "factory_core"
        if (
            Path(package_file).resolve(strict=True) != expected_package / "__init__.py"
            or tuple(Path(item).resolve(strict=True) for item in package_paths)
            != (expected_package,)
        ):
            raise Phase9P0EvidenceError("loaded formal producer package roots differ")
    except OSError as exc:
        raise Phase9P0EvidenceError(
            "loaded formal producer package cannot be resolved"
        ) from exc
    expected = {
        item.logical_path: item
        for item in snapshot.tracked_inventory.entries
        if item.git_mode != "160000"
    }
    records: list[dict[str, object]] = []
    bound_modules = (
        "factory_core",
        "factory_core.canonical",
        "factory_core.phase9_run_generation",
        "factory_core.phase9_p0_evidence",
    )
    for name in bound_modules:
        module = sys.modules.get(name)
        if module is None:
            raise Phase9P0EvidenceError(
                f"loaded formal producer module is unavailable: {name}"
            )
        module_file = getattr(module, "__file__", None)
        if module_file is None:
            continue
        try:
            path = Path(str(module_file)).resolve(strict=True)
            logical = path.relative_to(loaded_root).as_posix()
        except (OSError, ValueError) as exc:
            raise Phase9P0EvidenceError(
                "loaded formal producer module escapes its package root"
            ) from exc
        entry = expected.get(logical)
        raw = _stable_runtime_file(path, f"loaded formal producer module {name}")
        if (
            entry is None
            or entry.raw_bytes_sha256 is None
            or entry.byte_length != len(raw)
            or hashlib.sha256(raw).hexdigest() != entry.raw_bytes_sha256
        ):
            raise Phase9P0EvidenceError(
                f"loaded formal producer differs from candidate Git bytes: {logical}"
            )
        records.append(
            {
                "module": name,
                "logical_path": logical,
                "byte_length": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
        )
    required = {
        "factory_core/__init__.py",
        "factory_core/canonical.py",
        "factory_core/phase9_p0_evidence.py",
        "factory_core/phase9_run_generation.py",
    }
    if not required.issubset({str(item["logical_path"]) for item in records}):
        raise Phase9P0EvidenceError("loaded formal producer module closure is incomplete")
    inventory = {
        "schema": "phase9-p0-loaded-producer-inventory-v1",
        "modules": records,
    }
    return {
        "loaded_source_root": str(loaded_root),
        "loaded_source_inventory_sha256": canonical_sha256(inventory),
    }


def _trusted_python_runtime_identity(
    path_value: str | Path, source_root: Path
) -> dict[str, object]:
    """Bind the one authorized runner runtime and its pytest/pluggy code bytes."""

    requested = Path(os.path.abspath(os.fspath(path_value)))
    trusted_requested = Path(os.path.abspath(sys.executable))
    try:
        requested_resolved = requested.resolve(strict=True)
        trusted_resolved = trusted_requested.resolve(strict=True)
    except OSError as exc:
        raise Phase9P0EvidenceError("trusted Python runtime cannot be resolved") from exc
    if requested != trusted_requested or requested_resolved != trusted_resolved:
        raise Phase9P0EvidenceError(
            "formal P0 execution must use the current trusted producer Python"
        )
    try:
        lock_raw = _stable_runtime_file(source_root / "uv.lock", "candidate uv.lock")
        lock = tomllib.loads(lock_raw.decode("utf-8", errors="strict"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise Phase9P0EvidenceError("candidate uv.lock cannot bind pytest runtime") from exc
    packages = lock.get("package")
    if type(packages) is not list:
        raise Phase9P0EvidenceError("candidate uv.lock package graph differs")
    locked_versions: dict[str, str] = {}
    for name in ("pytest", "pluggy"):
        matches = [
            item.get("version") for item in packages
            if type(item) is dict and item.get("name") == name
        ]
        if len(matches) != 1 or type(matches[0]) is not str:
            raise Phase9P0EvidenceError(f"candidate uv.lock does not pin {name}")
        locked_versions[name] = str(matches[0])

    import _pytest
    import pluggy
    import pytest

    actual_versions = {"pytest": pytest.__version__, "pluggy": pluggy.__version__}
    if actual_versions != locked_versions:
        raise Phase9P0EvidenceError("trusted pytest runtime differs from uv.lock")
    prefix = Path(sys.prefix).resolve(strict=True)
    site_packages = Path(str(pytest.__file__)).resolve(strict=True).parent.parent
    records: list[dict[str, object]] = []
    directory_identities: dict[Path, tuple[int, ...]] = {}
    dir_identity = lambda value: (
        value.st_dev, value.st_ino, value.st_mode, value.st_nlink,
        value.st_mtime_ns, value.st_ctime_ns,
    )
    def traversal_error(error: OSError) -> None:
        raise Phase9P0EvidenceError("trusted runtime cannot be enumerated") from error
    try:
        site_packages_before = site_packages.lstat()
    except OSError as exc:
        raise Phase9P0EvidenceError("trusted site-packages cannot be inspected") from exc
    if not stat.S_ISDIR(site_packages_before.st_mode) or stat.S_ISLNK(
        site_packages_before.st_mode
    ):
        raise Phase9P0EvidenceError("trusted site-packages is not a stable directory")
    directory_identities[site_packages] = dir_identity(site_packages_before)
    for name, module in (("pytest", pytest), ("_pytest", _pytest), ("pluggy", pluggy)):
        module_file = Path(str(module.__file__)).resolve(strict=True)
        module_root = module_file.parent
        try:
            module_root.relative_to(prefix)
        except ValueError as exc:
            raise Phase9P0EvidenceError(
                f"trusted {name} module escapes the Python environment"
            ) from exc
        for current, directories, files in os.walk(
            module_root, followlinks=False, onerror=traversal_error
        ):
            current_path = Path(current)
            current_metadata = current_path.lstat()
            if not stat.S_ISDIR(current_metadata.st_mode):
                raise Phase9P0EvidenceError("trusted runtime directory changed")
            directory_identities[current_path] = dir_identity(current_metadata)
            directories[:] = sorted(
                directory for directory in directories if directory != "__pycache__"
            )
            for directory in directories:
                child = current_path / directory
                child_metadata = child.lstat()
                if stat.S_ISLNK(child_metadata.st_mode) or not stat.S_ISDIR(
                    child_metadata.st_mode
                ):
                    raise Phase9P0EvidenceError(
                        f"trusted {name} runtime contains a linked/special directory"
                    )
            for filename in sorted(files):
                if filename.endswith((".pyc", ".pyo")):
                    continue
                path = current_path / filename
                metadata = path.lstat()
                if not stat.S_ISREG(metadata.st_mode):
                    raise Phase9P0EvidenceError(
                        f"trusted {name} runtime contains a linked/special file"
                    )
                raw = _stable_runtime_file(path, f"trusted {name} runtime file")
                records.append(
                    {
                        "path": f"{name}/{path.relative_to(module_root).as_posix()}",
                        "bytes": len(raw),
                        "sha256": hashlib.sha256(raw).hexdigest(),
                    }
                )
    for path in sorted(site_packages.glob("*.pth")):
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode):
            raise Phase9P0EvidenceError("Python startup path hook is unsafe")
        raw = _stable_runtime_file(path, "Python startup path hook")
        records.append(
            {
                "path": f"startup/{path.name}",
                "bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
        )
    for name in ("sitecustomize.py", "usercustomize.py", "pytest.py", "pluggy.py"):
        if (site_packages / name).exists() or (site_packages / name).is_symlink():
            raise Phase9P0EvidenceError(
                f"Python runtime contains forbidden startup/module shadow: {name}"
            )
    for directory, before in directory_identities.items():
        try:
            after = directory.lstat()
        except OSError as exc:
            raise Phase9P0EvidenceError("trusted runtime directory disappeared") from exc
        if dir_identity(after) != before:
            raise Phase9P0EvidenceError("trusted runtime directory changed during inventory")
    records.sort(key=lambda item: str(item["path"]))
    body: dict[str, object] = {
        "schema": "phase9-p0-trusted-python-runtime-v1",
        "python": _python_identity(requested),
        "sys_version": sys.version,
        "sys_prefix": str(prefix),
        "site_packages": str(site_packages),
        "uv_lock_sha256": hashlib.sha256(lock_raw).hexdigest(),
        "locked_versions": locked_versions,
        "actual_versions": actual_versions,
        "runtime_files": records,
        "runtime_file_count": len(records),
    }
    body["runtime_sha256"] = canonical_sha256(body)
    return body


def _p0_runtime_environment_descriptor(
    *,
    runtime: Mapping[str, object],
    sandbox: Mapping[str, object],
    producer: Mapping[str, object],
) -> dict[str, object]:
    """Canonical, path-independent runtime contract frozen by a generation.

    Per-invocation writable paths and the nonce belong in the command record;
    this descriptor binds the immutable interpreter/dependency, sandbox and
    reporter bytes that an execution-context receipt can know in advance.
    """

    body: dict[str, object] = {
        "schema": PHASE9_P0_RUNTIME_ENVIRONMENT_DESCRIPTOR_SCHEMA,
        "python_runtime_sha256": _sha(
            runtime.get("runtime_sha256"), "python runtime descriptor"
        ),
        "dependency_lock_sha256": _sha(
            runtime.get("uv_lock_sha256"), "python runtime dependency lock"
        ),
        "sandbox_sha256": _sha(sandbox.get("sha256"), "sandbox descriptor"),
        "trusted_reporter_blob_sha256": _sha(
            producer.get("trusted_reporter_blob_sha256"),
            "trusted reporter descriptor",
        ),
        "trusted_reporter_schema": TRUSTED_PYTEST_EVENT_SCHEMA,
        "interpreter_flags": ["-I", "-S", "-B"],
        "environment_policy": {
            "inherit_host_environment": False,
            "python_user_site": False,
            "pytest_plugin_autoload": False,
            "network_namespace": "UNSHARED",
            "host_root": "READ_ONLY",
        },
    }
    body["descriptor_sha256"] = canonical_sha256(body)
    return body


def _p0_launcher_descriptor(
    *,
    producer: Mapping[str, object],
    sandbox: Mapping[str, object],
) -> dict[str, object]:
    body: dict[str, object] = {
        "schema": PHASE9_P0_LAUNCHER_DESCRIPTOR_SCHEMA,
        "producer_source_blob_sha256": _sha(
            producer.get("source_blob_sha256"), "P0 producer source"
        ),
        "loaded_source_inventory_sha256": _sha(
            producer.get("loaded_source_inventory_sha256"),
            "P0 loaded producer inventory",
        ),
        "trusted_reporter_blob_sha256": _sha(
            producer.get("trusted_reporter_blob_sha256"),
            "P0 trusted reporter source",
        ),
        "sandbox_sha256": _sha(sandbox.get("sha256"), "P0 sandbox"),
        "interpreter_flags": ["-I", "-S", "-B"],
        "pytest_flags": [
            "-p", "no:cacheprovider", "--noconftest", "-c", "/dev/null",
            "-o", "addopts=", "-vv", "--tb=short",
        ],
        "dynamic_arguments": [
            "trusted_reporter_path",
            "runtime_site_packages",
            "source_root",
            "basetemp",
            "junitxml",
        ],
        "test_nodes": list(phase9_p0_test_nodes()),
        "sandbox_mount_policy": {
            "source": "READ_ONLY",
            "runtime": "READ_ONLY",
            "authority_database": "READ_ONLY",
            "writable": ["HOME", "XDG_CACHE_HOME", "TMPDIR", "JUNIT_PARENT"],
        },
    }
    body["descriptor_sha256"] = canonical_sha256(body)
    return body


def phase9_p0_execution_context_bindings(
    *, source_repository: str | Path, python_executable: str | Path
) -> dict[str, object]:
    """Derive the exact hashes a Phase-9 formal execution context must freeze."""

    source_root = Path(os.path.abspath(os.fspath(source_repository)))
    snapshot = read_current_git_source_snapshot(source_root)
    runtime = _trusted_python_runtime_identity(python_executable, source_root)
    sandbox = _python_identity("/usr/bin/bwrap")
    producer = _producer_descriptor(snapshot, sandbox)
    runtime_descriptor = _p0_runtime_environment_descriptor(
        runtime=runtime, sandbox=sandbox, producer=producer
    )
    launcher_descriptor = _p0_launcher_descriptor(
        producer=producer, sandbox=sandbox
    )
    body: dict[str, object] = {
        "schema": PHASE9_P0_EXECUTION_CONTEXT_BINDING_SCHEMA,
        "runtime_environment": runtime_descriptor,
        "dependency_lock_sha256": runtime["uv_lock_sha256"],
        "launcher": launcher_descriptor,
    }
    body["binding_sha256"] = canonical_sha256(body)
    return body


def _bind_generation_execution_context(
    value: object,
    *,
    runtime: Mapping[str, object],
    sandbox: Mapping[str, object],
    producer: Mapping[str, object],
) -> dict[str, object]:
    context = _mapping(
        value,
        "generation execution_context",
        {
            "schema_version", "context_id", "runtime_environment_sha256",
            "dependency_lock_sha256", "launcher_argv_sha256", "captured_at",
        },
    )
    if context["schema_version"] != EXECUTION_CONTEXT_EVIDENCE_SCHEMA:
        raise Phase9P0EvidenceError("generation execution context schema differs")
    _text(context["context_id"], "generation execution_context.context_id", identifier=True)
    _integer(context["captured_at"], "generation execution_context.captured_at")
    runtime_descriptor = _p0_runtime_environment_descriptor(
        runtime=runtime, sandbox=sandbox, producer=producer
    )
    launcher_descriptor = _p0_launcher_descriptor(
        producer=producer, sandbox=sandbox
    )
    if (
        context["runtime_environment_sha256"]
        != runtime_descriptor["descriptor_sha256"]
        or context["dependency_lock_sha256"] != runtime["uv_lock_sha256"]
        or context["launcher_argv_sha256"]
        != launcher_descriptor["descriptor_sha256"]
    ):
        raise Phase9P0EvidenceError(
            "generation execution context does not authorize this runtime/launcher"
        )
    body: dict[str, object] = {
        "schema": PHASE9_P0_EXECUTION_CONTEXT_BINDING_SCHEMA,
        "execution_context": dict(context),
        "execution_context_receipt_sha256": canonical_sha256(context),
        "runtime_environment": runtime_descriptor,
        "dependency_lock_sha256": runtime["uv_lock_sha256"],
        "launcher": launcher_descriptor,
    }
    body["binding_sha256"] = canonical_sha256(body)
    return body


def _safe_environment(root: Path) -> tuple[dict[str, str], list[str], Path, Path, Path]:
    runner_home = root.parent / f".{root.name}.runner-home"
    runner_cache = root.parent / f".{root.name}.runner-cache"
    runner_temp = root.parent / f".{root.name}.pytest"
    for item in (runner_home, runner_cache, runner_temp):
        item.mkdir(mode=0o700)
    present_blocked = sorted(
        name for name in os.environ
        if name in _BLOCKED_ENVIRONMENT_NAMES
        or any(fragment in name.upper() for fragment in _BLOCKED_ENVIRONMENT_FRAGMENTS)
    )
    environment = {
        "HOME": str(runner_home),
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/bin:/bin",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "TEMP": str(runner_temp),
        "TMP": str(runner_temp),
        "TMPDIR": str(runner_temp),
        "XDG_CACHE_HOME": str(runner_cache),
    }
    return environment, present_blocked, runner_home, runner_cache, runner_temp


def _candidate_from_snapshot(snapshot: object) -> dict[str, str]:
    source = snapshot.source
    return {
        "commit": source.source_commit,
        "tree": source.source_tree,
        "parent": source.source_parent,
    }


def _producer_descriptor(snapshot: object, sandbox: Mapping[str, object]) -> dict[str, object]:
    source_entry = next(
        (
            item
            for item in snapshot.tracked_inventory.entries
            if item.logical_path == PHASE9_P0_PRODUCER_SOURCE_PATH
        ),
        None,
    )
    if source_entry is None or source_entry.raw_bytes_sha256 is None:
        raise Phase9P0EvidenceError("trusted P0 producer source is absent from Git inventory")
    reporter_entry = next(
        (
            item
            for item in snapshot.tracked_inventory.entries
            if item.logical_path == PHASE9_P0_TRUSTED_REPORTER_SOURCE_PATH
        ),
        None,
    )
    if reporter_entry is None or reporter_entry.raw_bytes_sha256 is None:
        raise Phase9P0EvidenceError("trusted pytest reporter is absent from Git inventory")
    loaded_source = _loaded_p0_source_binding(snapshot)
    return {
        "producer_type": PHASE9_P0_PRODUCER_TYPE,
        "producer_version": PHASE9_P0_PRODUCER_VERSION,
        "source_path": PHASE9_P0_PRODUCER_SOURCE_PATH,
        "source_blob_sha256": source_entry.raw_bytes_sha256,
        "trusted_reporter_source_path": PHASE9_P0_TRUSTED_REPORTER_SOURCE_PATH,
        "trusted_reporter_blob_sha256": reporter_entry.raw_bytes_sha256,
        "trusted_reporter_schema": TRUSTED_PYTEST_EVENT_SCHEMA,
        "loaded_source_root": loaded_source["loaded_source_root"],
        "loaded_source_inventory_sha256": loaded_source[
            "loaded_source_inventory_sha256"
        ],
        "sandbox_path": sandbox["requested_path"],
        "sandbox_sha256": sandbox["sha256"],
    }


def _require_pristine_formal_source(root: Path) -> None:
    command = (
        "git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
        "-c", "submodule.recurse=false",
    )
    environment = {
        "PATH": "/usr/bin:/bin",
        "LC_ALL": "C",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    try:
        status = subprocess.run(
            (*command, "status", "--porcelain=v1", "--untracked-files=all", "--ignore-submodules=none"),
            cwd=root,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
            timeout=30,
        ).stdout
        ignored = subprocess.run(
            (*command, "ls-files", "--others", "--ignored", "--exclude-standard", "-z"),
            cwd=root,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
            timeout=30,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise Phase9P0EvidenceError(
            "formal source namespace could not be verified"
        ) from exc
    if status or ignored:
        raise Phase9P0EvidenceError(
            "formal source must have no modified, untracked, or ignored files"
        )


def _coordinate(project_id: str, workflow_id: str, run_generation: str) -> dict[str, str]:
    return {
        "project_id": _text(project_id, "project_id", identifier=True),
        "workflow_id": _text(workflow_id, "workflow_id", identifier=True),
        "run_generation": _text(run_generation, "run_generation", identifier=True),
    }


def _execute_fixed_phase9_p0_suite(
    *,
    source_repository: str | Path,
    evidence_root: str | Path,
    python_executable: str | Path,
    project_id: str,
    workflow_id: str,
    run_generation: str,
    authority_runner: Mapping[str, object],
) -> Phase9P0EvidenceBundle:
    """Internal subprocess stage; callers must use the DB-backed public runner."""

    root = Path(os.path.abspath(os.fspath(evidence_root)))
    source_root = Path(os.path.abspath(os.fspath(source_repository)))
    if root.exists() or root.is_symlink():
        raise Phase9P0EvidenceError("formal evidence root must not already exist")
    if not root.parent.exists() or root.parent.is_symlink() or not root.parent.is_dir():
        raise Phase9P0EvidenceError("formal evidence parent must be an existing directory")
    root.mkdir(mode=0o700)
    now = lambda: time.time_ns() // 1_000_000_000
    cleanup: tuple[Path, Path, Path] | None = None
    try:
        _require_pristine_formal_source(source_root)
        snapshot_before = read_current_git_source_snapshot(source_root)
        candidate = _candidate_from_snapshot(snapshot_before)
        coordinate = _coordinate(project_id, workflow_id, run_generation)
        authority_raw = canonical_bytes(dict(authority_runner))
        runtime_before = _trusted_python_runtime_identity(
            python_executable, source_root
        )
        python = runtime_before["python"]
        sandbox = _python_identity("/usr/bin/bwrap")
        producer = _producer_descriptor(snapshot_before, sandbox)
        authority_context_binding = authority_runner.get(
            "execution_context_binding"
        )
        if type(authority_context_binding) is not dict:
            raise Phase9P0EvidenceError(
                "Authority runner lacks an execution-context runtime binding"
            )
        execution_context_binding = _bind_generation_execution_context(
            authority_context_binding.get("execution_context"),
            runtime=runtime_before,
            sandbox=sandbox,
            producer=producer,
        )
        if (
            authority_runner.get("candidate") != candidate
            or authority_runner.get("coordinate") != coordinate
            or authority_runner.get("source_inventory_sha256")
            != snapshot_before.source_inventory_sha256
            or authority_runner.get("spec_sha256") != phase9_p0_spec_sha256()
            or authority_runner.get("intended_evidence_root") != str(root)
            or authority_runner.get("python_identity_sha256")
            != runtime_before["runtime_sha256"]
            or authority_runner.get("producer") != producer
            or authority_context_binding != execution_context_binding
        ):
            raise Phase9P0EvidenceError(
                "Authority runner consumption does not bind this execution"
            )
        environment, excluded, runner_home, runner_cache, runner_temp = _safe_environment(root)
        cleanup = (runner_home, runner_cache, runner_temp)
        trusted_nonce = secrets.token_hex(16)
        trusted_event_runtime = runner_temp / "trusted-pytest-events.jsonl"
        environment["PHASE9_TRUSTED_PYTEST_EVENT_PATH"] = str(
            trusted_event_runtime
        )
        environment["PHASE9_TRUSTED_PYTEST_NONCE"] = trusted_nonce
        requested_python = Path(str(python["requested_path"]))
        venv_root = requested_python.parent.parent
        host_checkout = venv_root.parent
        if (
            requested_python.parent.name != "bin"
            or venv_root.name != ".venv"
            or source_root == host_checkout
            or host_checkout in source_root.parents
        ):
            raise Phase9P0EvidenceError(
                "formal runner requires a separate .venv and candidate source root"
            )
        host_mask = runner_temp / "host-checkout-mask"
        (host_mask / ".venv").mkdir(parents=True)
        # bwrap cannot create a mount target below the read-only host mask.
        # Pre-create an empty target for a linked worktree's common Git dir.
        (host_mask / ".git").mkdir()
        git_object_mount = _external_git_object_mount(source_root)

        spec_raw = canonical_bytes(phase9_p0_acceptance_spec())
        source_body = {
            "schema": PHASE9_P0_SOURCE_ATTESTATION_SCHEMA,
            "evidence_domain": PHASE9_P0_FORMAL_DOMAIN,
            "candidate": candidate,
            "tracked_inventory": snapshot_before.tracked_inventory.as_dict(),
            "source_inventory_sha256": snapshot_before.source_inventory_sha256,
        }
        source_body["attestation_sha256"] = canonical_sha256(source_body)
        source_raw = canonical_bytes(source_body)
        environment_body = {
            "schema": PHASE9_P0_ENVIRONMENT_SCHEMA,
            "evidence_domain": PHASE9_P0_FORMAL_DOMAIN,
            "environment": environment,
            "environment_sha256": canonical_sha256(environment),
            "excluded_present_names": excluded,
            "cwd": str(source_root),
            "python": python,
            "python_runtime": runtime_before,
            "execution_context_binding": execution_context_binding,
            "sandbox": sandbox,
            "masked_host_checkout": str(host_checkout),
            "venv_root": str(venv_root),
            "git_object_mount": git_object_mount,
        }
        environment_body["attestation_sha256"] = canonical_sha256(environment_body)
        environment_raw = canonical_bytes(environment_body)
        reporter_raw = (source_root / PHASE9_P0_TRUSTED_REPORTER_SOURCE_PATH).read_bytes()
        reporter_entry = next(
            item for item in snapshot_before.tracked_inventory.entries
            if item.logical_path == PHASE9_P0_TRUSTED_REPORTER_SOURCE_PATH
        )
        if hashlib.sha256(reporter_raw).hexdigest() != reporter_entry.raw_bytes_sha256:
            raise Phase9P0EvidenceError("trusted pytest reporter bytes differ")
        _write_new(root / "attestations/authority_runner.json", authority_raw)
        _write_new(root / "attestations/p0_spec.json", spec_raw)
        _write_new(root / "attestations/source_inventory.json", source_raw)
        _write_new(root / "attestations/environment.json", environment_raw)
        reporter_absolute = root / "attestations/trusted_pytest_reporter.py"
        _write_new(reporter_absolute, reporter_raw)

        report_absolute = root / "test_reports/p0_suite.xml"
        report_absolute.parent.mkdir(mode=0o700, parents=True)
        nodes = phase9_p0_test_nodes()
        argv = [
            str(python["requested_path"]),
            "-I",
            "-S",
            "-B",
            str(reporter_absolute),
            "--runtime-site-packages",
            str(runtime_before["site_packages"]),
            "--source-root",
            str(source_root),
            "--",
            "-p",
            "no:cacheprovider",
            "--noconftest",
            "-c",
            "/dev/null",
            "--rootdir",
            str(source_root),
            "-o",
            "addopts=",
            "-vv",
            "--tb=short",
            f"--basetemp={runner_temp / 'basetemp'}",
            f"--junitxml={report_absolute}",
            *nodes,
        ]
        sandbox_argv = [
            str(sandbox["requested_path"]),
            "--unshare-all",
            "--new-session",
            "--die-with-parent",
            "--ro-bind",
            "/",
            "/",
            "--ro-bind",
            str(host_mask),
            str(host_checkout),
            "--ro-bind",
            str(venv_root),
            str(venv_root),
        ]
        if git_object_mount is not None:
            sandbox_argv.extend(
                [
                    "--ro-bind", git_object_mount["path"],
                    git_object_mount["path"],
                ]
            )
        sandbox_argv.extend([
            "--bind",
            str(report_absolute.parent),
            str(report_absolute.parent),
            "--bind",
            str(runner_home),
            str(runner_home),
            "--bind",
            str(runner_cache),
            str(runner_cache),
            "--bind",
            str(runner_temp),
            str(runner_temp),
            "--bind",
            str(runner_temp),
            "/tmp",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--chdir",
            str(source_root),
            "--",
            *argv,
        ])
        started_at = _integer(now(), "clock.started_at")
        started_monotonic = time.monotonic_ns()
        process = subprocess.Popen(
            sandbox_argv,
            cwd=source_root,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        try:
            stdout, _ = process.communicate(timeout=900)
        except subprocess.TimeoutExpired as exc:
            process.kill()
            stdout, _ = process.communicate()
            _write_new(root / "test_results/p0_suite.log", bytes(stdout))
            raise Phase9P0EvidenceError(
                "fixed Phase9 P0 suite exceeded its 900-second deadline"
            ) from exc
        finished_monotonic = time.monotonic_ns()
        finished_at = _integer(now(), "clock.finished_at")
        log_raw = bytes(stdout)
        _write_new(root / "test_results/p0_suite.log", log_raw)
        if process.returncode != 0:
            raise Phase9P0EvidenceError(
                f"fixed Phase9 P0 suite exited {process.returncode}; failure log retained"
            )
        report_raw = report_absolute.read_bytes()
        outcomes = _pytest_outcomes(log_raw, nodes)
        try:
            trusted_event_raw = trusted_event_runtime.read_bytes()
            trusted = validate_trusted_pytest_events(
                trusted_event_raw,
                nonce=trusted_nonce,
                expected_rootdir=str(source_root),
                expected_nodes=nodes,
            )
        except (OSError, UnicodeError, ValueError) as exc:
            raise Phase9P0EvidenceError(
                "trusted pytest event stream is absent or non-PASS"
            ) from exc
        trusted_outcomes = {
            **trusted["counts"],
            "node_outcomes": trusted["node_outcomes"],
        }
        if trusted_outcomes != outcomes:
            raise Phase9P0EvidenceError(
                "trusted pytest events and terminal outcomes differ"
            )
        _write_new(root / "test_events/p0_suite.jsonl", trusted_event_raw)
        _validate_junit(report_raw, nodes)
        _require_pristine_formal_source(source_root)
        if _python_identity(python["requested_path"]) != python:
            raise Phase9P0EvidenceError(
                "runner Python executable changed during the P0 run"
            )
        if _trusted_python_runtime_identity(
            python["requested_path"], source_root
        ) != runtime_before:
            raise Phase9P0EvidenceError(
                "runner Python/pytest dependency bytes changed during the P0 run"
            )
        if _python_identity(sandbox["requested_path"]) != sandbox:
            raise Phase9P0EvidenceError("bwrap sandbox executable changed during the P0 run")
        snapshot_after = read_current_git_source_snapshot(source_root)
        if snapshot_after != snapshot_before:
            raise Phase9P0EvidenceError("candidate source changed during the P0 run")

        outcome_body: dict[str, object] = {
            "schema": PHASE9_P0_TEST_OUTCOME_SCHEMA,
            "evidence_domain": PHASE9_P0_FORMAL_DOMAIN,
            "suite_id": PHASE9_P0_SUITE_ID,
            "candidate": candidate,
            "coordinate": coordinate,
            "spec_sha256": phase9_p0_spec_sha256(),
            "source_inventory_sha256": snapshot_before.source_inventory_sha256,
            "producer": producer,
            "authority_runner_evidence_sha256": hashlib.sha256(
                authority_raw
            ).hexdigest(),
            "outcomes": outcomes,
            "raw_log": _file_reference("test_results/p0_suite.log", log_raw),
            "junit_report": _file_reference("test_reports/p0_suite.xml", report_raw),
            "trusted_events": _file_reference(
                "test_events/p0_suite.jsonl", trusted_event_raw
            ),
        }
        outcome_body["outcome_sha256"] = canonical_sha256(outcome_body)
        outcome_raw = canonical_bytes(outcome_body)
        _write_new(root / "test_outcomes/p0_suite.json", outcome_raw)

        evidence = [
            _file_reference("attestations/authority_runner.json", authority_raw, kind="AUTHORITY_RUNNER_CONSUMPTION"),
            _file_reference("attestations/environment.json", environment_raw, kind="RUNNER_ENVIRONMENT"),
            _file_reference("attestations/p0_spec.json", spec_raw, kind="FIXED_ACCEPTANCE_SPEC"),
            _file_reference("attestations/source_inventory.json", source_raw, kind="SOURCE_INVENTORY"),
            _file_reference("attestations/trusted_pytest_reporter.py", reporter_raw, kind="TRUSTED_PYTEST_REPORTER"),
            _file_reference("test_events/p0_suite.jsonl", trusted_event_raw, kind="TRUSTED_PYTEST_EVENTS"),
            _file_reference("test_outcomes/p0_suite.json", outcome_raw, kind="PARSED_TEST_OUTCOME"),
            _file_reference("test_reports/p0_suite.xml", report_raw, kind="JUNIT_REPORT"),
        ]
        evidence.sort(key=lambda item: str(item["path"]).encode("utf-8"))
        command_body: dict[str, object] = {
            "schema": PHASE9_P0_COMMAND_RECORD_SCHEMA,
            "evidence_domain": PHASE9_P0_FORMAL_DOMAIN,
            "suite_id": PHASE9_P0_SUITE_ID,
            "candidate": candidate,
            "coordinate": coordinate,
            "spec_sha256": phase9_p0_spec_sha256(),
            "source_inventory_sha256": snapshot_before.source_inventory_sha256,
            "producer": producer,
            "authority_runner": _file_reference(
                "attestations/authority_runner.json", authority_raw
            ),
            "invocation_id": authority_runner.get("invocation_id"),
            "child_pid": process.pid,
            "source_attestation_sha256": hashlib.sha256(source_raw).hexdigest(),
            "cwd": str(source_root),
            "python": python,
            "python_runtime": runtime_before,
            "execution_context_binding": execution_context_binding,
            "sandbox": sandbox,
            "environment": environment,
            "environment_sha256": canonical_sha256(environment),
            "excluded_present_environment_names": excluded,
            "command_argv": argv,
            "sandbox_argv": sandbox_argv,
            "started_at": started_at,
            "finished_at": finished_at,
            "duration_ns": finished_monotonic - started_monotonic,
            "exit_code": process.returncode,
            "raw_test_log": _file_reference("test_results/p0_suite.log", log_raw),
            "junit_report": _file_reference("test_reports/p0_suite.xml", report_raw),
            "trusted_events": _file_reference(
                "test_events/p0_suite.jsonl", trusted_event_raw
            ),
            "test_outcome": _file_reference("test_outcomes/p0_suite.json", outcome_raw),
            "observed_outcomes": outcomes,
            "capabilities": dict(_CAPABILITIES),
        }
        command_body["command_record_sha256"] = canonical_sha256(command_body)
        command_raw = canonical_bytes(command_body)
        _write_new(root / "command_records/p0_suite.json", command_raw)

        receipts: dict[str, dict[str, object]] = {}
        receipt_paths: dict[str, Path] = {}
        for requirement in P0_REQUIREMENTS:
            body: dict[str, object] = {
                "schema": PHASE9_P0_RECEIPT_SCHEMA,
                "evidence_domain": PHASE9_P0_FORMAL_DOMAIN,
                "suite_id": PHASE9_P0_SUITE_ID,
                "requirement": requirement,
                "candidate": candidate,
                "coordinate": coordinate,
                "status": "PASS",
                "spec_sha256": phase9_p0_spec_sha256(),
                "source_inventory_sha256": snapshot_before.source_inventory_sha256,
                "producer": producer,
                "test_nodes": list(PHASE9_P0_TEST_NODES[requirement]),
                "command_record": _file_reference("command_records/p0_suite.json", command_raw),
                "raw_test_log": _file_reference("test_results/p0_suite.log", log_raw),
                "test_outcome": _file_reference("test_outcomes/p0_suite.json", outcome_raw),
                "trusted_events": _file_reference(
                    "test_events/p0_suite.jsonl", trusted_event_raw
                ),
                "evidence": evidence,
                "evidence_sha256": canonical_sha256(evidence),
                "command_exit_code": process.returncode,
                "capabilities": dict(_CAPABILITIES),
            }
            body["receipt_sha256"] = canonical_sha256(body)
            receipt_raw = canonical_bytes(body)
            path = root / f"receipts/{requirement}.json"
            _write_new(path, receipt_raw)
            receipts[requirement] = body
            receipt_paths[requirement] = path

        files = {
            path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob("*") if path.is_file()
        }
        if set(files) != set(formal_p0_paths()):
            raise Phase9P0EvidenceError("producer emitted an unexpected evidence inventory")
        return Phase9P0EvidenceBundle(
            root,
            evidence_root_sha256_from_files(files),
            candidate,
            coordinate,
            snapshot_before.source_inventory_sha256,
            receipt_paths,
            receipts,
            "",
        )
    finally:
        if cleanup is not None:
            for path in cleanup:
                shutil.rmtree(path, ignore_errors=True)


def _validate_candidate(value: object, expected: Mapping[str, str], path: str) -> None:
    item = _mapping(value, path, {"commit", "tree", "parent"})
    for name in ("commit", "tree", "parent"):
        _git_oid(item[name], f"{path}.{name}")
    if dict(item) != dict(expected):
        raise Phase9P0EvidenceError(f"{path} differs from the current candidate")


def _validate_coordinate(value: object, expected: Mapping[str, str], path: str) -> None:
    item = _mapping(value, path, {"project_id", "workflow_id", "run_generation"})
    for name in item:
        _text(item[name], f"{path}.{name}", identifier=True)
    if dict(item) != dict(expected):
        raise Phase9P0EvidenceError(f"{path} differs from the current coordinate")


def _validate_reference(
    value: object,
    *,
    expected_path: str,
    files: Mapping[str, bytes],
    path: str,
    kind: str | None = None,
) -> bytes:
    keys = {"path", "byte_length", "sha256"} | ({"kind"} if kind is not None else set())
    item = _mapping(value, path, keys)
    if item["path"] != expected_path or (kind is not None and item["kind"] != kind):
        raise Phase9P0EvidenceError(f"{path} logical path/kind differs")
    raw = files.get(expected_path)
    length = _integer(item["byte_length"], f"{path}.byte_length", minimum=1)
    digest = _sha(item["sha256"], f"{path}.sha256")
    if raw is None or len(raw) != length or hashlib.sha256(raw).hexdigest() != digest:
        raise Phase9P0EvidenceError(f"{path} bytes/length/hash differ")
    return raw


def _validate_source_attestation(
    raw: bytes, *, candidate: Mapping[str, str], expected_inventory_sha256: str
) -> None:
    value = _canonical_json(raw, "attestations/source_inventory.json")
    _mapping(
        value,
        "source_attestation",
        {"schema", "evidence_domain", "candidate", "tracked_inventory", "source_inventory_sha256", "attestation_sha256"},
    )
    if value["schema"] != PHASE9_P0_SOURCE_ATTESTATION_SCHEMA or value["evidence_domain"] != PHASE9_P0_FORMAL_DOMAIN:
        raise Phase9P0EvidenceError("source attestation is not in the formal domain")
    _validate_candidate(value["candidate"], candidate, "source_attestation.candidate")
    inventory = _mapping(
        value["tracked_inventory"],
        "source_attestation.tracked_inventory",
        {"schema_version", "source_commit", "source_tree", "source_parent", "entries", "path_count", "total_bytes"},
    )
    if (
        inventory["schema_version"] != GIT_TRACKED_SOURCE_INVENTORY_SCHEMA
        or inventory["source_commit"] != candidate["commit"]
        or inventory["source_tree"] != candidate["tree"]
        or inventory["source_parent"] != candidate["parent"]
        or type(inventory["entries"]) is not list
        or not inventory["entries"]
        or inventory["path_count"] != len(inventory["entries"])
        or type(inventory["total_bytes"]) is not int
        or inventory["total_bytes"] < 0
    ):
        raise Phase9P0EvidenceError("source tracked inventory structure/identity differs")
    previous = ""
    total = 0
    inventory_paths: set[str] = set()
    for index, raw_entry in enumerate(inventory["entries"]):
        entry = _mapping(
            raw_entry,
            f"source_attestation.entries[{index}]",
            {"schema_version", "logical_path", "git_mode", "git_object_type", "git_object_id", "byte_length", "raw_bytes_sha256"},
        )
        if entry["schema_version"] != GIT_TRACKED_SOURCE_ENTRY_SCHEMA:
            raise Phase9P0EvidenceError("source tracked entry schema differs")
        logical = _text(entry["logical_path"], f"source_attestation.entries[{index}].logical_path")
        if logical <= previous or logical.startswith("/") or "\\" in logical or ".." in Path(logical).parts:
            raise Phase9P0EvidenceError("source tracked inventory paths differ")
        previous = logical
        inventory_paths.add(logical)
        _git_oid(entry["git_object_id"], f"source_attestation.entries[{index}].git_object_id")
        if entry["git_mode"] == "160000":
            if entry["git_object_type"] != "commit" or entry["byte_length"] is not None or entry["raw_bytes_sha256"] is not None:
                raise Phase9P0EvidenceError("source gitlink evidence differs")
        else:
            if entry["git_mode"] not in {"100644", "100755"} or entry["git_object_type"] != "blob":
                raise Phase9P0EvidenceError("source blob mode/type differs")
            total += _integer(entry["byte_length"], f"source_attestation.entries[{index}].byte_length")
            _sha(entry["raw_bytes_sha256"], f"source_attestation.entries[{index}].raw_bytes_sha256")
    if total != inventory["total_bytes"]:
        raise Phase9P0EvidenceError("source tracked inventory total bytes differ")
    required_source_paths = {
        "tools/run_phase9_p0_evidence.py",
        PHASE9_P0_PRODUCER_SOURCE_PATH,
        PHASE9_P0_TRUSTED_REPORTER_SOURCE_PATH,
    } | {
        node.split("::", 1)[0] for node in phase9_p0_test_nodes()
    }
    if not required_source_paths.issubset(inventory_paths):
        raise Phase9P0EvidenceError(
            "source inventory omits the trusted runner or a fixed test source"
        )
    expected = _sha(expected_inventory_sha256, "expected source inventory")
    if value["source_inventory_sha256"] != expected or canonical_sha256(inventory) != expected:
        raise Phase9P0EvidenceError("source tracked inventory hash differs")
    _self_hash(value, "attestation_sha256", "source_attestation")


def _validate_authority_runner_evidence(
    raw: bytes,
    *,
    candidate: Mapping[str, str],
    coordinate: Mapping[str, str],
    expected_inventory_sha256: str,
) -> tuple[dict[str, object], str]:
    value = _canonical_json(raw, "attestations/authority_runner.json")
    _mapping(
        value,
        "authority_runner",
        {
            "schema",
            "evidence_domain",
            "authorization_id",
            "authorization_receipt_sha256",
            "nonce",
            "nonce_sha256",
            "invocation_id",
            "candidate",
            "coordinate",
            "source_inventory_sha256",
            "live_binding_sha256",
            "spec_sha256",
            "producer",
            "operator_uid",
            "operator_account",
            "issued_at",
            "expires_at",
            "consumed_at",
            "intended_evidence_root",
            "python_identity_sha256",
            "execution_context_binding",
            "capabilities",
            "evidence_sha256",
        },
    )
    if (
        value["schema"] != PHASE9_P0_AUTHORITY_RUNNER_EVIDENCE_SCHEMA
        or value["evidence_domain"] != PHASE9_P0_FORMAL_DOMAIN
    ):
        raise Phase9P0EvidenceError("authority runner evidence domain/schema differs")
    _text(value["authorization_id"], "authority_runner.authorization_id", identifier=True)
    _text(value["invocation_id"], "authority_runner.invocation_id", identifier=True)
    nonce = _text(value["nonce"], "authority_runner.nonce")
    if hashlib.sha256(nonce.encode("utf-8")).hexdigest() != _sha(
        value["nonce_sha256"], "authority_runner.nonce_sha256"
    ):
        raise Phase9P0EvidenceError("authority runner nonce binding differs")
    _sha(
        value["authorization_receipt_sha256"],
        "authority_runner.authorization_receipt_sha256",
    )
    _validate_candidate(value["candidate"], candidate, "authority_runner.candidate")
    _validate_coordinate(value["coordinate"], coordinate, "authority_runner.coordinate")
    if (
        value["source_inventory_sha256"] != expected_inventory_sha256
        or value["spec_sha256"] != phase9_p0_spec_sha256()
    ):
        raise Phase9P0EvidenceError("authority runner source/spec/capabilities differ")
    _validate_capabilities(value["capabilities"], "authority_runner.capabilities")
    producer = _mapping(
        value["producer"],
        "authority_runner.producer",
        {
            "producer_type",
            "producer_version",
            "source_path",
            "source_blob_sha256",
            "trusted_reporter_source_path",
            "trusted_reporter_blob_sha256",
            "trusted_reporter_schema",
            "loaded_source_root",
            "loaded_source_inventory_sha256",
            "sandbox_path",
            "sandbox_sha256",
        },
    )
    if (
        producer["producer_type"] != PHASE9_P0_PRODUCER_TYPE
        or producer["producer_version"] != PHASE9_P0_PRODUCER_VERSION
        or producer["source_path"] != PHASE9_P0_PRODUCER_SOURCE_PATH
        or producer["trusted_reporter_source_path"]
        != PHASE9_P0_TRUSTED_REPORTER_SOURCE_PATH
        or producer["trusted_reporter_schema"] != TRUSTED_PYTEST_EVENT_SCHEMA
        or producer["sandbox_path"] != "/usr/bin/bwrap"
    ):
        raise Phase9P0EvidenceError("authority runner producer type/version differs")
    _sha(producer["source_blob_sha256"], "authority_runner.producer.source_blob_sha256")
    _sha(
        producer["trusted_reporter_blob_sha256"],
        "authority_runner.producer.trusted_reporter_blob_sha256",
    )
    if not Path(
        _text(producer["loaded_source_root"], "authority_runner.producer.loaded_source_root")
    ).is_absolute():
        raise Phase9P0EvidenceError("authority runner loaded source root must be absolute")
    _sha(
        producer["loaded_source_inventory_sha256"],
        "authority_runner.producer.loaded_source_inventory_sha256",
    )
    _sha(producer["sandbox_sha256"], "authority_runner.producer.sandbox_sha256")
    _sha(value["live_binding_sha256"], "authority_runner.live_binding_sha256")
    _sha(value["python_identity_sha256"], "authority_runner.python_identity_sha256")
    binding = _mapping(
        value["execution_context_binding"],
        "authority_runner.execution_context_binding",
        {
            "schema", "execution_context", "execution_context_receipt_sha256",
            "runtime_environment", "dependency_lock_sha256", "launcher",
            "binding_sha256",
        },
    )
    if binding["schema"] != PHASE9_P0_EXECUTION_CONTEXT_BINDING_SCHEMA:
        raise Phase9P0EvidenceError("authority runner execution-context schema differs")
    _self_hash(
        binding,
        "binding_sha256",
        "authority_runner.execution_context_binding",
    )
    _integer(value["operator_uid"], "authority_runner.operator_uid")
    _text(value["operator_account"], "authority_runner.operator_account", identifier=True)
    issued = _integer(value["issued_at"], "authority_runner.issued_at")
    expires = _integer(value["expires_at"], "authority_runner.expires_at")
    consumed = _integer(value["consumed_at"], "authority_runner.consumed_at")
    if (
        not issued <= consumed <= expires
        or expires - issued > PHASE9_P0_RUNNER_AUTHORIZATION_TTL_SECONDS
    ):
        raise Phase9P0EvidenceError("authority runner authorization time bounds differ")
    intended = Path(
        _text(value["intended_evidence_root"], "authority_runner.intended_evidence_root")
    )
    if not intended.is_absolute():
        raise Phase9P0EvidenceError("authority runner evidence root must be absolute")
    evidence_sha = _self_hash(value, "evidence_sha256", "authority_runner")
    return dict(value), evidence_sha


def validate_formal_phase9_p0_evidence(
    *,
    receipts: Mapping[str, object],
    files: Mapping[str, bytes],
    candidate: Mapping[str, str],
    coordinate: Mapping[str, str],
    source_inventory_sha256: str,
) -> ValidatedPhase9P0Evidence:
    """Validate the fixed external closure; entry binds it to Authority state."""

    if type(receipts) is not dict or set(receipts) != set(P0_REQUIREMENTS):
        raise Phase9P0EvidenceError("P0 receipt set must contain exactly all nine receipts")
    if set(files) != set(formal_p0_paths()):
        raise Phase9P0EvidenceError("P0 formal evidence file inventory differs")
    expected_coordinate = _coordinate(
        str(coordinate.get("project_id", "")),
        str(coordinate.get("workflow_id", "")),
        str(coordinate.get("run_generation", "")),
    )
    expected_candidate = {
        "commit": _git_oid(candidate.get("commit"), "candidate.commit"),
        "tree": _git_oid(candidate.get("tree"), "candidate.tree"),
        "parent": _git_oid(candidate.get("parent"), "candidate.parent"),
    }
    expected_inventory = _sha(source_inventory_sha256, "source_inventory_sha256")

    spec = _canonical_json(files["attestations/p0_spec.json"], "P0 acceptance spec")
    if spec != phase9_p0_acceptance_spec():
        raise Phase9P0EvidenceError("P0 acceptance spec differs from the fixed reviewed spec")
    _validate_source_attestation(
        files["attestations/source_inventory.json"],
        candidate=expected_candidate,
        expected_inventory_sha256=expected_inventory,
    )
    authority_runner, authority_runner_sha = _validate_authority_runner_evidence(
        files["attestations/authority_runner.json"],
        candidate=expected_candidate,
        coordinate=expected_coordinate,
        expected_inventory_sha256=expected_inventory,
    )
    environment = _canonical_json(files["attestations/environment.json"], "runner environment")
    _mapping(
        environment,
        "runner_environment",
        {"schema", "evidence_domain", "environment", "environment_sha256", "excluded_present_names", "cwd", "python", "python_runtime", "execution_context_binding", "sandbox", "masked_host_checkout", "venv_root", "git_object_mount", "attestation_sha256"},
    )
    if environment["schema"] != PHASE9_P0_ENVIRONMENT_SCHEMA or environment["evidence_domain"] != PHASE9_P0_FORMAL_DOMAIN:
        raise Phase9P0EvidenceError("runner environment is not in the formal domain")
    environment_values = _mapping(
        environment["environment"],
        "runner_environment.environment",
        {
            "HOME",
            "LC_ALL",
            "PATH",
            "PYTHONDONTWRITEBYTECODE",
            "PYTHONNOUSERSITE",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD",
            "TEMP",
            "TMP",
            "TMPDIR",
            "XDG_CACHE_HOME",
            "PHASE9_TRUSTED_PYTEST_EVENT_PATH",
            "PHASE9_TRUSTED_PYTEST_NONCE",
        },
    )
    for name, value in environment_values.items():
        _text(value, f"runner_environment.environment.{name}")
    runner_paths = ("HOME", "TEMP", "TMP", "TMPDIR", "XDG_CACHE_HOME")
    if (
        environment_values["PATH"] != "/usr/bin:/bin"
        or environment_values["LC_ALL"] != "C.UTF-8"
        or any(environment_values[name] != "1" for name in ("PYTHONDONTWRITEBYTECODE", "PYTHONNOUSERSITE", "PYTEST_DISABLE_PLUGIN_AUTOLOAD"))
        or any(not Path(environment_values[name]).is_absolute() for name in runner_paths)
        or not (
            environment_values["TEMP"]
            == environment_values["TMP"]
            == environment_values["TMPDIR"]
        )
        or environment_values["PHASE9_TRUSTED_PYTEST_EVENT_PATH"]
        != str(Path(environment_values["TMPDIR"]) / "trusted-pytest-events.jsonl")
        or re.fullmatch(
            r"[0-9a-f]{32}",
            environment_values["PHASE9_TRUSTED_PYTEST_NONCE"],
        ) is None
        or environment["environment_sha256"] != canonical_sha256(environment_values)
        or type(environment["excluded_present_names"]) is not list
        or environment["excluded_present_names"] != sorted(set(environment["excluded_present_names"]))
        or any(
            name not in _BLOCKED_ENVIRONMENT_NAMES
            and not any(fragment in name.upper() for fragment in _BLOCKED_ENVIRONMENT_FRAGMENTS)
            for name in environment["excluded_present_names"]
        )
    ):
        raise Phase9P0EvidenceError("runner sanitized environment differs")
    cwd = Path(_text(environment["cwd"], "runner_environment.cwd"))
    if not cwd.is_absolute():
        raise Phase9P0EvidenceError("runner cwd must be absolute")
    try:
        cwd_metadata = cwd.lstat()
    except OSError as exc:
        raise Phase9P0EvidenceError("runner cwd no longer exists") from exc
    if stat.S_ISLNK(cwd_metadata.st_mode) or not stat.S_ISDIR(cwd_metadata.st_mode):
        raise Phase9P0EvidenceError(
            "runner cwd must remain a non-symlink source directory"
        )
    try:
        _require_pristine_formal_source(cwd)
        live_source = read_current_git_source_snapshot(cwd)
    except Exception as exc:
        raise Phase9P0EvidenceError(
            "runner cwd is not the recorded pristine candidate source"
        ) from exc
    if (
        _candidate_from_snapshot(live_source) != expected_candidate
        or live_source.source_inventory_sha256 != expected_inventory
    ):
        raise Phase9P0EvidenceError(
            "runner cwd candidate/source inventory differs"
        )
    python = _mapping(
        environment["python"], "runner_environment.python",
        {"requested_path", "resolved_path", "byte_length", "sha256"},
    )
    for name in ("requested_path", "resolved_path"):
        if not Path(_text(python[name], f"runner_environment.python.{name}")).is_absolute():
            raise Phase9P0EvidenceError("runner Python paths must be absolute")
    _integer(python["byte_length"], "runner_environment.python.byte_length", minimum=1)
    _sha(python["sha256"], "runner_environment.python.sha256")
    if _python_identity(str(python["requested_path"])) != dict(python):
        raise Phase9P0EvidenceError(
            "runner Python executable no longer matches its recorded bytes"
        )
    python_runtime = _mapping(
        environment["python_runtime"],
        "runner_environment.python_runtime",
        {
            "schema", "python", "sys_version", "sys_prefix", "site_packages", "uv_lock_sha256",
            "locked_versions", "actual_versions", "runtime_files",
            "runtime_file_count", "runtime_sha256",
        },
    )
    if (
        python_runtime["python"] != python
        or _trusted_python_runtime_identity(
            str(python["requested_path"]), cwd
        ) != dict(python_runtime)
    ):
        raise Phase9P0EvidenceError("trusted Python/pytest runtime bytes differ")
    sandbox = _mapping(
        environment["sandbox"], "runner_environment.sandbox",
        {"requested_path", "resolved_path", "byte_length", "sha256"},
    )
    if sandbox["requested_path"] != "/usr/bin/bwrap":
        raise Phase9P0EvidenceError("runner sandbox must be the fixed bwrap path")
    if _python_identity(str(sandbox["requested_path"])) != dict(sandbox):
        raise Phase9P0EvidenceError("runner bwrap executable bytes differ")
    expected_execution_context_binding = _bind_generation_execution_context(
        authority_runner["execution_context_binding"]["execution_context"],
        runtime=python_runtime,
        sandbox=sandbox,
        producer=authority_runner["producer"],
    )
    if (
        environment["execution_context_binding"]
        != expected_execution_context_binding
        or authority_runner["execution_context_binding"]
        != expected_execution_context_binding
    ):
        raise Phase9P0EvidenceError(
            "runner execution context does not bind runtime/launcher bytes"
        )
    masked_host_checkout = Path(
        _text(environment["masked_host_checkout"], "runner_environment.masked_host_checkout")
    )
    venv_root = Path(_text(environment["venv_root"], "runner_environment.venv_root"))
    git_object_mount = environment["git_object_mount"]
    if git_object_mount is not None:
        git_object_mount = _mapping(
            git_object_mount,
            "runner_environment.git_object_mount",
            {"path", "access"},
        )
        git_mount_path = Path(
            _text(git_object_mount["path"], "runner_environment.git_object_mount.path")
        )
        if not git_mount_path.is_absolute() or git_object_mount["access"] != "READ_ONLY":
            raise Phase9P0EvidenceError("runner Git object mount differs")
    if (
        not masked_host_checkout.is_absolute()
        or not venv_root.is_absolute()
        or venv_root.parent != masked_host_checkout
        or Path(str(python["requested_path"])).parent.parent != venv_root
        or cwd == masked_host_checkout
        or masked_host_checkout in cwd.parents
        or environment["git_object_mount"] != _external_git_object_mount(cwd)
    ):
        raise Phase9P0EvidenceError("runner host-checkout isolation differs")
    source_value = _canonical_json(
        files["attestations/source_inventory.json"], "source attestation"
    )
    producer_entry = next(
        (
            item for item in source_value["tracked_inventory"]["entries"]
            if item["logical_path"] == PHASE9_P0_PRODUCER_SOURCE_PATH
        ),
        None,
    )
    reporter_entry = next(
        (
            item for item in source_value["tracked_inventory"]["entries"]
            if item["logical_path"] == PHASE9_P0_TRUSTED_REPORTER_SOURCE_PATH
        ),
        None,
    )
    expected_producer = authority_runner["producer"]
    live_producer = _producer_descriptor(live_source, sandbox)
    if (
        producer_entry is None
        or reporter_entry is None
        or expected_producer != live_producer
        or producer_entry["raw_bytes_sha256"]
        != expected_producer["source_blob_sha256"]
        or reporter_entry["raw_bytes_sha256"]
        != expected_producer["trusted_reporter_blob_sha256"]
        or hashlib.sha256(
            files["attestations/trusted_pytest_reporter.py"]
        ).hexdigest() != expected_producer["trusted_reporter_blob_sha256"]
        or sandbox["sha256"] != expected_producer["sandbox_sha256"]
        or authority_runner["python_identity_sha256"]
        != python_runtime["runtime_sha256"]
    ):
        raise Phase9P0EvidenceError("runner producer byte identity differs")
    _self_hash(environment, "attestation_sha256", "runner_environment")

    log_raw = files["test_results/p0_suite.log"]
    report_raw = files["test_reports/p0_suite.xml"]
    trusted_event_raw = files["test_events/p0_suite.jsonl"]
    outcome_raw = files["test_outcomes/p0_suite.json"]
    nodes = phase9_p0_test_nodes()
    observed = _pytest_outcomes(log_raw, nodes)
    try:
        trusted = validate_trusted_pytest_events(
            trusted_event_raw,
            nonce=environment_values["PHASE9_TRUSTED_PYTEST_NONCE"],
            expected_rootdir=str(cwd),
            expected_nodes=nodes,
        )
    except (UnicodeError, ValueError) as exc:
        raise Phase9P0EvidenceError("trusted pytest event stream differs") from exc
    trusted_observed = {
        **trusted["counts"],
        "node_outcomes": trusted["node_outcomes"],
    }
    if trusted_observed != observed:
        raise Phase9P0EvidenceError(
            "trusted pytest events and terminal outcomes differ"
        )
    _validate_junit(report_raw, nodes)
    outcome = _canonical_json(outcome_raw, "test outcome")
    _mapping(
        outcome,
        "test_outcome",
        {"schema", "evidence_domain", "suite_id", "candidate", "coordinate", "spec_sha256", "source_inventory_sha256", "producer", "authority_runner_evidence_sha256", "outcomes", "raw_log", "junit_report", "trusted_events", "outcome_sha256"},
    )
    if outcome["schema"] != PHASE9_P0_TEST_OUTCOME_SCHEMA or outcome["evidence_domain"] != PHASE9_P0_FORMAL_DOMAIN or outcome["suite_id"] != PHASE9_P0_SUITE_ID:
        raise Phase9P0EvidenceError("test outcome is not a formal fixed-suite outcome")
    outcome_counts = _mapping(
        outcome["outcomes"],
        "test_outcome.outcomes",
        set(_OUTCOME_KEYS) | {"node_outcomes"},
    )
    for name in _OUTCOME_KEYS:
        _integer(outcome_counts[name], f"test_outcome.outcomes.{name}")
    _validate_candidate(outcome["candidate"], expected_candidate, "test_outcome.candidate")
    _validate_coordinate(outcome["coordinate"], expected_coordinate, "test_outcome.coordinate")
    if (
        outcome["spec_sha256"] != phase9_p0_spec_sha256()
        or outcome["source_inventory_sha256"] != expected_inventory
        or outcome["producer"] != expected_producer
        or outcome["authority_runner_evidence_sha256"]
        != hashlib.sha256(files["attestations/authority_runner.json"]).hexdigest()
        or outcome["outcomes"] != observed
    ):
        raise Phase9P0EvidenceError("test outcome binding differs")
    _validate_reference(outcome["raw_log"], expected_path="test_results/p0_suite.log", files=files, path="test_outcome.raw_log")
    _validate_reference(outcome["junit_report"], expected_path="test_reports/p0_suite.xml", files=files, path="test_outcome.junit_report")
    _validate_reference(outcome["trusted_events"], expected_path="test_events/p0_suite.jsonl", files=files, path="test_outcome.trusted_events")
    _self_hash(outcome, "outcome_sha256", "test_outcome")

    command = _canonical_json(files["command_records/p0_suite.json"], "command record")
    _mapping(
        command,
        "command_record",
        {"schema", "evidence_domain", "suite_id", "candidate", "coordinate", "spec_sha256", "source_inventory_sha256", "producer", "authority_runner", "invocation_id", "child_pid", "source_attestation_sha256", "cwd", "python", "python_runtime", "execution_context_binding", "sandbox", "environment", "environment_sha256", "excluded_present_environment_names", "command_argv", "sandbox_argv", "started_at", "finished_at", "duration_ns", "exit_code", "raw_test_log", "junit_report", "trusted_events", "test_outcome", "observed_outcomes", "capabilities", "command_record_sha256"},
    )
    if command["schema"] != PHASE9_P0_COMMAND_RECORD_SCHEMA or command["evidence_domain"] != PHASE9_P0_FORMAL_DOMAIN or command["suite_id"] != PHASE9_P0_SUITE_ID:
        raise Phase9P0EvidenceError("command record is not in the formal fixed-suite domain")
    _validate_candidate(command["candidate"], expected_candidate, "command_record.candidate")
    _validate_coordinate(command["coordinate"], expected_coordinate, "command_record.coordinate")
    if (
        command["spec_sha256"] != phase9_p0_spec_sha256()
        or command["source_inventory_sha256"] != expected_inventory
        or command["producer"] != expected_producer
        or command["invocation_id"] != authority_runner["invocation_id"]
        or command["source_attestation_sha256"] != hashlib.sha256(files["attestations/source_inventory.json"]).hexdigest()
        or command["cwd"] != environment["cwd"]
        or command["python"] != environment["python"]
        or command["python_runtime"] != environment["python_runtime"]
        or command["execution_context_binding"]
        != environment["execution_context_binding"]
        or command["sandbox"] != environment["sandbox"]
        or command["environment"] != environment_values
        or command["environment_sha256"] != environment["environment_sha256"]
        or command["excluded_present_environment_names"] != environment["excluded_present_names"]
        or command["observed_outcomes"] != observed
    ):
        raise Phase9P0EvidenceError("command record provenance/outcome binding differs")
    if _integer(command["exit_code"], "command_record.exit_code") != 0:
        raise Phase9P0EvidenceError("command record exit code differs")
    _validate_capabilities(command["capabilities"], "command_record.capabilities")
    _integer(command["child_pid"], "command_record.child_pid", minimum=1)
    _validate_reference(
        command["authority_runner"],
        expected_path="attestations/authority_runner.json",
        files=files,
        path="command_record.authority_runner",
    )
    started = _integer(command["started_at"], "command_record.started_at")
    finished = _integer(command["finished_at"], "command_record.finished_at")
    _integer(command["duration_ns"], "command_record.duration_ns")
    if finished < started:
        raise Phase9P0EvidenceError("command record timestamps are reversed")
    evidence_root = Path(
        _text(
            authority_runner["intended_evidence_root"],
            "authority_runner.intended_evidence_root",
        )
    )
    argv = command["command_argv"]
    if type(argv) is not list or any(type(item) is not str or not item for item in argv):
        raise Phase9P0EvidenceError("command argv must be a non-empty string array")
    fixed_prefix = [
        python["requested_path"],
        "-I",
        "-S",
        "-B",
        str(evidence_root / "attestations/trusted_pytest_reporter.py"),
        "--runtime-site-packages",
        str(python_runtime["site_packages"]),
        "--source-root",
        str(cwd),
        "--",
        "-p",
        "no:cacheprovider",
        "--noconftest",
        "-c",
        "/dev/null",
        "--rootdir",
        str(cwd),
        "-o",
        "addopts=",
        "-vv",
        "--tb=short",
    ]
    if (
        argv[: len(fixed_prefix)] != fixed_prefix
        or len(argv) != len(fixed_prefix) + 2 + len(nodes)
        or argv[len(fixed_prefix)]
        != f"--basetemp={Path(environment_values['TMPDIR']) / 'basetemp'}"
        or not argv[len(fixed_prefix) + 1].startswith("--junitxml=/")
        or argv[-len(nodes):] != list(nodes)
    ):
        raise Phase9P0EvidenceError("command argv differs from the fixed Python -m pytest shape")
    junit_absolute = Path(argv[len(fixed_prefix) + 1].split("=", 1)[1])
    if junit_absolute.parent.parent != evidence_root:
        raise Phase9P0EvidenceError("JUnit path does not bind the evidence root")
    expected_sandbox_argv = [
        "/usr/bin/bwrap",
        "--unshare-all",
        "--new-session",
        "--die-with-parent",
        "--ro-bind",
        "/",
        "/",
        "--ro-bind",
        str(Path(environment_values["TMPDIR"]) / "host-checkout-mask"),
        str(masked_host_checkout),
        "--ro-bind",
        str(venv_root),
        str(venv_root),
    ]
    if git_object_mount is not None:
        expected_sandbox_argv.extend(
            ["--ro-bind", str(git_mount_path), str(git_mount_path)]
        )
    expected_sandbox_argv.extend([
        "--bind",
        str(junit_absolute.parent),
        str(junit_absolute.parent),
        "--bind",
        environment_values["HOME"],
        environment_values["HOME"],
        "--bind",
        environment_values["XDG_CACHE_HOME"],
        environment_values["XDG_CACHE_HOME"],
        "--bind",
        environment_values["TMPDIR"],
        environment_values["TMPDIR"],
        "--bind",
        environment_values["TMPDIR"],
        "/tmp",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--chdir",
        str(cwd),
        "--",
        *argv,
    ])
    if (
        command["sandbox_argv"] != expected_sandbox_argv
        or authority_runner["intended_evidence_root"] != str(evidence_root)
    ):
        raise Phase9P0EvidenceError("command bwrap sandbox argv differs")
    _validate_reference(command["raw_test_log"], expected_path="test_results/p0_suite.log", files=files, path="command_record.raw_test_log")
    _validate_reference(command["junit_report"], expected_path="test_reports/p0_suite.xml", files=files, path="command_record.junit_report")
    _validate_reference(command["trusted_events"], expected_path="test_events/p0_suite.jsonl", files=files, path="command_record.trusted_events")
    _validate_reference(command["test_outcome"], expected_path="test_outcomes/p0_suite.json", files=files, path="command_record.test_outcome")
    _self_hash(command, "command_record_sha256", "command_record")

    evidence_expected = [
        _file_reference("attestations/authority_runner.json", files["attestations/authority_runner.json"], kind="AUTHORITY_RUNNER_CONSUMPTION"),
        _file_reference("attestations/environment.json", files["attestations/environment.json"], kind="RUNNER_ENVIRONMENT"),
        _file_reference("attestations/p0_spec.json", files["attestations/p0_spec.json"], kind="FIXED_ACCEPTANCE_SPEC"),
        _file_reference("attestations/source_inventory.json", files["attestations/source_inventory.json"], kind="SOURCE_INVENTORY"),
        _file_reference("attestations/trusted_pytest_reporter.py", files["attestations/trusted_pytest_reporter.py"], kind="TRUSTED_PYTEST_REPORTER"),
        _file_reference("test_events/p0_suite.jsonl", trusted_event_raw, kind="TRUSTED_PYTEST_EVENTS"),
        _file_reference("test_outcomes/p0_suite.json", outcome_raw, kind="PARSED_TEST_OUTCOME"),
        _file_reference("test_reports/p0_suite.xml", report_raw, kind="JUNIT_REPORT"),
    ]
    receipt_hashes: dict[str, str] = {}
    for requirement in P0_REQUIREMENTS:
        receipt_path = f"receipts/{requirement}.json"
        receipt_raw = files[receipt_path]
        receipt = _canonical_json(receipt_raw, receipt_path)
        if receipt != receipts[requirement]:
            raise Phase9P0EvidenceError(f"{requirement} supplied receipt differs from root bytes")
        _mapping(
            receipt,
            f"p0_receipts.{requirement}",
            {"schema", "evidence_domain", "suite_id", "requirement", "candidate", "coordinate", "status", "spec_sha256", "source_inventory_sha256", "producer", "test_nodes", "command_record", "raw_test_log", "test_outcome", "trusted_events", "evidence", "evidence_sha256", "command_exit_code", "capabilities", "receipt_sha256"},
        )
        if (
            receipt["schema"] != PHASE9_P0_RECEIPT_SCHEMA
            or receipt["evidence_domain"] != PHASE9_P0_FORMAL_DOMAIN
            or receipt["suite_id"] != PHASE9_P0_SUITE_ID
            or receipt["requirement"] != requirement
            or receipt["status"] != "PASS"
            or receipt["spec_sha256"] != phase9_p0_spec_sha256()
            or receipt["source_inventory_sha256"] != expected_inventory
            or receipt["producer"] != expected_producer
            or receipt["test_nodes"] != list(PHASE9_P0_TEST_NODES[requirement])
            or receipt["evidence"] != evidence_expected
            or receipt["evidence_sha256"] != canonical_sha256(evidence_expected)
        ):
            raise Phase9P0EvidenceError(f"{requirement} formal receipt binding differs")
        if _integer(
            receipt["command_exit_code"],
            f"{requirement}.command_exit_code",
        ) != 0:
            raise Phase9P0EvidenceError(
                f"{requirement} command exit code differs"
            )
        _validate_capabilities(
            receipt["capabilities"], f"{requirement}.capabilities"
        )
        _validate_candidate(receipt["candidate"], expected_candidate, f"{requirement}.candidate")
        _validate_coordinate(receipt["coordinate"], expected_coordinate, f"{requirement}.coordinate")
        _validate_reference(receipt["command_record"], expected_path="command_records/p0_suite.json", files=files, path=f"{requirement}.command_record")
        _validate_reference(receipt["raw_test_log"], expected_path="test_results/p0_suite.log", files=files, path=f"{requirement}.raw_test_log")
        _validate_reference(receipt["test_outcome"], expected_path="test_outcomes/p0_suite.json", files=files, path=f"{requirement}.test_outcome")
        _validate_reference(receipt["trusted_events"], expected_path="test_events/p0_suite.jsonl", files=files, path=f"{requirement}.trusted_events")
        receipt_hashes[requirement] = _self_hash(receipt, "receipt_sha256", f"p0_receipts.{requirement}")
    return ValidatedPhase9P0Evidence(
        receipt_sha256s=receipt_hashes,
        authority_runner=authority_runner,
        consumption_receipt_sha256=authority_runner_sha,
        authority_runner_evidence_sha256=hashlib.sha256(
            files["attestations/authority_runner.json"]
        ).hexdigest(),
        command_record_sha256=_sha(
            command["command_record_sha256"], "command_record.command_record_sha256"
        ),
        raw_log_byte_length=len(log_raw),
        raw_log_sha256=hashlib.sha256(log_raw).hexdigest(),
        junit_byte_length=len(report_raw),
        junit_sha256=hashlib.sha256(report_raw).hexdigest(),
        outcome_sha256=_sha(outcome["outcome_sha256"], "test_outcome.outcome_sha256"),
        started_at=started,
        finished_at=finished,
    )


def _receipt(body: Mapping[str, object], field: str) -> dict[str, object]:
    value = dict(body)
    value[field] = canonical_sha256(value)
    return value


def _trusted_authority_connection(database: str | Path) -> sqlite3.Connection:
    from .authority_production_schema import authority_database_path, connect_authority_rw

    connection = connect_authority_rw(authority_database_path(database))
    connection.create_function("phase9_p0_write_capability", 0, lambda: 1)
    return connection


def _generation_execution_context(
    connection: sqlite3.Connection,
    *,
    workflow_id: str,
    run_generation: str,
    request_sha256: str,
    expected_receipt_sha256: str,
) -> dict[str, object]:
    """Read the already-validated generation's exact context from Authority."""

    row = connection.execute(
        "SELECT request_sha256, receipt_json FROM "
        "authority_production_run_generation_creation_receipts "
        "WHERE workflow_id=? AND run_generation=?",
        (workflow_id, run_generation),
    ).fetchone()
    if row is None or row["request_sha256"] != request_sha256:
        raise Phase9P0EvidenceError("generation execution context evidence is missing")
    try:
        receipt = json.loads(str(row["receipt_json"]))
    except json.JSONDecodeError as exc:
        raise Phase9P0EvidenceError(
            "generation execution context evidence is malformed"
        ) from exc
    if type(receipt) is not dict or canonical_bytes(receipt).decode("utf-8") != row[
        "receipt_json"
    ]:
        raise Phase9P0EvidenceError(
            "generation execution context receipt is not canonical"
        )
    request = receipt.get("request")
    if type(request) is not dict or canonical_sha256(request) != request_sha256:
        raise Phase9P0EvidenceError("generation request evidence differs")
    context = request.get("execution_context")
    if type(context) is not dict or canonical_sha256(context) != expected_receipt_sha256:
        raise Phase9P0EvidenceError("generation execution context receipt differs")
    return dict(context)


def produce_formal_phase9_p0_evidence(
    *,
    authority_database: str | Path,
    expected_source_fence_sha256: str,
    source_repository: str | Path,
    evidence_root: str | Path,
    python_executable: str | Path,
    workflow_id: str,
) -> Phase9P0EvidenceBundle:
    """Run the sole supported formal producer and persist its Authority attestation.

    The caller supplies locations and a workflow only.  Candidate identity,
    project/run coordinate, acceptance nodes, command shape, nonce, OS identity,
    and all receipt identities are derived and consumed by this trusted path.
    """

    from .phase9_entry import (
        CandidateIdentity,
        collect_phase9_entry_state_in_transaction,
    )

    now = lambda: time.time_ns() // 1_000_000_000
    source_root = Path(os.path.abspath(os.fspath(source_repository)))
    root = Path(os.path.abspath(os.fspath(evidence_root)))
    if root.exists() or root.is_symlink():
        raise Phase9P0EvidenceError("formal evidence root must not already exist")
    if not root.parent.exists() or root.parent.is_symlink() or not root.parent.is_dir():
        raise Phase9P0EvidenceError("formal evidence parent must be an existing directory")
    _require_pristine_formal_source(source_root)
    source_snapshot = read_current_git_source_snapshot(source_root)
    candidate_dict = _candidate_from_snapshot(source_snapshot)
    candidate = CandidateIdentity(
        candidate_dict["commit"], candidate_dict["tree"], candidate_dict["parent"]
    )
    runtime = _trusted_python_runtime_identity(python_executable, source_root)
    python = runtime["python"]
    sandbox = _python_identity("/usr/bin/bwrap")
    producer = _producer_descriptor(source_snapshot, sandbox)
    operator_uid = os.geteuid()
    try:
        operator_account = pwd.getpwuid(operator_uid).pw_name
    except KeyError as exc:
        raise Phase9P0EvidenceError("controlled runner OS account is unavailable") from exc

    connection = _trusted_authority_connection(authority_database)
    try:
        connection.execute("BEGIN IMMEDIATE")
        state = collect_phase9_entry_state_in_transaction(
            connection,
            expected_source_fence_sha256=expected_source_fence_sha256,
            workflow_id=workflow_id,
            candidate=candidate,
        )
        if state.p0_runner_attestation is not None:
            raise Phase9P0EvidenceError(
                "this run generation already has a successful P0 runner attestation"
            )
        if (
            state.source_inventory_sha256 != source_snapshot.source_inventory_sha256
            or state.run_mode != "FORENSIC_REPLAY"
            or state.modeling_consultation_contract != "LEGACY_NOT_APPLICABLE"
            or state.delivery_capability != "DISABLED"
            or state.writer_switch_mode != "V1_ONLY"
            or state.writer_enabled
            or state.consumer_enabled
            or state.active_process_count != 0
            or state.pending_outbox_count != 0
            or state.unresolved_migration_count != 0
            or state.old_generation_post_boundary_event_count != 0
            or not state.old_generation_read_only_guards_verified
        ):
            raise Phase9P0EvidenceError(
                "Authority live state is not eligible for the fixed P0 runner"
            )
        generation_context = _generation_execution_context(
            connection,
            workflow_id=state.workflow_id,
            run_generation=state.run_generation,
            request_sha256=state.request_sha256,
            expected_receipt_sha256=state.execution_context_receipt_sha256,
        )
        execution_context_binding = _bind_generation_execution_context(
            generation_context,
            runtime=runtime,
            sandbox=sandbox,
            producer=producer,
        )
        coordinate = _coordinate(
            state.project_id, state.workflow_id, state.run_generation
        )
        issued_at = _integer(now(), "clock.issued_at")
        expires_at = issued_at + PHASE9_P0_RUNNER_AUTHORIZATION_TTL_SECONDS
        nonce = secrets.token_hex(32)
        nonce_sha256 = hashlib.sha256(nonce.encode("utf-8")).hexdigest()
        authorization_id = f"p0-auth-{secrets.token_hex(16)}"
        invocation_id = f"p0-invocation-{secrets.token_hex(16)}"
        python_identity_sha256 = str(runtime["runtime_sha256"])
        authorization = _receipt(
            {
                "schema": PHASE9_P0_RUNNER_AUTHORIZATION_SCHEMA,
                "evidence_domain": PHASE9_P0_FORMAL_DOMAIN,
                "authorization_id": authorization_id,
                "nonce_sha256": nonce_sha256,
                "candidate": candidate_dict,
                "coordinate": coordinate,
                "source_inventory_sha256": state.source_inventory_sha256,
                "live_binding_sha256": state.runner_live_binding_sha256,
                "spec_sha256": phase9_p0_spec_sha256(),
                "producer": producer,
                "operator_uid": operator_uid,
                "operator_account": operator_account,
                "issued_at": issued_at,
                "expires_at": expires_at,
                "intended_evidence_root": str(root),
                "python_identity_sha256": python_identity_sha256,
                "execution_context_binding": execution_context_binding,
                "capabilities": dict(_CAPABILITIES),
            },
            "authorization_receipt_sha256",
        )
        authorization_receipt_sha256 = str(
            authorization["authorization_receipt_sha256"]
        )
        connection.execute(
            """
            INSERT INTO authority_production_phase9_p0_runner_authorizations(
                authorization_id, nonce_sha256, project_id, workflow_id,
                run_generation, source_commit, source_tree, source_parent,
                source_inventory_sha256, live_binding_sha256, spec_sha256,
                operator_uid, operator_account, issued_at, expires_at,
                intended_evidence_root, python_identity_sha256,
                authorization_json, authorization_receipt_sha256
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                authorization_id,
                nonce_sha256,
                state.project_id,
                state.workflow_id,
                state.run_generation,
                candidate.commit,
                candidate.tree,
                candidate.parent,
                state.source_inventory_sha256,
                state.runner_live_binding_sha256,
                phase9_p0_spec_sha256(),
                operator_uid,
                operator_account,
                issued_at,
                expires_at,
                str(root),
                python_identity_sha256,
                canonical_bytes(authorization).decode("utf-8"),
                authorization_receipt_sha256,
            ),
        )
        consumed_at = _integer(now(), "clock.consumed_at")
        if not issued_at <= consumed_at <= expires_at:
            raise Phase9P0EvidenceError(
                "P0 authorization expired before it could be consumed"
            )
        authority_runner = _receipt(
            {
                "schema": PHASE9_P0_AUTHORITY_RUNNER_EVIDENCE_SCHEMA,
                "evidence_domain": PHASE9_P0_FORMAL_DOMAIN,
                "authorization_id": authorization_id,
                "authorization_receipt_sha256": authorization_receipt_sha256,
                "nonce": nonce,
                "nonce_sha256": nonce_sha256,
                "invocation_id": invocation_id,
                "candidate": candidate_dict,
                "coordinate": coordinate,
                "source_inventory_sha256": state.source_inventory_sha256,
                "live_binding_sha256": state.runner_live_binding_sha256,
                "spec_sha256": phase9_p0_spec_sha256(),
                "producer": producer,
                "operator_uid": operator_uid,
                "operator_account": operator_account,
                "issued_at": issued_at,
                "expires_at": expires_at,
                "consumed_at": consumed_at,
                "intended_evidence_root": str(root),
                "python_identity_sha256": python_identity_sha256,
                "execution_context_binding": execution_context_binding,
                "capabilities": dict(_CAPABILITIES),
            },
            "evidence_sha256",
        )
        consumption_receipt_sha256 = str(authority_runner["evidence_sha256"])
        connection.execute(
            """
            INSERT INTO authority_production_phase9_p0_runner_consumptions(
                authorization_id, nonce_sha256, invocation_id, consumed_at,
                consumption_json, consumption_receipt_sha256
            ) VALUES(?,?,?,?,?,?)
            """,
            (
                authorization_id,
                nonce_sha256,
                invocation_id,
                consumed_at,
                canonical_bytes(authority_runner).decode("utf-8"),
                consumption_receipt_sha256,
            ),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    bundle = _execute_fixed_phase9_p0_suite(
        source_repository=source_root,
        evidence_root=root,
        python_executable=python_executable,
        project_id=state.project_id,
        workflow_id=state.workflow_id,
        run_generation=state.run_generation,
        authority_runner=authority_runner,
    )
    files = {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }
    validated = validate_formal_phase9_p0_evidence(
        receipts=bundle.receipts,
        files=files,
        candidate=candidate_dict,
        coordinate=coordinate,
        source_inventory_sha256=state.source_inventory_sha256,
    )

    connection = _trusted_authority_connection(authority_database)
    try:
        connection.execute("BEGIN IMMEDIATE")
        state_after = collect_phase9_entry_state_in_transaction(
            connection,
            expected_source_fence_sha256=expected_source_fence_sha256,
            workflow_id=workflow_id,
            candidate=candidate,
        )
        attested_at = _integer(now(), "clock.attested_at")
        if (
            state_after.p0_runner_attestation is not None
            or state_after.runner_live_binding_sha256
            != state.runner_live_binding_sha256
            or not (
                issued_at
                <= consumed_at
                <= validated.started_at
                <= validated.finished_at
                <= attested_at
                <= expires_at
            )
            or os.geteuid() != operator_uid
            or pwd.getpwuid(os.geteuid()).pw_name != operator_account
            or read_current_git_source_snapshot(source_root) != source_snapshot
            or _trusted_python_runtime_identity(
                python_executable, source_root
            ) != runtime
            or _producer_descriptor(source_snapshot, sandbox) != producer
        ):
            raise Phase9P0EvidenceError(
                "Authority/source/runner identity changed before P0 attestation"
            )
        attestation = _receipt(
            {
                "schema": PHASE9_P0_RUNNER_ATTESTATION_SCHEMA,
                "evidence_domain": PHASE9_P0_FORMAL_DOMAIN,
                "authorization_id": authorization_id,
                "authorization_receipt_sha256": authorization_receipt_sha256,
                "consumption_receipt_sha256": consumption_receipt_sha256,
                "authority_runner_evidence_sha256": (
                    validated.authority_runner_evidence_sha256
                ),
                "invocation_id": invocation_id,
                "candidate": candidate_dict,
                "coordinate": coordinate,
                "source_inventory_sha256": state.source_inventory_sha256,
                "live_binding_sha256": state.runner_live_binding_sha256,
                "spec_sha256": phase9_p0_spec_sha256(),
                "execution_context_binding": execution_context_binding,
                "operator_uid": operator_uid,
                "operator_account": operator_account,
                "issued_at": issued_at,
                "expires_at": expires_at,
                "consumed_at": consumed_at,
                "started_at": validated.started_at,
                "finished_at": validated.finished_at,
                "attested_at": attested_at,
                "exit_code": 0,
                "evidence_root_sha256": bundle.evidence_root_sha256,
                "command_record_sha256": validated.command_record_sha256,
                "raw_log_byte_length": validated.raw_log_byte_length,
                "raw_log_sha256": validated.raw_log_sha256,
                "junit_byte_length": validated.junit_byte_length,
                "junit_sha256": validated.junit_sha256,
                "outcome_sha256": validated.outcome_sha256,
                "receipt_sha256s": validated.receipt_sha256s,
                "receipt_set_sha256": validated.receipt_set_sha256,
                "capabilities": dict(_CAPABILITIES),
            },
            "attestation_sha256",
        )
        attestation_sha256 = str(attestation["attestation_sha256"])
        connection.execute(
            """
            INSERT INTO authority_production_phase9_p0_runner_attestations(
                authorization_id, authorization_receipt_sha256,
                consumption_receipt_sha256, authority_runner_evidence_sha256,
                invocation_id, project_id, workflow_id, run_generation,
                source_inventory_sha256, live_binding_sha256, spec_sha256,
                evidence_root_sha256, command_record_sha256,
                raw_log_byte_length, raw_log_sha256, junit_byte_length,
                junit_sha256, outcome_sha256, receipt_set_sha256, started_at,
                finished_at, attested_at, exit_code, attestation_json,
                attestation_sha256
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                authorization_id,
                authorization_receipt_sha256,
                consumption_receipt_sha256,
                validated.authority_runner_evidence_sha256,
                invocation_id,
                state.project_id,
                state.workflow_id,
                state.run_generation,
                state.source_inventory_sha256,
                state.runner_live_binding_sha256,
                phase9_p0_spec_sha256(),
                bundle.evidence_root_sha256,
                validated.command_record_sha256,
                validated.raw_log_byte_length,
                validated.raw_log_sha256,
                validated.junit_byte_length,
                validated.junit_sha256,
                validated.outcome_sha256,
                validated.receipt_set_sha256,
                validated.started_at,
                validated.finished_at,
                attested_at,
                0,
                canonical_bytes(attestation).decode("utf-8"),
                attestation_sha256,
            ),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return replace(bundle, authority_attestation_sha256=attestation_sha256)
