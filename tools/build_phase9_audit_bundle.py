#!/usr/bin/env python3
"""Build and independently verify a deterministic Phase9 Pro audit ZIP."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tempfile
import unicodedata
import zipfile
import stat

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from factory_core.canonical import canonical_bytes, canonical_sha256
from tools.build_phase9_test_summary import (
    PHASE9_REQUIRED_SUITE_SPECS,
    _build_summary_for_policy,
    _stable_regular_bytes,
)
from tools.run_audit_command import INVENTORY_SCHEMA, executed_source_inventory
from tools.run_full_repo_with_frontend_deps import composite_stage_contract


FIXED_ZIP_TIMESTAMP = (2026, 1, 1, 0, 0, 0)
NORMALIZED_MODE = 0o100644
MANIFEST_SCHEMA = "paper-factory-phase9-audit-manifest-v1"
IDENTITY_SCHEMA = "paper-factory-phase9-candidate-identity-v1"
PRODUCTION_STATUS_SCHEMA = "paper-factory-phase9-production-status-v1"
REVIEW_STATUS_SCHEMA = "paper-factory-phase9-review-status-v1"
AUDITED_BASELINE_IDENTITY = {
    "commit": "2de2f29d25970c2a3cefa4f674fc53894d782f57",
    "tree": "6f911507c16fbec1d80eca345bf2133ce50ede9d",
    "parent": "f7a2eb85a90730639166f35e4deae708d4762d00",
}
PRODUCTION_BLOCK_REASON = (
    "production authority database, official inputs, credentials, runtime state, "
    "and live operator authorization are intentionally absent from the audit package"
)
REVIEW_PATHS = {
    "review/FIX_CLOSURE.md",
    "review/VALIDATION_NOTES.md",
    "review/PRO_AUDIT_PROMPT_ZH.md",
    "review/REVIEW_STATUS.json",
}
FULL_REPOSITORY_BROWSER_TEST_PATHS = frozenset(
    str(target)
    for stage in composite_stage_contract()
    if stage["id"] == "phase6_browser"
    for target in stage["targets"]
)
_HEX40 = re.compile(r"[0-9a-f]{40}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")

PACKAGE_README = (
    "# Paper Factory Phase9 Pro audit package\n\n"
    "This deterministic, single-root package is bound to the candidate in "
    "`identity/CANDIDATE_IDENTITY.json`. It contains a full tracked-file "
    "inventory, a frozen source subset, exact test commands/raw logs/statistics, "
    "parent-captured Python/build/browser stage records and ordered browser-node "
    "outcomes, exact frontend scripts, safe production-build output inventories, "
    "byte-bound Node/npm identities, and Node dependency/browser runtime inventories, "
    "plus process-not-started preflight failures without synthetic stage evidence, "
    "requirements/gaps, receipt contracts and schemas, tests, offline evidence, "
    "and an honestly BLOCKED production status. It contains no real production "
    "receipts. It contains no production database, official input, credentials, "
    "caches, dependencies, runtime state, generated paper, or nested archive. "
    "A review PASS does not authorize migration, provider/network use, outbox, "
    "delivery, release, deployment, or cutover.\n"
).encode("utf-8")


def _validate_freeze_utc(value: object) -> str:
    if type(value) is not str or re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value
    ) is None:
        raise RuntimeError("freeze_utc is not canonical second-precision UTC")
    parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    if parsed.tzinfo != UTC:
        raise RuntimeError("freeze_utc is not UTC")
    return value
DEFAULT_SOURCE_PATHS = (
    "AGENTS.md", "CHANGELOG.md", "CLAUDE.md", "DOCUMENTATION_INDEX.md",
    "README.md", "STEPS.md",
    "docs/architecture/PHASE2_PRODUCTION_AUTHORITY_FOUNDATION.md",
    "docs/architecture/PHASE7_8_DURABLE_FULL_SHADOW.md",
    "docs/operations/PHASE9_ENTRY_GATE.md",
    "docs/operations/PHASE9_GAP_MATRIX.md",
    "docs/operations/PHASE9_IMPLEMENTATION_AND_ROLLBACK.md",
    "docs/operations/PHASE9_PREP_RUNBOOK.md",
    "docs/operations/PHASE9_REQUIREMENT_IMPLEMENTATION_TEST_EVIDENCE_MAP.tsv",
    "docs/operations/PHASE9_TEST_EVIDENCE_MATRIX.md",
    "docs/operations/PHASE9_TEST_SUITE_CONTRACT.json",
    "docs/operations/PHASE9_FORENSIC_EVIDENCE.template.json",
    "tests/fixtures/m03_persisted_dirty_owner_policy/golden_identity.json",
    "tests/fixtures/m03_contract_pins/golden_identity.json",
    "tests/fixtures/m03_snapshot_v0/golden_identity.json",
    "tests/fixtures/m03_command_envelope/golden_identity.json",
    "tests/support/m03_identity_replay.py", "tests/test_m03_contract_pins.py", "tests/test_m03_command_envelope.py",
    "factory_core/persisted_dirty_owner_implementation_manifest.py",
    "tests/test_m03_persisted_dirty_owner_policy.py", "tests/support/m03_symbol_manifest.py",
    "factory_core/authority_operations.py",
    "factory_core/authority_operator_workflow.py",
    "factory_core/authority_outbox_delivery.py",
    "factory_core/authority_production_schema.py",
    "factory_core/authority_read_repository.py", "factory_core/engine.py",
    "factory_core/cli.py",
    "factory_core/service.py",
    "factory_core/delivery/release.py",
    "factory_core/audit/acceptance.py", "factory_core/audit/service.py",
    "factory_core/adapters/legacy_runner.sh",
    "factory_core/adapters/infrastructure/process.py", "factory_core/adapters/models/backends.py",
    "scripts/judge_packet.py", "modeling_guide.md",
    "factory_core/phase9_delivery_fence.py",
    "factory_core/phase9_authority_lease.py",
    "factory_core/phase9_config.py", "factory_core/phase9_entry.py",
    "factory_core/phase9_forensic_replay.py",
    "factory_core/phase9_replay_evidence.py",
    "factory_core/phase9_run_generation.py",
    "factory_core/phase9_p0_evidence.py", "factory_core/steps/specialized.py",
    "factory_core/phase5_shadow_supervisor.py", "factory_core/phase78_worker.py",
    "scripts/audit_complete_projects.py", "scripts/authority_operator.py",
    "scripts/phase9_entry_gate.py",
    "scripts/phase9_forensic_replay.py", "scripts/phase9_prep_manifest.py",
    "scripts/check_phase9_delivery_fence.py", "scripts/publish_release.py",
    "scripts/package_submission.py",
    "scripts/workflow_state.py", "scripts/evaluate_modeling_project.py",
    "scripts/delivery_contract.py",
    "tools/run_phase9_p0_evidence.py",
    "tests/test_atomic_release.py", "tests/test_authority_operations.py",
    "tests/test_audit_service.py", "tests/test_authority_outbox_delivery.py",
    "tests/test_authority_production_migration.py",
    "tests/test_contest_dashboard.py", "tests/test_delivery_contract.py",
    "tests/test_m01_runtime_parity.py",
    "tests/test_native_orchestration.py", "tests/test_phase5_shadow_supervisor.py",
    "tests/test_phase1_8_durable_continuous_chain.py",
    "tests/test_phase78_enabled_e2e.py", "tests/test_phase78_bootstrap_contract.py",
    "tests/test_phase9_entry_gate.py",
    "tests/test_phase9_acceptance_probes.py",
    "tests/test_phase9_forensic_replay.py",
    "tests/test_phase9_runtime_authority.py", "tests/test_phase9_runtime.py",
    "tests/test_json_evidence_view.py", "scripts/json_evidence_view.py",
    "scripts/phase9_authorized_runtime.py", "scripts/phase9_runtime.py",
    "factory_core/phase9_runtime.py", "factory_core/phase9_runtime_authority.py",
    "factory_core/phase9_runtime_coordinator.py", "factory_core/phase9_runtime_probes.py",
    "factory_core/phase9_runtime_receipts.py",
    "factory_core/phase9_provider_identity.py", "factory_core/phase9_runtime_export.py",
    "factory_core/phase9_provider_sandbox.py", "factory_core/phase9_provider_gate.py",
    "tests/test_phase9_prep_manifest.py", "tests/test_phase9_run_generation.py",
    "tests/test_phase9_delivery_fence.py", "tests/test_phase9_p0_evidence.py",
    "tests/test_phase9_audit_bundle.py", "tests/test_phase9_audit_evidence.py",
    "tests/test_package_submission.py",
    "tests/test_workflow_state.py", "tools/build_phase9_audit_bundle.py",
    "tests/test_dirty_and_finalization.py",
    "tests/test_quality_gates_regression.py",
    "tools/build_phase9_test_summary.py", "tools/run_audit_command.py",
    "tools/run_full_repo_with_frontend_deps.py",
    "tools/phase9_composite_evidence.py",
    "tools/trusted_pytest_reporter.py",
    "web/README.md", "web/frontend/package.json",
    "web/frontend/package-lock.json",
    "web/frontend/tests/phase6-controller.test.mjs",
    "web/frontend/tests/phase6-build-browser.test.mjs",
    "web/frontend/tests/phase6-panel.harness.html",
    "web/backend/project_api.py", "web/backend/contest_dashboard.py",
    "web/backend/showcase.py",
)


def _git(repository: Path, *args: str, input_bytes: bytes | None = None) -> bytes:
    return subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", *args],
        cwd=repository, input=input_bytes, check=True,
        stdin=subprocess.DEVNULL if input_bytes is None else None,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env={
            "PATH": "/usr/bin:/bin",
            "LC_ALL": "C",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
        },
    ).stdout


def _identity(repository: Path, commit: str) -> dict[str, str]:
    resolved = _git(repository, "rev-parse", f"{commit}^{{commit}}").decode().strip()
    tree = _git(repository, "rev-parse", f"{resolved}^{{tree}}").decode().strip()
    parents = _git(repository, "show", "-s", "--format=%P", resolved).decode().split()
    if len(parents) != 1:
        raise RuntimeError("candidate must have exactly one parent")
    return {"commit": resolved, "tree": tree, "parent": parents[0]}


def _safe(path: str) -> str:
    pure = PurePosixPath(path)
    if (
        not path or pure.is_absolute() or pure.as_posix() != path
        or ".." in pure.parts or "\\" in path or "\x00" in path
        or unicodedata.normalize("NFC", path) != path
        or any(ord(character) < 32 or ord(character) == 127 for character in path)
        or any(part.endswith((".", " ")) for part in pure.parts)
    ):
        raise RuntimeError(f"unsafe package path: {path!r}")
    return path


def _candidate_inventory(repository: Path, commit: str) -> tuple[bytes, set[str]]:
    raw = _git(repository, "ls-tree", "-rz", "--full-tree", commit)
    entries = []
    paths: set[str] = set()
    folded_paths: set[str] = set()
    blob_oids = []
    for record in raw.split(b"\0"):
        if not record:
            continue
        header, raw_path = record.split(b"\t", 1)
        mode, kind, oid = header.decode("ascii").split()
        path = _safe(raw_path.decode("utf-8", errors="strict"))
        if path in paths:
            raise RuntimeError(f"duplicate Git path: {path}")
        folded = unicodedata.normalize("NFC", path).casefold()
        if folded in folded_paths:
            raise RuntimeError(f"casefold/Unicode-colliding Git path: {path}")
        paths.add(path)
        folded_paths.add(folded)
        if kind == "blob":
            if mode not in {"100644", "100755"}:
                raise RuntimeError(f"unsupported Git blob mode: {path}: {mode}")
            blob_oids.append(oid)
        elif kind != "commit" or mode != "160000":
            raise RuntimeError(f"unsupported recursive Git object kind: {path}: {kind}")
        entries.append((path, mode, kind, oid))
    query = b"".join(f"{oid}\n".encode() for oid in blob_oids)
    sizes_raw = _git(
        repository, "cat-file", "--batch-check=%(objectname) %(objecttype) %(objectsize)",
        input_bytes=query,
    )
    sizes = {}
    for line in sizes_raw.decode("ascii").splitlines():
        oid, kind, size = line.split()
        sizes[oid] = (kind, int(size))
    lines = ["path\tmode\ttype\tobject_id\tbytes"]
    for path, mode, kind, oid in sorted(entries):
        if kind == "blob":
            observed_kind, size = sizes[oid]
            if observed_kind != kind:
                raise RuntimeError(f"Git object kind differs: {path}")
            size_value = str(size)
        else:
            size_value = "-"
        lines.append(f"{path}\t{mode}\t{kind}\t{oid}\t{size_value}")
    return ("\n".join(lines) + "\n").encode(), paths


def _blob(repository: Path, commit: str, path: str) -> bytes:
    return _git(repository, "cat-file", "blob", f"{commit}:{path}")


def _artifact_inventory(audit_root: Path) -> dict[str, Path]:
    """Return the complete package-eligible artifact inventory, fail closed."""

    roots = ("command_records", "evidence", "receipts", "review", "test_logs")
    result: dict[str, Path] = {}
    collisions: set[str] = set()

    def traversal_error(error: OSError) -> None:
        raise RuntimeError("audit artifact tree cannot be enumerated") from error

    for root_name in roots:
        root = audit_root / root_name
        if not root.exists() and not root.is_symlink():
            continue
        root_metadata = root.lstat()
        if not stat.S_ISDIR(root_metadata.st_mode):
            raise RuntimeError(f"audit artifact root is not an ordinary directory: {root_name}")
        directory_identities: dict[Path, tuple[int, ...]] = {}
        for current, directories, files in os.walk(
            root, topdown=True, followlinks=False, onerror=traversal_error
        ):
            current_path = Path(current)
            current_metadata = current_path.lstat()
            if not stat.S_ISDIR(current_metadata.st_mode):
                raise RuntimeError("audit artifact directory changed during traversal")
            directory_identities[current_path] = (
                current_metadata.st_dev, current_metadata.st_ino,
                current_metadata.st_mode, current_metadata.st_nlink,
                current_metadata.st_mtime_ns, current_metadata.st_ctime_ns,
            )
            for name in directories:
                item = current_path / name
                metadata = item.lstat()
                if not stat.S_ISDIR(metadata.st_mode):
                    raise RuntimeError("audit evidence contains a linked/special directory")
            for name in files:
                item = current_path / name
                relative = _safe(item.relative_to(audit_root).as_posix())
                metadata = item.lstat()
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                    raise RuntimeError(
                        f"audit evidence contains a link, hardlink, or special file: {relative}"
                    )
                folded = unicodedata.normalize("NFC", relative).casefold()
                if folded in collisions:
                    raise RuntimeError("audit evidence has a casefold/Unicode collision")
                collisions.add(folded)
                lowered_parts = [part.casefold() for part in PurePosixPath(relative).parts]
                forbidden_suffixes = (
                    ".db", ".sqlite", ".sqlite3", ".db-wal", ".db-shm",
                    ".wal", ".shm", ".journal", ".zip", ".tar", ".tgz",
                    ".gz", ".7z", ".pyc",
                )
                if (
                    "__pycache__" in lowered_parts
                    or any(part in {".env", "credentials", "secrets"} for part in lowered_parts)
                    or relative.casefold().endswith(forbidden_suffixes)
                ):
                    raise RuntimeError(f"forbidden audit evidence path: {relative}")
                result[relative] = item
        for directory, before in directory_identities.items():
            after = directory.lstat()
            observed = (
                after.st_dev, after.st_ino, after.st_mode, after.st_nlink,
                after.st_mtime_ns, after.st_ctime_ns,
            )
            if observed != before:
                raise RuntimeError("audit artifact directory changed during traversal")
    return result


def _expected_evidence_paths(summary: dict[str, object]) -> set[str]:
    expected = {
        "evidence/FINAL_TEST_SUMMARY.json",
        "evidence/PRODUCTION_STATUS.json",
        *REVIEW_PATHS,
    }
    records = [*summary["records"], *summary["failed_attempts"]]
    for record in records:
        expected.add(str(record["command_record_path"]))
        expected.add(str(record["raw_log"]["path"]))
        expected.add(str(record["source_inventory"]["path"]))
        if record["dependency_inventory"] is not None:
            expected.add(str(record["dependency_inventory"]["path"]))
        if record["trusted_pytest"] is not None:
            expected.add(str(record["trusted_pytest"]["event_artifact"]["path"]))
        if record["composite_suite"] is not None:
            expected.add(
                str(record["composite_suite"]["event_artifact"]["path"])
            )
    return expected


def _copy_audit_evidence(
    audit_root: Path, payload: dict[str, bytes], expected_paths: set[str]
) -> None:
    actual = _artifact_inventory(audit_root)
    if set(actual) != expected_paths:
        raise RuntimeError(
            "audit evidence closure differs: "
            f"missing={sorted(expected_paths-set(actual))}, "
            f"extra={sorted(set(actual)-expected_paths)}"
        )
    for relative in sorted(expected_paths):
        raw = _stable_regular_bytes(actual[relative], f"audit evidence {relative}")
        if not raw:
            raise RuntimeError(f"audit evidence is empty: {relative}")
        payload[relative] = raw


def _verify_production_status(
    production: dict[str, object], identity: dict[str, str]
) -> None:
    scope_keys = {
        "migration", "network_or_provider", "outbox_dispatch", "delivery",
        "release", "deployment", "cutover",
    }
    if set(production) != {
        "schema", "candidate_commit", "status", "reason", "authorization_scope"
    }:
        raise RuntimeError("production status keys differ")
    scope = production.get("authorization_scope")
    if (
        production.get("schema") != PRODUCTION_STATUS_SCHEMA
        or production.get("candidate_commit") != identity["commit"]
        or production.get("status") != "BLOCKED"
        or production.get("reason") != PRODUCTION_BLOCK_REASON
        or type(scope) is not dict
        or set(scope) != scope_keys
        or any(scope.get(name) is not False for name in scope_keys)
    ):
        raise RuntimeError("production status is not the exact fail-closed contract")


def build_review_status(
    identity: dict[str, str], summary: dict[str, object]
) -> dict[str, object]:
    """Derive the machine-readable review/stage boundary from verified facts."""

    body: dict[str, object] = {
        "schema": REVIEW_STATUS_SCHEMA,
        "candidate": identity,
        "final_test_summary_sha256": summary["summary_sha256"],
        "required_suite_count": summary["required_suite_count"],
        "final_record_count": summary["final_record_count"],
        "failed_attempt_count": summary["failed_attempt_count"],
        "source_fresh_exact": summary["source_fresh_exact"],
        "production": "BLOCKED",
        "production_migrations_a2_0016_through_a2_0019": "NOT_APPLIED",
        "formal_phase9_a": "NOT_RUN",
        "run4_forensic_replay": "NOT_RUN",
        "phase9": "NOT_COMPLETE",
        "phase10_b": "NOT_STARTED",
        "independent_audit": "PENDING",
    }
    body["review_status_sha256"] = canonical_sha256(body)
    return body


def _verify_review_material(
    root: Path, identity: dict[str, str], summary: dict[str, object]
) -> None:
    status, raw = _read_canonical_json(
        root / "review/REVIEW_STATUS.json", "review status"
    )
    expected = build_review_status(identity, summary)
    if raw != canonical_bytes(expected) + b"\n" or status != expected:
        raise RuntimeError("review status is not derived from candidate/test state")

    markers = (
        f"Candidate commit: `{identity['commit']}`",
        f"Candidate tree: `{identity['tree']}`",
        f"Candidate parent: `{identity['parent']}`",
        f"Final test summary: `{summary['summary_sha256']}`",
        "Production: `BLOCKED`",
        "A2_0016-A2_0019: `NOT APPLIED`",
        "Formal Phase9-A: `NOT RUN`",
        "Run4 forensic replay: `NOT RUN`",
        "Phase 9: `NOT COMPLETE`",
        "Phase10-B: `NOT STARTED`",
        "Independent audit: `PENDING`",
    )
    allowed_pass_counts = {
        int(record["outcomes"]["passed"])
        for record in summary["records"]
    }
    for relative in (
        "review/FIX_CLOSURE.md",
        "review/VALIDATION_NOTES.md",
        "review/PRO_AUDIT_PROMPT_ZH.md",
    ):
        raw_markdown = _stable_regular_bytes(root / relative, relative)
        try:
            text = raw_markdown.decode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise RuntimeError(f"review document is not UTF-8: {relative}") from exc
        if not text.endswith("\n") or any(marker not in text for marker in markers):
            raise RuntimeError(
                f"review document lacks the verified status block: {relative}"
            )
        claimed_counts = {
            int(value)
            for value in re.findall(r"(?<![0-9])(\d+) passed(?![A-Za-z])", text)
        }
        if not claimed_counts.issubset(allowed_pass_counts):
            raise RuntimeError(
                f"review document has an unbound passed-count claim: {relative}"
            )


def _references(value: str, label: str) -> list[str]:
    if type(value) is not str:
        raise RuntimeError(f"requirement {label} is not text")
    result = value.split(";") if value else []
    if any(not item for item in result) or len(result) != len(set(result)):
        raise RuntimeError(f"requirement {label} references are empty or duplicated")
    for item in result:
        _safe(item)
    return result


def _full_repository_executes_test_path(path: str) -> bool:
    """Recognize only tests reached by the two full-repository test stages."""

    if path in FULL_REPOSITORY_BROWSER_TEST_PATHS:
        return True
    pure = PurePosixPath(path)
    name = pure.name
    return bool(
        len(pure.parts) > 1
        and pure.parts[0] == "tests"
        and name.endswith(".py")
        and (name.startswith("test_") or name.endswith("_test.py"))
    )


def _verify_requirement_map(
    mapping: bytes,
    *,
    candidate_paths: set[str],
    frozen_paths: set[str],
    audit_root: Path,
    summary: dict[str, object],
    suite_specs: dict[str, dict[str, object]],
) -> None:
    try:
        text = mapping.decode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise RuntimeError("requirement evidence map is not UTF-8") from exc
    if not text.endswith("\n") or "\r" in text:
        raise RuntimeError("requirement evidence map newline form differs")
    lines = text.splitlines()
    columns = [
        "requirement_id", "status", "implementation", "tests",
        "command_records", "raw_logs", "summary_evidence",
    ]
    if not lines or lines[0].split("\t") != columns:
        raise RuntimeError("requirement evidence map columns differ")

    final_records = {
        str(item["command_record_path"]): item for item in summary["records"]
    }
    expected_requirements = {
        str(requirement)
        for suite in suite_specs.values()
        for requirement in suite["requirements"]
    } | {"P9-PRODUCTION-RUN"}
    observed_requirements: set[str] = set()
    referenced_final_records: set[str] = set()
    referenced_implementations: set[str] = set()
    referenced_tests: set[str] = set()
    full_repository_tests: set[str] = set()
    full_repository_implementations: set[str] = set()
    for line in lines[1:]:
        fields = line.split("\t")
        if len(fields) != len(columns):
            raise RuntimeError("requirement evidence map row width differs")
        row = dict(zip(columns, fields, strict=True))
        requirement = row["requirement_id"]
        if not requirement or requirement in observed_requirements:
            raise RuntimeError("requirement evidence map IDs are empty or duplicated")
        observed_requirements.add(requirement)
        implementations = _references(row["implementation"], "implementation")
        tests = _references(row["tests"], "tests")
        referenced_implementations.update(implementations)
        referenced_tests.update(tests)
        for reference in (*implementations, *tests):
            if reference not in candidate_paths or reference not in frozen_paths:
                raise RuntimeError(
                    f"requirement file is not candidate-bound and frozen: {reference}"
                )

        command_paths = _references(row["command_records"], "command_records")
        log_paths = _references(row["raw_logs"], "raw_logs")
        summary_paths = _references(row["summary_evidence"], "summary_evidence")
        if requirement == "P9-PRODUCTION-RUN":
            if (
                row["status"] != "BLOCKED_EXTERNAL"
                or command_paths
                or log_paths
                or summary_paths != ["evidence/PRODUCTION_STATUS.json"]
            ):
                raise RuntimeError("production requirement is not honestly blocked")
            continue
        if row["status"] != "CLOSED_OFFLINE":
            raise RuntimeError(f"offline requirement status differs: {requirement}")
        if summary_paths != ["evidence/FINAL_TEST_SUMMARY.json"]:
            raise RuntimeError(f"requirement summary evidence differs: {requirement}")
        if not command_paths or any(path not in final_records for path in command_paths):
            raise RuntimeError(f"requirement command evidence differs: {requirement}")
        records = [final_records[path] for path in command_paths]
        expected_logs = [str(item["raw_log"]["path"]) for item in records]
        if log_paths != expected_logs:
            raise RuntimeError(f"requirement command/log binding differs: {requirement}")
        suites = {str(item["suite"]) for item in records}
        for suite in suites:
            environments = {
                str(item["environment"])
                for item in records
                if item["suite"] == suite
            }
            if environments != {"source", "fresh"}:
                raise RuntimeError(
                    f"requirement lacks a source/fresh record pair: {requirement}"
                )
        if not any(
            requirement in suite_specs[suite]["requirements"] for suite in suites
        ):
            raise RuntimeError(f"no cited suite owns requirement: {requirement}")
        directly_executed = {
            str(target)
            for suite in suites
            if suite != "full_repository"
            for target in suite_specs[suite]["required_targets"]
        }
        full_repository_cited = "full_repository" in suites
        if full_repository_cited:
            full_repository_tests.update(tests)
            full_repository_implementations.update(implementations)
        if any(
            test not in directly_executed
            and not (
                full_repository_cited
                and _full_repository_executes_test_path(test)
            )
            for test in tests
        ):
            raise RuntimeError(
                f"requirement test is not executed by its command records: {requirement}"
            )
        referenced_final_records.update(command_paths)
        for path in (*command_paths, *log_paths, *summary_paths):
            if not (audit_root / path).is_file():
                raise RuntimeError(f"requirement references missing evidence: {path}")

    if observed_requirements != expected_requirements:
        raise RuntimeError(
            "requirement map set differs: "
            f"missing={sorted(expected_requirements-observed_requirements)}, "
            f"extra={sorted(observed_requirements-expected_requirements)}"
        )
    if referenced_final_records != set(final_records):
        raise RuntimeError("final command records are not all reachable from requirements")
    required_targets = {
        str(target)
        for suite in suite_specs.values()
        for target in suite["required_targets"]
    }
    uncovered_test_targets = {
        target
        for target in required_targets
        if target.startswith("tests/") and target not in referenced_tests
    }
    uncovered_tool_targets = {
        target
        for target in required_targets
        if not target.startswith("tests/") and target not in referenced_implementations
    }
    if uncovered_test_targets or uncovered_tool_targets:
        raise RuntimeError(
            "required suite targets are not reachable from requirements: "
            f"tests={sorted(uncovered_test_targets)}, "
            f"tools={sorted(uncovered_tool_targets)}"
        )
    if "full_repository" in suite_specs:
        stage_targets = {
            str(target)
            for stage in suite_specs["full_repository"]["composite_stages"]
            for target in stage["targets"]
        }
        required_stage_tests = {
            target for target in stage_targets
            if _full_repository_executes_test_path(target)
        }
        required_stage_implementations = {
            target for target in stage_targets
            if target != "tests" and target not in required_stage_tests
        }
        missing_stage_tests = required_stage_tests - full_repository_tests
        missing_stage_implementations = (
            required_stage_implementations - full_repository_implementations
        )
        if missing_stage_tests or missing_stage_implementations:
            raise RuntimeError(
                "full-repository stage targets are not reachable from requirements: "
                f"tests={sorted(missing_stage_tests)}, "
                f"implementations={sorted(missing_stage_implementations)}"
            )


def _verify_semantic_evidence(
    repository: Path,
    commit: str,
    audit_root: Path,
    identity: dict[str, str],
    source_paths: tuple[str, ...],
    *,
    suite_specs: dict[str, dict[str, object]],
) -> tuple[dict[str, object], set[str]]:
    contract_path = repository / "docs/operations/PHASE9_TEST_SUITE_CONTRACT.json"
    contract_blob = _blob(
        repository, commit, "docs/operations/PHASE9_TEST_SUITE_CONTRACT.json"
    )
    if _stable_regular_bytes(contract_path, "suite contract") != contract_blob:
        raise RuntimeError("working suite contract differs from immutable candidate")
    rebuilt = _build_summary_for_policy(
        audit_root=audit_root,
        records_root=audit_root / "command_records",
        suite_contract_path=contract_path,
        expected_suite_specs=suite_specs,
        runtime_validation=True,
    )
    summary_path = audit_root / "evidence/FINAL_TEST_SUMMARY.json"
    summary_raw = _stable_regular_bytes(summary_path, "final test summary")
    if summary_raw != canonical_bytes(rebuilt) + b"\n":
        raise RuntimeError("FINAL_TEST_SUMMARY is not independently reproducible")
    if rebuilt.get("candidate") != identity or rebuilt.get("result") != "PASS":
        raise RuntimeError("test summary does not PASS the immutable candidate")
    independent_inventory, independent_raw = executed_source_inventory(
        repository, repository, execution_environment="source"
    )
    if rebuilt.get("source_inventory_sha256") != independent_inventory[
        "inventory_sha256"
    ]:
        raise RuntimeError("test records do not bind current immutable source bytes")
    for record in [*rebuilt["records"], *rebuilt["failed_attempts"]]:
        bound_inventory_path = audit_root / str(record["source_inventory"]["path"])
        if _stable_regular_bytes(
            bound_inventory_path, "bound source inventory"
        ) != independent_raw:
            raise RuntimeError("bound source inventory is not independently reproducible")

    production, _ = _read_canonical_json(
        audit_root / "evidence/PRODUCTION_STATUS.json", "production status"
    )
    _verify_production_status(production, identity)
    _verify_review_material(audit_root, identity, rebuilt)

    mapping = _blob(
        repository,
        commit,
        "docs/operations/PHASE9_REQUIREMENT_IMPLEMENTATION_TEST_EVIDENCE_MAP.tsv",
    )
    _inventory, candidate_paths = _candidate_inventory(repository, commit)
    _verify_requirement_map(
        mapping,
        candidate_paths=candidate_paths,
        frozen_paths=set(source_paths),
        audit_root=audit_root,
        summary=rebuilt,
        suite_specs=suite_specs,
    )
    expected_paths = _expected_evidence_paths(rebuilt)
    if set(_artifact_inventory(audit_root)) != expected_paths:
        raise RuntimeError("audit artifact inventory is not the exact semantic closure")
    return rebuilt, expected_paths


def _read_canonical_json(
    path: Path, label: str
) -> tuple[dict[str, object], bytes]:
    raw = _stable_regular_bytes(path, label)
    try:
        value = json.loads(raw)
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{label} is not JSON") from exc
    if type(value) is not dict or canonical_bytes(value) + b"\n" != raw:
        raise RuntimeError(f"{label} is not canonical JSON")
    return value, raw


def _secret_scan(payload: dict[str, bytes]) -> None:
    private_key_header = b"-----BEGIN " + b"PRIVATE KEY-----"
    openssh_private_key_header = b"-----BEGIN OPENSSH " + b"PRIVATE KEY-----"
    google_api_key_prefix = b"AIza" + b"Sy"
    aws_access_key_prefix = b"AK" + b"IA"
    forbidden = (
        private_key_header, openssh_private_key_header,
        google_api_key_prefix, aws_access_key_prefix,
    )
    for path, raw in payload.items():
        lowered_parts = [part.casefold() for part in PurePosixPath(path).parts]
        if any(part in {".env", "credentials", "secrets"} for part in lowered_parts):
            raise RuntimeError(f"possible credential path in {path}")
        if any(marker in raw for marker in forbidden):
            raise RuntimeError(f"possible credential material in {path}")


def _manifest(payload: dict[str, bytes]) -> bytes:
    body = {
        "schema": MANIFEST_SCHEMA,
        "closure": "all payload members except PACKAGE_MANIFEST.json and checksums/SHA256SUMS",
        "files": [
            {
                "path": path,
                "bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "mode": "0644",
            }
            for path, raw in sorted(payload.items())
        ],
    }
    body["files_sha256"] = canonical_sha256(body["files"])
    return canonical_bytes(body) + b"\n"


def _verify_manifest_payload(
    manifest_raw: bytes, actual: dict[str, bytes]
) -> None:
    try:
        manifest = json.loads(manifest_raw)
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("manifest is not JSON") from exc
    if canonical_bytes(manifest) + b"\n" != manifest_raw or type(manifest) is not dict:
        raise RuntimeError("manifest is not canonical JSON")
    if set(manifest) != {"schema", "closure", "files", "files_sha256"} or (
        manifest.get("schema") != MANIFEST_SCHEMA
        or manifest.get("closure")
        != "all payload members except PACKAGE_MANIFEST.json and checksums/SHA256SUMS"
    ):
        raise RuntimeError("manifest schema/closure differs")
    rows = manifest.get("files")
    if type(rows) is not list or manifest.get("files_sha256") != canonical_sha256(rows):
        raise RuntimeError("manifest files hash differs")
    declared: dict[str, tuple[int, str]] = {}
    for row in rows:
        if type(row) is not dict or set(row) != {"path", "bytes", "sha256", "mode"}:
            raise RuntimeError("manifest row shape differs")
        if type(row.get("path")) is not str:
            raise RuntimeError("manifest row path differs")
        relative = _safe(row["path"])
        if (
            relative in declared
            or type(row.get("bytes")) is not int
            or row["bytes"] < 0
            or type(row.get("sha256")) is not str
            or _HEX64.fullmatch(row["sha256"]) is None
            or row.get("mode") != "0644"
        ):
            raise RuntimeError("manifest row identity is invalid or duplicated")
        declared[relative] = (row["bytes"], row["sha256"])
    if [row["path"] for row in rows] != sorted(declared):
        raise RuntimeError("manifest rows are not canonically sorted")
    if set(declared) != set(actual):
        raise RuntimeError("manifest member closure differs")
    for relative, (size, digest) in declared.items():
        raw = actual[relative]
        if len(raw) != size or hashlib.sha256(raw).hexdigest() != digest:
            raise RuntimeError(f"manifest identity differs: {relative}")


def _verify_checksum_payload(checksum_raw: bytes, actual: dict[str, bytes]) -> None:
    try:
        text = checksum_raw.decode("ascii")
    except UnicodeError as exc:
        raise RuntimeError("checksums are not ASCII") from exc
    if not text.endswith("\n") or "\r" in text:
        raise RuntimeError("checksums newline form differs")
    lines = text.splitlines()
    expected_lines = [
        f"{hashlib.sha256(raw).hexdigest()}  {relative}"
        for relative, raw in sorted(actual.items())
    ]
    if lines != expected_lines:
        raise RuntimeError("checksum closure/order/identity differs")


def _verify_zip(path: Path, root_name: str) -> dict[str, object]:
    raw_zip = path.read_bytes()
    with zipfile.ZipFile(path) as archive:
        if archive.comment:
            raise RuntimeError("ZIP archive comment must be empty")
        if archive.testzip() is not None:
            raise RuntimeError("ZIP CRC verification failed")
        infos = archive.infolist()
        if not infos or len({info.filename for info in infos}) != len(infos):
            raise RuntimeError("ZIP member inventory is empty or duplicated")
        folded: set[str] = set()
        for info in infos:
            name = _safe(info.filename)
            parts = PurePosixPath(name).parts
            if not parts or parts[0] != root_name or info.is_dir():
                raise RuntimeError("ZIP must contain ordinary files under one root")
            relative = PurePosixPath(*parts[1:]).as_posix()
            _safe(relative)
            folded_name = unicodedata.normalize("NFC", relative).casefold()
            if folded_name in folded:
                raise RuntimeError("ZIP has a casefold/NFC collision")
            folded.add(folded_name)
            if info.date_time != FIXED_ZIP_TIMESTAMP:
                raise RuntimeError("ZIP timestamp is not fixed")
            if (info.external_attr >> 16) != NORMALIZED_MODE:
                raise RuntimeError("ZIP member mode is not normalized 0644")
            if info.create_system != 3 or info.compress_type != zipfile.ZIP_DEFLATED:
                raise RuntimeError("ZIP member platform/compression differs")
            if info.extra or info.comment or info.flag_bits & 0x1:
                raise RuntimeError("ZIP member extra/comment/encryption differs")
        names = {info.filename for info in infos}
        prefix = f"{root_name}/"
        manifest_path = prefix + "PACKAGE_MANIFEST.json"
        checksum_path = prefix + "checksums/SHA256SUMS"
        payload_without_meta = {
            name.removeprefix(prefix): archive.read(name)
            for name in names - {manifest_path, checksum_path}
        }
        _verify_manifest_payload(archive.read(manifest_path), payload_without_meta)
        checksum_actual = {
            name.removeprefix(prefix): archive.read(name)
            for name in names - {checksum_path}
        }
        _verify_checksum_payload(archive.read(checksum_path), checksum_actual)
    return {
        "path": str(path.resolve()), "bytes": len(raw_zip),
        "member_count": len(infos), "sha256": hashlib.sha256(raw_zip).hexdigest(),
        "single_root": root_name, "crc_verified": True,
        "fixed_timestamp": "2026-01-01T00:00:00Z", "normalized_mode": "0644",
    }


def _tree_payload(root: Path) -> dict[str, bytes]:
    """Read a complete extracted tree with collision/link/hardlink checks."""

    result: dict[str, bytes] = {}
    folded: set[str] = set()

    def traversal_error(error: OSError) -> None:
        raise RuntimeError("extracted package cannot be enumerated") from error

    for current, directories, files in os.walk(
        root, topdown=True, followlinks=False, onerror=traversal_error
    ):
        current_path = Path(current)
        for name in directories:
            item = current_path / name
            if not stat.S_ISDIR(item.lstat().st_mode):
                raise RuntimeError("extracted package contains a linked/special directory")
        for name in files:
            item = current_path / name
            relative = _safe(item.relative_to(root).as_posix())
            metadata = item.lstat()
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise RuntimeError("extracted package contains a link/hardlink/special file")
            collision = unicodedata.normalize("NFC", relative).casefold()
            if collision in folded:
                raise RuntimeError("extracted package has a casefold/Unicode collision")
            folded.add(collision)
            result[relative] = _stable_regular_bytes(item, f"package member {relative}")
    return result


def _parse_candidate_inventory(raw: bytes) -> dict[str, dict[str, object]]:
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise RuntimeError("candidate inventory is not UTF-8") from exc
    if not text.endswith("\n") or "\r" in text:
        raise RuntimeError("candidate inventory newline form differs")
    lines = text.splitlines()
    if not lines or lines[0] != "path\tmode\ttype\tobject_id\tbytes":
        raise RuntimeError("candidate inventory header differs")
    result: dict[str, dict[str, object]] = {}
    collisions: set[str] = set()
    for line in lines[1:]:
        fields = line.split("\t")
        if len(fields) != 5:
            raise RuntimeError("candidate inventory row width differs")
        path, mode, kind, object_id, byte_text = fields
        _safe(path)
        collision = unicodedata.normalize("NFC", path).casefold()
        if path in result or collision in collisions or _HEX40.fullmatch(object_id) is None:
            raise RuntimeError("candidate inventory path/object is invalid or duplicated")
        collisions.add(collision)
        if kind == "blob":
            if mode not in {"100644", "100755"} or not byte_text.isdecimal():
                raise RuntimeError("candidate inventory blob row differs")
            size: int | None = int(byte_text)
        elif kind == "commit" and mode == "160000" and byte_text == "-":
            size = None
        else:
            raise RuntimeError("candidate inventory object type differs")
        result[path] = {
            "mode": mode, "type": kind, "object_id": object_id, "bytes": size,
        }
    if list(result) != sorted(result) or not result:
        raise RuntimeError("candidate inventory rows are empty or unsorted")
    return result


def _reconstruct_git_tree_oid(
    inventory: dict[str, dict[str, object]]
) -> str:
    """Reconstruct the recursive Git tree OID from the portable flat inventory."""

    root: dict[str, object] = {}
    for path, record in inventory.items():
        parts = PurePosixPath(path).parts
        node = root
        for part in parts[:-1]:
            existing = node.get(part)
            if existing is None:
                child: dict[str, object] = {}
                node[part] = child
                node = child
            elif type(existing) is dict:
                node = existing
            else:
                raise RuntimeError("candidate inventory has a file/directory conflict")
        leaf = parts[-1]
        if leaf in node:
            raise RuntimeError("candidate inventory has a path conflict")
        node[leaf] = ("leaf", record)

    def tree_oid(node: dict[str, object]) -> str:
        encoded: list[tuple[bytes, bytes]] = []
        for name, value in node.items():
            raw_name = name.encode("utf-8", errors="strict")
            if type(value) is dict:
                oid = tree_oid(value)
                mode = "40000"
                ordering = raw_name + b"/"
            else:
                _leaf, record = value
                if type(record) is not dict:
                    raise RuntimeError("candidate inventory tree record differs")
                oid = str(record["object_id"])
                mode = str(record["mode"])
                ordering = raw_name
            wire = (
                mode.encode("ascii")
                + b" "
                + raw_name
                + b"\0"
                + bytes.fromhex(oid)
            )
            encoded.append((ordering, wire))
        content = b"".join(wire for _key, wire in sorted(encoded))
        header = f"tree {len(content)}\0".encode("ascii")
        return hashlib.sha1(header + content, usedforsecurity=False).hexdigest()

    return tree_oid(root)


def _verify_execution_inventories_against_candidate(
    package_root: Path,
    summary: dict[str, object],
    inventory: dict[str, dict[str, object]],
) -> None:
    """Cross-bind every candidate row to every executed-source inventory."""

    records = [*summary["records"], *summary["failed_attempts"]]
    if not records:
        raise RuntimeError("package has no execution inventory evidence")
    candidate_paths = set(inventory)
    for record in records:
        descriptor = record.get("source_inventory")
        if type(descriptor) is not dict or type(descriptor.get("path")) is not str:
            raise RuntimeError("execution inventory descriptor differs")
        body, _raw = _read_canonical_json(
            package_root / str(descriptor["path"]), "execution inventory"
        )
        files = body.get("files")
        if type(files) is not list:
            raise RuntimeError("execution inventory files differ")
        observed: dict[str, dict[str, object]] = {}
        for item in files:
            if type(item) is not dict or type(item.get("path")) is not str:
                raise RuntimeError("execution inventory row differs")
            path = str(item["path"])
            if path in observed:
                raise RuntimeError("execution inventory path is duplicated")
            observed[path] = item
        if set(observed) != candidate_paths:
            raise RuntimeError("execution/candidate inventory path closure differs")
        for path, candidate_row in inventory.items():
            executed = observed[path]
            for field in ("mode", "type", "object_id", "bytes"):
                if field in candidate_row and executed.get(field) != candidate_row[field]:
                    raise RuntimeError(
                        f"execution/candidate inventory row differs: {path}"
                    )


def _git_blob_oid(raw: bytes) -> str:
    header = f"blob {len(raw)}\0".encode("ascii")
    return hashlib.sha1(header + raw, usedforsecurity=False).hexdigest()


def _verify_frozen_subset(
    raw: bytes,
    *,
    payload: dict[str, bytes],
    inventory: dict[str, dict[str, object]],
) -> set[str]:
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise RuntimeError("frozen source inventory is not UTF-8") from exc
    if not text.endswith("\n") or "\r" in text:
        raise RuntimeError("frozen source inventory newline form differs")
    lines = text.splitlines()
    if not lines or lines[0] != "candidate_path\tbytes\tsha256\tpackage_path":
        raise RuntimeError("frozen source inventory header differs")
    result: set[str] = set()
    ordered: list[str] = []
    for line in lines[1:]:
        fields = line.split("\t")
        if len(fields) != 4:
            raise RuntimeError("frozen source inventory row width differs")
        candidate_path, byte_text, digest, package_path = fields
        _safe(candidate_path)
        _safe(package_path)
        if (
            candidate_path in result
            or package_path != f"source/{candidate_path}"
            or not byte_text.isdecimal()
            or _HEX64.fullmatch(digest) is None
            or candidate_path not in inventory
            or inventory[candidate_path]["type"] != "blob"
            or package_path not in payload
        ):
            raise RuntimeError("frozen source row differs")
        source_raw = payload[package_path]
        if (
            len(source_raw) != int(byte_text)
            or hashlib.sha256(source_raw).hexdigest() != digest
            or inventory[candidate_path]["bytes"] != len(source_raw)
            or inventory[candidate_path]["object_id"] != _git_blob_oid(source_raw)
        ):
            raise RuntimeError(f"frozen source bytes differ: {candidate_path}")
        result.add(candidate_path)
        ordered.append(candidate_path)
    if ordered != sorted(ordered) or not result:
        raise RuntimeError("frozen source rows are empty or unsorted")
    actual_source = {
        relative.removeprefix("source/")
        for relative in payload
        if relative.startswith("source/")
    }
    if actual_source != result:
        raise RuntimeError("frozen source member closure differs")
    return result


def _verify_extracted_package_for_policy(
    package_root: Path,
    *,
    suite_specs: dict[str, dict[str, object]],
) -> dict[str, object]:
    package_root = package_root.resolve(strict=True)
    payload = _tree_payload(package_root)
    required_meta = {"PACKAGE_MANIFEST.json", "checksums/SHA256SUMS"}
    if not required_meta.issubset(payload):
        raise RuntimeError("package metadata is missing")
    _verify_manifest_payload(
        payload["PACKAGE_MANIFEST.json"],
        {path: raw for path, raw in payload.items() if path not in required_meta},
    )
    _verify_checksum_payload(
        payload["checksums/SHA256SUMS"],
        {path: raw for path, raw in payload.items() if path != "checksums/SHA256SUMS"},
    )

    identity, _ = _read_canonical_json(
        package_root / "identity/CANDIDATE_IDENTITY.json", "candidate identity"
    )
    identity_keys = {
        "schema", "commit", "tree", "parent", "baseline", "freeze_utc",
        "candidate_inventory_bytes", "candidate_inventory_sha256",
        "candidate_path_count", "frozen_source_path_count", "identity_sha256",
    }
    if set(identity) != identity_keys or identity.get("schema") != IDENTITY_SCHEMA:
        raise RuntimeError("candidate identity schema/keys differ")
    if any(
        type(identity.get(name)) is not str
        or _HEX40.fullmatch(str(identity[name])) is None
        for name in ("commit", "tree", "parent")
    ):
        raise RuntimeError("candidate identity OIDs differ")
    baseline = identity.get("baseline")
    if baseline != AUDITED_BASELINE_IDENTITY:
        raise RuntimeError("candidate baseline identity differs")
    unsigned_identity = dict(identity)
    identity_digest = unsigned_identity.pop("identity_sha256")
    if identity_digest != canonical_sha256(unsigned_identity):
        raise RuntimeError("candidate identity self-hash differs")
    _validate_freeze_utc(identity.get("freeze_utc"))

    inventory_raw = payload["identity/CANDIDATE_FILE_INVENTORY.tsv"]
    inventory = _parse_candidate_inventory(inventory_raw)
    if (
        identity.get("candidate_inventory_bytes") != len(inventory_raw)
        or identity.get("candidate_inventory_sha256")
        != hashlib.sha256(inventory_raw).hexdigest()
        or identity.get("candidate_path_count") != len(inventory)
    ):
        raise RuntimeError("candidate inventory identity differs")
    if _reconstruct_git_tree_oid(inventory) != identity.get("tree"):
        raise RuntimeError("candidate inventory does not reconstruct the candidate tree")
    frozen = _verify_frozen_subset(
        payload["identity/FROZEN_SOURCE_SUBSET.tsv"],
        payload=payload,
        inventory=inventory,
    )
    if identity.get("frozen_source_path_count") != len(frozen):
        raise RuntimeError("frozen source path count differs")

    summary = _build_summary_for_policy(
        audit_root=package_root,
        records_root=package_root / "command_records",
        suite_contract_path=(
            package_root / "source/docs/operations/PHASE9_TEST_SUITE_CONTRACT.json"
        ),
        expected_suite_specs=suite_specs,
        runtime_validation=False,
    )
    if payload["evidence/FINAL_TEST_SUMMARY.json"] != canonical_bytes(summary) + b"\n":
        raise RuntimeError("packaged test summary is not portably reproducible")
    expected_identity = {
        "commit": identity["commit"], "tree": identity["tree"],
        "parent": identity["parent"],
    }
    if summary.get("candidate") != expected_identity or summary.get("result") != "PASS":
        raise RuntimeError("packaged test summary does not PASS this candidate")
    _verify_execution_inventories_against_candidate(
        package_root, summary, inventory
    )
    production, _ = _read_canonical_json(
        package_root / "evidence/PRODUCTION_STATUS.json", "production status"
    )
    _verify_production_status(production, expected_identity)
    _verify_review_material(package_root, expected_identity, summary)
    _verify_requirement_map(
        payload[
            "source/docs/operations/PHASE9_REQUIREMENT_IMPLEMENTATION_TEST_EVIDENCE_MAP.tsv"
        ],
        candidate_paths=set(inventory),
        frozen_paths=frozen,
        audit_root=package_root,
        summary=summary,
        suite_specs=suite_specs,
    )
    if set(_artifact_inventory(package_root)) != _expected_evidence_paths(summary):
        raise RuntimeError("packaged audit artifact closure differs")
    if payload.get("PACKAGE_README.md") != PACKAGE_README:
        raise RuntimeError("package README contract differs")
    _secret_scan(payload)
    return {
        "candidate": expected_identity,
        "summary_sha256": summary["summary_sha256"],
        "semantic_verified": True,
        "frozen_source_path_count": len(frozen),
    }


def verify_extracted_package(package_root: Path) -> dict[str, object]:
    """Verify an extracted formal package without original host paths or Git."""

    return _verify_extracted_package_for_policy(
        package_root, suite_specs=PHASE9_REQUIRED_SUITE_SPECS
    )


def _build_for_policy(
    repository: Path,
    audit_root: Path,
    output: Path,
    *,
    root_name: str,
    freeze_utc: str,
    commit: str = "HEAD",
    source_paths: tuple[str, ...],
    suite_specs: dict[str, dict[str, object]],
) -> dict[str, object]:
    repository = repository.resolve(strict=True)
    audit_root = audit_root.resolve(strict=True)
    freeze_utc = _validate_freeze_utc(freeze_utc)
    identity = _identity(repository, commit)
    if _identity(repository, "HEAD") != identity:
        raise RuntimeError("package repository HEAD must be the immutable candidate")
    tracked_dirty = _git(
        repository, "status", "--porcelain=v1", "-z", "--untracked-files=no"
    )
    if tracked_dirty:
        raise RuntimeError("package build refuses tracked or index dirty bytes")
    _summary, expected_evidence_paths = _verify_semantic_evidence(
        repository,
        identity["commit"],
        audit_root,
        identity,
        source_paths,
        suite_specs=suite_specs,
    )
    inventory, candidate_paths = _candidate_inventory(repository, identity["commit"])
    if len(source_paths) != len(set(source_paths)):
        raise RuntimeError("frozen source policy contains duplicate paths")
    payload: dict[str, bytes] = {
        "identity/CANDIDATE_FILE_INVENTORY.tsv": inventory,
    }
    source_rows = []
    for path in sorted(source_paths):
        _safe(path)
        if path not in candidate_paths:
            raise RuntimeError(f"frozen source path is absent from candidate: {path}")
        raw = _blob(repository, identity["commit"], path)
        package_path = f"source/{path}"
        payload[package_path] = raw
        source_rows.append(
            f"{path}\t{len(raw)}\t{hashlib.sha256(raw).hexdigest()}\t{package_path}"
        )
    payload["identity/FROZEN_SOURCE_SUBSET.tsv"] = (
        "candidate_path\tbytes\tsha256\tpackage_path\n"
        + "\n".join(source_rows) + "\n"
    ).encode()
    identity_body = {
        "schema": "paper-factory-phase9-candidate-identity-v1",
        **identity,
        "baseline": dict(AUDITED_BASELINE_IDENTITY),
        "freeze_utc": freeze_utc,
        "candidate_inventory_bytes": len(inventory),
        "candidate_inventory_sha256": hashlib.sha256(inventory).hexdigest(),
        "candidate_path_count": len(candidate_paths),
        "frozen_source_path_count": len(source_paths),
    }
    identity_body["identity_sha256"] = canonical_sha256(identity_body)
    payload["identity/CANDIDATE_IDENTITY.json"] = canonical_bytes(identity_body) + b"\n"
    _copy_audit_evidence(audit_root, payload, expected_evidence_paths)
    payload["PACKAGE_README.md"] = PACKAGE_README
    _secret_scan(payload)
    manifest = _manifest(payload)
    payload["PACKAGE_MANIFEST.json"] = manifest
    checksum_lines = [
        f"{hashlib.sha256(raw).hexdigest()}  {path}"
        for path, raw in sorted(payload.items())
    ]
    payload["checksums/SHA256SUMS"] = ("\n".join(checksum_lines) + "\n").encode("ascii")
    output = output.absolute()
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists() or output.is_symlink():
        raise RuntimeError("audit ZIP output is append-only; choose a new path")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix="package-build.", suffix=".zip", dir=audit_root
    )
    os.close(descriptor)
    temporary_zip = Path(temporary_name)
    try:
        with zipfile.ZipFile(
            temporary_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
        ) as archive:
            for relative, raw in sorted(payload.items()):
                info = zipfile.ZipInfo(f"{root_name}/{relative}", FIXED_ZIP_TIMESTAMP)
                info.create_system = 3
                info.external_attr = NORMALIZED_MODE << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(
                    info, raw, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9
                )
        archive_result = _verify_zip(temporary_zip, root_name)
        with tempfile.TemporaryDirectory(
            prefix="package-verify.", dir=audit_root
        ) as verification_name:
            verification = Path(verification_name)
            package_root = verification / root_name
            package_root.mkdir()
            with zipfile.ZipFile(temporary_zip) as archive:
                for info in archive.infolist():
                    relative = PurePosixPath(info.filename).relative_to(root_name)
                    target = package_root.joinpath(*relative.parts)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with target.open("xb") as stream:
                        stream.write(archive.read(info))
            semantic = _verify_extracted_package_for_policy(
                package_root, suite_specs=suite_specs
            )
        final_raw = _stable_regular_bytes(temporary_zip, "temporary audit ZIP")
        with output.open("xb") as stream:
            stream.write(final_raw)
            stream.flush()
            os.fsync(stream.fileno())
        final_result = _verify_zip(output, root_name)
        if final_result["sha256"] != archive_result["sha256"]:
            raise RuntimeError("final ZIP bytes differ from verified temporary archive")
        return {**final_result, **semantic}
    finally:
        temporary_zip.unlink(missing_ok=True)


def build(
    repository: Path,
    audit_root: Path,
    output: Path,
    *,
    root_name: str,
    freeze_utc: str,
    commit: str = "HEAD",
) -> dict[str, object]:
    """Build a formal package under non-overridable source/suite policies."""

    return _build_for_policy(
        repository,
        audit_root,
        output,
        root_name=root_name,
        freeze_utc=freeze_utc,
        commit=commit,
        source_paths=DEFAULT_SOURCE_PATHS,
        suite_specs=PHASE9_REQUIRED_SUITE_SPECS,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True, type=Path)
    parser.add_argument("--audit-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--root-name", default="PAPER_FACTORY_PHASE9_PRO_AUDIT")
    parser.add_argument("--freeze-utc", required=True)
    parser.add_argument("--commit", default="HEAD")
    args = parser.parse_args(argv)
    if re.fullmatch(r"[A-Z0-9_]+", args.root_name) is None:
        parser.error("root name must contain only A-Z, 0-9, underscore")
    result = build(
        args.repository, args.audit_root, args.output, root_name=args.root_name,
        freeze_utc=args.freeze_utc, commit=args.commit,
    )
    print(canonical_bytes(result).decode())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
