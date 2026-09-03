from __future__ import annotations

import json
import importlib
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from factory_core.canonical import canonical_bytes, canonical_sha256
from tools.build_phase9_test_summary import (
    _build_summary_for_policy,
    build_summary,
)
from tools.run_audit_command import (
    executed_source_inventory,
    main as run_audit_command,
    parse_outcomes,
)
from tools.run_full_repo_with_frontend_deps import _verify_locked_dependencies


REPOSITORY = Path(__file__).resolve().parents[1]
SAMPLE_SUITE_SPECS = {
    "sample": {
        "kind": "pytest",
        "requirements": ["P9-EVIDENCE"],
        "required_targets": ["tests/test_sample.py"],
    }
}


def _git(repository: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", *arguments],
        cwd=repository,
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={
            "PATH": "/usr/bin:/bin",
            "LC_ALL": "C",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
        },
    )


def _candidate_repository(tmp_path: Path) -> tuple[Path, Path]:
    repository = tmp_path / "candidate"
    (repository / "factory_core").mkdir(parents=True)
    (repository / "tools").mkdir()
    (repository / "factory_core/__init__.py").write_text("", encoding="utf-8")
    for relative in (
        "factory_core/canonical.py",
        "tools/run_audit_command.py",
        "tools/trusted_pytest_reporter.py",
    ):
        target = repository / relative
        target.write_bytes((REPOSITORY / relative).read_bytes())
    (repository / "candidate_probe.py").write_text(
        "ORIGIN = 'candidate'\n", encoding="utf-8"
    )
    (repository / "tests").mkdir()
    (repository / "tests/test_sample.py").write_text(
        "import os\nimport subprocess\nimport sys\nfrom pathlib import Path\n"
        "from candidate_probe import ORIGIN\n\n"
        "def test_evidence_runner_executes_candidate_bytes(tmp_path):\n"
        "    assert ORIGIN == 'candidate'\n"
        "    assert tmp_path.is_dir()\n"
        "    assert Path(sys.prefix).resolve() == Path(sys.executable).parent.parent.resolve()\n"
        "    child = subprocess.run([sys.executable, '-c', 'import candidate_probe'], cwd=Path(__file__).resolve().parents[1])\n"
        "    assert child.returncode == 0\n"
        "    root = Path(__file__).resolve().parents[1]\n"
        "    for name in ('ongoing', 'run_state', 'logs', 'papers'):\n"
        "        target = root / name / 'sandbox-probe'\n"
        "        target.parent.mkdir(parents=True, exist_ok=True)\n"
        "        target.write_text('isolated\\n', encoding='utf-8')\n"
        "    identity_root = Path(os.environ['PHASE9_TEST_SOURCE_REPOSITORY'])\n"
        "    assert identity_root.resolve() != root.resolve()\n"
        "    identity_status = subprocess.run(['git', 'status', '--porcelain=v1', "
        "'--untracked-files=all', '--ignored'], cwd=identity_root, "
        "capture_output=True, text=True, check=True)\n"
        "    assert identity_status.stdout == ''\n"
        "    if (root / '.git').exists():\n"
        "        execution_files = subprocess.run(['git', 'ls-files', '-co', "
        "'--exclude-standard', '-z'], cwd=root, capture_output=True, check=True)\n"
        "        assert b'tests/test_sample.py\\0' in execution_files.stdout\n"
        "    else:\n"
        "        assert not (root / '.git').exists()\n"
        "    assert 'PHASE9_POLLUTER_LOADED' not in os.environ\n"
        "    assert not any('attempt1' in item for item in sys.argv)\n"
        "    host_project = Path(sys.executable).parent.parent.parent\n"
        "    assert not (host_project / 'factory_core').exists()\n",
        encoding="utf-8",
    )
    contract = {
        "schema": "paper-factory-phase9-test-suite-contract-v1",
        "required_environments": ["fresh", "source"],
        "suites": [
            {
                "id": "sample",
                "kind": "pytest",
                "requirements": ["P9-EVIDENCE"],
                "description": "isolated evidence runner contract",
                "required_targets": ["tests/test_sample.py"],
            }
        ],
    }
    contract_path = repository / "suite-contract.json"
    contract_path.write_bytes(canonical_bytes(contract) + b"\n")
    _git(repository, "init", "-q")
    _git(repository, "config", "user.name", "Phase9 Evidence Test")
    _git(repository, "config", "user.email", "phase9-evidence@example.invalid")
    (repository / "parent.txt").write_text("parent\n", encoding="utf-8")
    _git(repository, "add", "--", "parent.txt")
    _git(repository, "commit", "-qm", "parent")
    _git(
        repository,
        "add",
        "--",
        "factory_core/__init__.py",
        "factory_core/canonical.py",
        "tools/run_audit_command.py",
        "tools/trusted_pytest_reporter.py",
        "candidate_probe.py",
        "tests/test_sample.py",
        "suite-contract.json",
    )
    _git(repository, "commit", "-qm", "candidate")
    return repository, contract_path


def _fresh_copy(repository: Path, tmp_path: Path) -> Path:
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    for relative in (
        "factory_core/__init__.py",
        "factory_core/canonical.py",
        "tools/run_audit_command.py",
        "tools/trusted_pytest_reporter.py",
        "candidate_probe.py",
        "tests/test_sample.py",
        "suite-contract.json",
        "parent.txt",
    ):
        target = fresh / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(
            subprocess.run(
                ["git", "show", f"HEAD:{relative}"],
                cwd=repository,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            ).stdout
        )
    return fresh


def _record(
    *, repository: Path, cwd: Path, audit_root: Path, environment: str
) -> int:
    return run_audit_command(
        [
            "--id", f"{environment}_sample_final",
            "--suite", "sample",
            "--environment", environment,
            "--kind", "pytest",
            "--repository", str(repository),
            "--audit-root", str(audit_root),
            "--cwd", str(cwd),
            "--log", str(audit_root / f"test_logs/{environment}_sample_final.log"),
            "--record",
            str(audit_root / f"command_records/{environment}_sample_final.json"),
            "--",
            sys.executable,
            "-B",
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            f"--basetemp={audit_root / 'runtime' / (environment + '_sample_final-pytest') / 'basetemp'}",
            "tests/test_sample.py",
        ]
    )


def test_runner_and_summary_bind_real_source_and_preserve_pair(tmp_path):
    repository, contract = _candidate_repository(tmp_path)
    fresh = _fresh_copy(repository, tmp_path)
    audit_root = tmp_path / "audit"
    audit_root.mkdir()

    assert _record(
        repository=repository, cwd=repository, audit_root=audit_root,
        environment="source",
    ) == 0
    assert _record(
        repository=repository, cwd=fresh, audit_root=audit_root,
        environment="fresh",
    ) == 0
    summary = _build_summary_for_policy(
        audit_root=audit_root,
        records_root=audit_root / "command_records",
        suite_contract_path=contract,
        expected_suite_specs=SAMPLE_SUITE_SPECS,
        runtime_validation=True,
    )

    assert summary["result"] == "PASS"
    assert summary["source_fresh_exact"] is True
    assert summary["final_record_count"] == 2
    assert summary["failed_attempt_count"] == 0
    assert summary["non_pass_totals"] == {
        "failed": 0, "errors": 0, "skipped": 0, "xfailed": 0, "xpassed": 0,
    }
    records = summary["records"]
    assert all(not item["raw_log"]["path"].startswith("/") for item in records)
    assert len({item["source_inventory_sha256"] for item in records}) == 1
    assert summary["pairs"][0]["exact_node_outcome_match"] is True


def test_summary_rejects_same_counts_from_different_collected_nodes(tmp_path):
    repository, contract = _candidate_repository(tmp_path)
    (repository / "tests/test_sample.py").write_text(
        "from pathlib import Path\n"
        "import pytest\n\n"
        "NODE = 'source' if (Path(__file__).parents[1] / '.git').exists() else 'fresh'\n\n"
        "@pytest.mark.parametrize('value', [True], ids=[NODE])\n"
        "def test_environment_specific_node(value):\n"
        "    assert value is True\n",
        encoding="utf-8",
    )
    _git(repository, "add", "--", "tests/test_sample.py")
    _git(repository, "commit", "-qm", "environment-dependent collection")
    fresh = _fresh_copy(repository, tmp_path)
    audit_root = tmp_path / "audit"
    audit_root.mkdir()

    assert _record(
        repository=repository, cwd=repository, audit_root=audit_root,
        environment="source",
    ) == 0
    assert _record(
        repository=repository, cwd=fresh, audit_root=audit_root,
        environment="fresh",
    ) == 0
    summary = _build_summary_for_policy(
        audit_root=audit_root,
        records_root=audit_root / "command_records",
        suite_contract_path=contract,
        expected_suite_specs=SAMPLE_SUITE_SPECS,
        runtime_validation=True,
    )

    assert summary["pairs"][0]["source_outcomes"] == summary["pairs"][0][
        "fresh_outcomes"
    ]
    assert summary["pairs"][0]["exact_outcome_match"] is True
    assert summary["pairs"][0]["exact_node_outcome_match"] is False
    assert summary["source_fresh_exact"] is False
    assert summary["result"] == "NONPASS"


def test_summary_rejects_log_tamper_and_forged_command_shape(tmp_path):
    repository, contract = _candidate_repository(tmp_path)
    fresh = _fresh_copy(repository, tmp_path)
    audit_root = tmp_path / "audit"
    audit_root.mkdir()
    assert _record(
        repository=repository, cwd=repository, audit_root=audit_root,
        environment="source",
    ) == 0
    assert _record(
        repository=repository, cwd=fresh, audit_root=audit_root,
        environment="fresh",
    ) == 0

    log = audit_root / "test_logs/source_sample_final.log"
    original = log.read_bytes()
    log.write_bytes(original + b"forged\n")
    with pytest.raises(RuntimeError, match="raw log byte identity"):
        _build_summary_for_policy(
            audit_root=audit_root,
            records_root=audit_root / "command_records",
            suite_contract_path=contract,
            expected_suite_specs=SAMPLE_SUITE_SPECS,
            runtime_validation=True,
        )
    log.write_bytes(original)

    record_path = audit_root / "command_records/source_sample_final.json"
    record = json.loads(record_path.read_bytes())
    record["command_argv"] = [
        record["command_argv"][0], "-c", "print('1 passed in 0.01s')",
        "-m", "pytest", "tests/test_sample.py",
    ]
    record.pop("record_sha256")
    record["record_sha256"] = canonical_sha256(record)
    record_path.write_bytes(canonical_bytes(record) + b"\n")
    with pytest.raises(RuntimeError, match="isolated pytest command shape differs"):
        _build_summary_for_policy(
            audit_root=audit_root,
            records_root=audit_root / "command_records",
            suite_contract_path=contract,
            expected_suite_specs=SAMPLE_SUITE_SPECS,
            runtime_validation=True,
        )


def test_inventory_rejects_dirty_source_and_modified_fresh_bytes(tmp_path):
    repository, _ = _candidate_repository(tmp_path)
    fresh = _fresh_copy(repository, tmp_path)
    (repository / "tests/test_sample.py").write_text("def test_changed():\n    assert True\n")
    with pytest.raises(RuntimeError, match="tracked or index dirty"):
        executed_source_inventory(repository, repository, execution_environment="source")

    (fresh / "tests/test_sample.py").write_text("def test_changed():\n    assert True\n")
    with pytest.raises(RuntimeError, match="tracked bytes differ"):
        executed_source_inventory(repository, fresh, execution_environment="fresh")


def test_inventory_rejects_untracked_or_extra_importable_bytes(tmp_path):
    repository, _ = _candidate_repository(tmp_path)
    fresh = _fresh_copy(repository, tmp_path)
    (repository / "untracked_pass.py").write_text("assert True\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="execution source file closure differs"):
        executed_source_inventory(repository, repository, execution_environment="source")

    (repository / "untracked_pass.py").unlink()
    (fresh / "extra_test.py").write_text("def test_extra(): assert True\n")
    with pytest.raises(RuntimeError, match="execution source file closure differs"):
        executed_source_inventory(repository, fresh, execution_environment="fresh")


def test_runner_rejects_fake_python_and_collection_shrinking_options(tmp_path):
    repository, _ = _candidate_repository(tmp_path)
    audit_root = tmp_path / "audit"
    audit_root.mkdir()
    fake_python = tmp_path / "fakepython"
    fake_python.write_text("#!/bin/sh\necho '1 passed in 0.01s'\n", encoding="utf-8")
    fake_python.chmod(0o755)
    common = [
        "--id", "source_sample_failed",
        "--suite", "sample",
        "--environment", "source",
        "--kind", "pytest",
        "--repository", str(repository),
        "--audit-root", str(audit_root),
        "--cwd", str(repository),
    ]
    with pytest.raises(RuntimeError, match="exact Python launcher"):
        run_audit_command(
            common
            + [
                "--log", str(audit_root / "test_logs/source_sample_failed.log"),
                "--record", str(audit_root / "command_records/source_sample_failed.json"),
                "--", str(fake_python), "-B", "-m", "pytest", "-q", "-p",
                "no:cacheprovider", f"--basetemp={audit_root / 'fake-temp'}",
                "tests/test_sample.py",
            ]
        )

    with pytest.raises(RuntimeError, match="complete test modules"):
        run_audit_command(
            common
            + [
                "--log", str(audit_root / "test_logs/source_sample_failed.log"),
                "--record", str(audit_root / "command_records/source_sample_failed.json"),
                "--", sys.executable, "-B", "-m", "pytest", "-q", "-p",
                "no:cacheprovider", f"--basetemp={audit_root / 'runtime' / 'source_sample_failed-pytest' / 'basetemp'}",
                "--deselect", "tests/test_sample.py", "untracked_pass.py",
            ]
        )


def test_runner_sanitizes_host_pythonpath_and_preimport_state(tmp_path, monkeypatch):
    repository, _ = _candidate_repository(tmp_path)
    polluter = tmp_path / "host-polluter"
    polluter.mkdir()
    (polluter / "candidate_probe.py").write_text(
        "ORIGIN = 'host-pollution'\n", encoding="utf-8"
    )
    (polluter / "sitecustomize.py").write_text(
        "import os\nos.environ['PHASE9_POLLUTER_LOADED'] = '1'\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(polluter))
    monkeypatch.setenv("PYTHONPATH", str(polluter))
    sys.modules.pop("candidate_probe", None)
    assert importlib.import_module("candidate_probe").ORIGIN == "host-pollution"
    audit_root = tmp_path / "audit"
    audit_root.mkdir()
    assert _record(
        repository=repository,
        cwd=repository,
        audit_root=audit_root,
        environment="source",
    ) == 0
    raw_log = (audit_root / "test_logs/source_sample_final.log").read_text(
        encoding="utf-8"
    )
    assert "host-pollution" not in raw_log
    sys.modules.pop("candidate_probe", None)


def test_runner_rejects_alias_to_trusted_python_launcher(tmp_path):
    repository, _ = _candidate_repository(tmp_path)
    audit_root = tmp_path / "audit"
    audit_root.mkdir()
    alias = tmp_path / "python-alias"
    alias.symlink_to(Path(sys.executable))
    with pytest.raises(RuntimeError, match="exact Python launcher"):
        run_audit_command(
            [
                "--id", "source_sample_alias",
                "--suite", "sample",
                "--environment", "source",
                "--kind", "pytest",
                "--repository", str(repository),
                "--audit-root", str(audit_root),
                "--cwd", str(repository),
                "--log", str(audit_root / "test_logs/source_sample_alias.log"),
                "--record",
                str(audit_root / "command_records/source_sample_alias.json"),
                "--", str(alias), "-B", "-m", "pytest", "-q", "-p",
                "no:cacheprovider", f"--basetemp={audit_root / 'alias-temp'}",
                "tests/test_sample.py",
            ]
        )
    assert not (audit_root / "command_records").exists()
    assert not (audit_root / "test_logs").exists()


def test_formal_summary_policy_cannot_be_shrunk(tmp_path):
    repository, contract = _candidate_repository(tmp_path)
    fresh = _fresh_copy(repository, tmp_path)
    audit_root = tmp_path / "audit"
    audit_root.mkdir()
    assert _record(
        repository=repository, cwd=repository, audit_root=audit_root,
        environment="source",
    ) == 0
    assert _record(
        repository=repository, cwd=fresh, audit_root=audit_root,
        environment="fresh",
    ) == 0
    with pytest.raises(RuntimeError, match="required suite set"):
        build_summary(
            audit_root=audit_root,
            records_root=audit_root / "command_records",
            suite_contract_path=contract,
        )


def test_packaged_summary_rebuild_is_portable_and_reverse_closed(tmp_path):
    repository, contract = _candidate_repository(tmp_path)
    fresh = _fresh_copy(repository, tmp_path)
    audit_root = tmp_path / "audit"
    audit_root.mkdir()
    assert _record(
        repository=repository, cwd=repository, audit_root=audit_root,
        environment="source",
    ) == 0
    assert _record(
        repository=repository, cwd=fresh, audit_root=audit_root,
        environment="fresh",
    ) == 0
    expected = _build_summary_for_policy(
        audit_root=audit_root,
        records_root=audit_root / "command_records",
        suite_contract_path=contract,
        expected_suite_specs=SAMPLE_SUITE_SPECS,
        runtime_validation=True,
    )
    portable = tmp_path / "portable"
    shutil.copytree(audit_root, portable)
    portable_contract = portable / "suite-contract.json"
    portable_contract.write_bytes(contract.read_bytes())
    repository.rename(tmp_path / "candidate-gone")
    fresh.rename(tmp_path / "fresh-gone")
    audit_root.rename(tmp_path / "audit-gone")
    rebuilt = _build_summary_for_policy(
        audit_root=portable,
        records_root=portable / "command_records",
        suite_contract_path=portable_contract,
        expected_suite_specs=SAMPLE_SUITE_SPECS,
        runtime_validation=False,
    )
    assert rebuilt == expected

    (portable / "test_logs/unbound.log").write_text(
        "1 passed in 0.01s\n", encoding="utf-8"
    )
    with pytest.raises(RuntimeError, match="raw log reverse closure"):
        _build_summary_for_policy(
            audit_root=portable,
            records_root=portable / "command_records",
            suite_contract_path=portable_contract,
            expected_suite_specs=SAMPLE_SUITE_SPECS,
            runtime_validation=False,
        )


def test_runtime_summary_recomputes_external_repository_identity(tmp_path):
    repository, contract = _candidate_repository(tmp_path)
    fresh = _fresh_copy(repository, tmp_path)
    audit_root = tmp_path / "audit"
    audit_root.mkdir()
    assert _record(
        repository=repository, cwd=repository, audit_root=audit_root,
        environment="source",
    ) == 0
    assert _record(
        repository=repository, cwd=fresh, audit_root=audit_root,
        environment="fresh",
    ) == 0
    (repository / "identity-change.txt").write_text("new identity\n", encoding="utf-8")
    _git(repository, "add", "--", "identity-change.txt")
    _git(repository, "commit", "-qm", "different candidate")
    with pytest.raises(RuntimeError, match="source execution Git identity differs"):
        _build_summary_for_policy(
            audit_root=audit_root,
            records_root=audit_root / "command_records",
            suite_contract_path=contract,
            expected_suite_specs=SAMPLE_SUITE_SPECS,
            runtime_validation=True,
        )


def test_failed_attempt_is_append_only_and_preserved_in_summary(tmp_path):
    repository, contract = _candidate_repository(tmp_path)
    fresh = _fresh_copy(repository, tmp_path)
    audit_root = tmp_path / "audit"
    audit_root.mkdir()
    invalid_basetemp = (
        audit_root / "runtime" / "source_sample_attempt1-pytest" / "basetemp"
    )
    attempt = [
        "--id", "source_sample_attempt1",
        "--suite", "sample",
        "--environment", "source",
        "--kind", "pytest",
        "--repository", str(repository),
        "--audit-root", str(audit_root),
        "--cwd", str(repository),
        "--log", str(audit_root / "test_logs/source_sample_attempt1.log"),
        "--record",
        str(audit_root / "command_records/source_sample_attempt1.json"),
        "--", sys.executable, "-B", "-m", "pytest", "-q", "-p",
        "no:cacheprovider", f"--basetemp={invalid_basetemp}",
        "tests/test_sample.py",
    ]
    assert run_audit_command(attempt) != 0
    with pytest.raises(RuntimeError, match="append-only"):
        run_audit_command(attempt)
    assert _record(
        repository=repository, cwd=repository, audit_root=audit_root,
        environment="source",
    ) == 0
    assert _record(
        repository=repository, cwd=fresh, audit_root=audit_root,
        environment="fresh",
    ) == 0
    summary = _build_summary_for_policy(
        audit_root=audit_root,
        records_root=audit_root / "command_records",
        suite_contract_path=contract,
        expected_suite_specs=SAMPLE_SUITE_SPECS,
        runtime_validation=True,
    )
    assert summary["result"] == "PASS"
    assert summary["failed_attempt_count"] == 1
    assert summary["failed_attempts"][0]["id"] == "source_sample_attempt1"


def test_outcome_parser_rejects_pass_text_and_truncation():
    empty = {
        "passed": 0, "failed": 0, "errors": 0, "skipped": 0,
        "xfailed": 0, "xpassed": 0, "warnings": 0, "collected": 0,
    }
    assert parse_outcomes(b"PASS\n", "pytest") == empty
    assert parse_outcomes(b"1 passed\n", "pytest") == empty
    assert parse_outcomes(b"1 passed in 0.01s\ntruncated tail\n", "pytest") == empty
    assert parse_outcomes(b"1 passed in 0.01s\n", "pytest")["collected"] == 1
    assert parse_outcomes(
        b"1 skipped in 0.01s\n1 passed in 0.01s\n", "pytest"
    ) == empty
    assert parse_outcomes(
        b"frontend_dependency_preflight=PASS\n1 passed in 0.01s\n", "pytest"
    )["collected"] == 1


def test_runner_rejects_atexit_forged_summary_after_real_nonpass(tmp_path):
    repository, _ = _candidate_repository(tmp_path)
    (repository / "tests/test_sample.py").write_text(
        "import atexit\nimport pytest\n"
        "atexit.register(lambda: print('1 passed in 0.01s'))\n"
        "@pytest.mark.skip(reason='genuine non-pass')\n"
        "def test_not_a_pass():\n    pass\n",
        encoding="utf-8",
    )
    _git(repository, "add", "--", "tests/test_sample.py")
    _git(repository, "commit", "-qm", "forged summary candidate")
    audit_root = tmp_path / "audit"
    audit_root.mkdir()
    assert _record(
        repository=repository, cwd=repository, audit_root=audit_root,
        environment="source",
    ) != 0
    record = json.loads(
        (audit_root / "command_records/source_sample_final.json").read_bytes()
    )
    assert record["attempt_kind"] == "failed"
    assert record["outcomes"]["collected"] == 0
    assert record["exit_code"] == 0
    assert record["runner_exit_code"] != 0
    raw = (audit_root / "test_logs/source_sample_final.log").read_bytes()
    assert b"1 skipped" in raw and raw.rstrip().endswith(b"1 passed in 0.01s")


def test_runner_rejects_dynamic_terminal_stats_forgery(tmp_path):
    repository, _ = _candidate_repository(tmp_path)
    (repository / "tests/test_sample.py").write_text(
        "import pytest\n\n"
        "class TerminalForgery:\n"
        "    @pytest.hookimpl(tryfirst=True)\n"
        "    def pytest_sessionfinish(self, session, exitstatus):\n"
        "        reporter = session.config.pluginmanager.get_plugin('terminalreporter')\n"
        "        reporter.stats.setdefault('passed', []).extend(\n"
        "            reporter.stats.pop('skipped', [])\n"
        "        )\n\n"
        "def test_register_terminal_forgery(request):\n"
        "    request.config.pluginmanager.register(TerminalForgery())\n\n"
        "@pytest.mark.skip(reason='must remain non-pass')\n"
        "def test_skipped_result():\n"
        "    pass\n",
        encoding="utf-8",
    )
    _git(repository, "add", "--", "tests/test_sample.py")
    _git(repository, "commit", "-qm", "dynamic terminal forgery")
    audit_root = tmp_path / "audit"
    audit_root.mkdir()
    assert _record(
        repository=repository,
        cwd=repository,
        audit_root=audit_root,
        environment="source",
    ) != 0
    record = json.loads(
        (audit_root / "command_records/source_sample_final.json").read_bytes()
    )
    assert record["attempt_kind"] == "failed"
    assert record["trusted_pytest"]["validation"] == "NONPASS"
    assert record["trusted_pytest"]["outcomes"] is None
    raw = (audit_root / "test_logs/source_sample_final.log").read_bytes()
    assert b"trusted pytest plugin graph is frozen" in raw
    assert b"2 passed" not in raw


def test_runner_rejects_dynamic_logreport_outcome_forgery(tmp_path):
    repository, _ = _candidate_repository(tmp_path)
    (repository / "tests/test_sample.py").write_text(
        "import pytest\n\n"
        "class ReportForgery:\n"
        "    @pytest.hookimpl(tryfirst=True)\n"
        "    def pytest_runtest_logreport(self, report):\n"
        "        if report.outcome == 'skipped':\n"
        "            report.outcome = 'passed'\n"
        "            report.longrepr = None\n\n"
        "def test_register_report_forgery(request):\n"
        "    request.config.pluginmanager.register(ReportForgery())\n\n"
        "@pytest.mark.skip(reason='must remain a trusted non-pass')\n"
        "def test_skipped_result():\n"
        "    pass\n",
        encoding="utf-8",
    )
    _git(repository, "add", "--", "tests/test_sample.py")
    _git(repository, "commit", "-qm", "dynamic report forgery")
    audit_root = tmp_path / "audit"
    audit_root.mkdir()
    assert _record(
        repository=repository,
        cwd=repository,
        audit_root=audit_root,
        environment="source",
    ) != 0
    record = json.loads(
        (audit_root / "command_records/source_sample_final.json").read_bytes()
    )
    assert record["attempt_kind"] == "failed"
    assert record["trusted_pytest"]["validation"] == "NONPASS"
    raw = (audit_root / "test_logs/source_sample_final.log").read_bytes()
    assert b"trusted pytest plugin graph is frozen" in raw
    assert b"2 passed" not in raw


def test_runner_ignores_candidate_conftest_and_pytest_configuration(tmp_path):
    repository, _ = _candidate_repository(tmp_path)
    (repository / "conftest.py").write_text(
        "def pytest_sessionfinish(session, exitstatus):\n"
        "    raise RuntimeError('candidate conftest executed')\n",
        encoding="utf-8",
    )
    (repository / "pytest.ini").write_text(
        "[pytest]\naddopts = --definitely-not-a-real-option\n",
        encoding="utf-8",
    )
    _git(repository, "add", "--", "conftest.py", "pytest.ini")
    _git(repository, "commit", "-qm", "candidate pytest hook attack")
    audit_root = tmp_path / "audit"
    audit_root.mkdir()
    assert _record(
        repository=repository,
        cwd=repository,
        audit_root=audit_root,
        environment="source",
    ) == 0
    record = json.loads(
        (audit_root / "command_records/source_sample_final.json").read_bytes()
    )
    assert record["trusted_pytest"]["validation"] == "PASS"
    assert "--noconftest" in record["command_argv"]
    assert "/dev/null" in record["command_argv"]


def test_candidate_conftest_cannot_relabel_skip_as_pass(tmp_path):
    repository, _ = _candidate_repository(tmp_path)
    (repository / "conftest.py").write_text(
        "import pytest\n"
        "@pytest.hookimpl(tryfirst=True)\n"
        "def pytest_sessionfinish(session, exitstatus):\n"
        "    reporter = session.config.pluginmanager.get_plugin('terminalreporter')\n"
        "    reporter.stats['passed'] = reporter.stats.pop('skipped')\n",
        encoding="utf-8",
    )
    (repository / "tests/test_sample.py").write_text(
        "import pytest\n"
        "@pytest.mark.skip(reason='real skip')\n"
        "def test_is_not_passed():\n    pass\n",
        encoding="utf-8",
    )
    _git(repository, "add", "--", "conftest.py", "tests/test_sample.py")
    _git(repository, "commit", "-qm", "conftest skip relabel attack")
    audit_root = tmp_path / "audit"
    audit_root.mkdir()
    assert _record(
        repository=repository,
        cwd=repository,
        audit_root=audit_root,
        environment="source",
    ) != 0
    record = json.loads(
        (audit_root / "command_records/source_sample_final.json").read_bytes()
    )
    assert record["trusted_pytest"]["validation"] == "NONPASS"
    raw = (audit_root / "test_logs/source_sample_final.log").read_bytes()
    assert b"1 skipped" in raw and b"1 passed" not in raw


def test_isolated_reporter_precedes_candidate_startup_and_pytest_shadow(tmp_path):
    repository, _ = _candidate_repository(tmp_path)
    (repository / "sitecustomize.py").write_text(
        "raise RuntimeError('candidate startup pollution executed')\n",
        encoding="utf-8",
    )
    (repository / "pytest.py").write_text(
        "raise RuntimeError('candidate pytest shadow imported')\n",
        encoding="utf-8",
    )
    _git(repository, "add", "--", "sitecustomize.py", "pytest.py")
    _git(repository, "commit", "-qm", "candidate startup shadow attack")
    audit_root = tmp_path / "audit"
    audit_root.mkdir()
    assert _record(
        repository=repository,
        cwd=repository,
        audit_root=audit_root,
        environment="source",
    ) == 0
    record = json.loads(
        (audit_root / "command_records/source_sample_final.json").read_bytes()
    )
    assert record["command_argv"][1:4] == ["-I", "-S", "-B"]
    assert record["trusted_pytest"]["validation"] == "PASS"


def test_runner_readonly_sandbox_blocks_mutate_import_restore_attack(tmp_path):
    repository, _ = _candidate_repository(tmp_path)
    (repository / "tests/test_sample.py").write_text(
        "import importlib\nfrom pathlib import Path\nimport candidate_probe\n\n"
        "def test_mutate_import_restore():\n"
        "    target = Path(candidate_probe.__file__)\n"
        "    original = target.read_bytes()\n"
        "    try:\n"
        "        target.write_text(\"ORIGIN = 'forged'\\n\", encoding='utf-8')\n"
        "        importlib.reload(candidate_probe)\n"
        "        assert candidate_probe.ORIGIN == 'forged'\n"
        "    finally:\n"
        "        target.write_bytes(original)\n",
        encoding="utf-8",
    )
    _git(repository, "add", "--", "tests/test_sample.py")
    _git(repository, "commit", "-qm", "mutation attack candidate")
    audit_root = tmp_path / "audit"
    audit_root.mkdir()
    assert _record(
        repository=repository, cwd=repository, audit_root=audit_root,
        environment="source",
    ) != 0
    assert (repository / "candidate_probe.py").read_text(encoding="utf-8") == (
        "ORIGIN = 'candidate'\n"
    )
    record = json.loads(
        (audit_root / "command_records/source_sample_final.json").read_bytes()
    )
    assert record["attempt_kind"] == "failed"
    assert record["source_stable"] is True


def test_runner_exposes_only_dedicated_writable_audit_mounts(tmp_path):
    repository, _ = _candidate_repository(tmp_path)
    (repository / "tests/test_sample.py").write_text(
        "from pathlib import Path\nimport sys\n\n"
        "def test_audit_evidence_is_read_only():\n"
        "    argument = next(item for item in sys.argv if item.startswith('--basetemp='))\n"
        "    audit = Path(argument.split('=', 1)[1]).parent\n"
        "    target = audit / 'test_logs/source_sample_final.log'\n"
        "    try:\n"
        "        target.write_text('forged\\n', encoding='utf-8')\n"
        "    except OSError:\n"
        "        return\n"
        "    raise AssertionError('raw audit log was writable by tested code')\n",
        encoding="utf-8",
    )
    _git(repository, "add", "--", "tests/test_sample.py")
    _git(repository, "commit", "-qm", "audit overwrite probe")
    audit_root = tmp_path / "audit"
    audit_root.mkdir()
    assert _record(
        repository=repository, cwd=repository, audit_root=audit_root,
        environment="source",
    ) == 0
    record = json.loads(
        (audit_root / "command_records/source_sample_final.json").read_bytes()
    )
    mounts = record["execution_sandbox"]["writable_mounts"]
    assert {item["purpose"] for item in mounts} == {
        "HOME", "XDG_CACHE_HOME", "TMPDIR", "PYTEST_BASETEMP_PARENT",
        "SOURCE_ONGOING", "SOURCE_RUN_STATE", "SOURCE_LOGS", "SOURCE_PAPERS",
    }
    test_source = Path(
        record["environment"]["variables"]["PHASE9_TEST_SOURCE_REPOSITORY"]
    )
    assert test_source != repository
    assert test_source.is_relative_to(audit_root / "runtime")
    assert not (test_source / ".git/objects/info/alternates").exists()
    assert subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=test_source,
        check=True,
        text=True,
        capture_output=True,
    ).stdout == ""
    assert record["execution_sandbox"]["audit_mount"]["access"] == "READ_ONLY"
    assert record["execution_sandbox"]["system_tmp_mount"] == {
        "source": record["environment"]["variables"]["TMPDIR"],
        "target": "/tmp",
        "access": "READ_WRITE_PRIVATE",
    }


def test_runner_mounts_linked_worktree_common_git_directory(tmp_path):
    repository, _ = _candidate_repository(tmp_path)
    linked = tmp_path / "linked-candidate"
    _git(repository, "worktree", "add", "--detach", str(linked), "HEAD")
    audit_root = tmp_path / "linked-audit"
    audit_root.mkdir()

    assert _record(
        repository=linked,
        cwd=linked,
        audit_root=audit_root,
        environment="source",
    ) == 0
    record = json.loads(
        (audit_root / "command_records/source_sample_final.json").read_bytes()
    )
    git_mount = record["execution_sandbox"]["git_object_mount"]
    assert git_mount == {
        "path": str((repository / ".git").resolve()),
        "access": "READ_ONLY",
    }


def test_full_runner_mounts_and_recursively_binds_frontend_dependencies(tmp_path):
    repository = tmp_path / "full-candidate"
    for relative in (
        "factory_core", "tools", "tests", "scripts", "web/frontend"
    ):
        (repository / relative).mkdir(parents=True, exist_ok=True)
    (repository / "factory_core/__init__.py").write_text("", encoding="utf-8")
    for relative in (
        "factory_core/canonical.py",
        "tools/run_audit_command.py",
            "tools/run_full_repo_with_frontend_deps.py",
            "tools/trusted_pytest_reporter.py",
    ):
        (repository / relative).write_bytes((REPOSITORY / relative).read_bytes())
    (repository / "scripts/hard_metrics.py").write_text(
        "METRIC_SENTINEL = 42\n", encoding="utf-8"
    )
    (repository / "tests/conftest.py").write_text(
        "FIXTURE_SENTINEL = 'tracked-conftest'\n"
        "def pytest_sessionfinish(session, exitstatus):\n"
        "    raise RuntimeError('candidate conftest hook executed')\n",
        encoding="utf-8",
    )
    (repository / "tests/test_sample.py").write_text(
        "from conftest import FIXTURE_SENTINEL\n"
        "from hard_metrics import METRIC_SENTINEL\n"
        "def test_full_candidate():\n"
        "    assert (FIXTURE_SENTINEL, METRIC_SENTINEL) == "
        "('tracked-conftest', 42)\n",
        encoding="utf-8",
    )
    (repository / "web/frontend/package.json").write_text(
        '{"name":"audit-fixture","version":"1.0.0",'
        '"dependencies":{"audit-dependency":"1.0.0"}}\n',
        encoding="utf-8",
    )
    (repository / "web/frontend/package-lock.json").write_text(
        '{"name":"audit-fixture","version":"1.0.0","lockfileVersion":3,'
        '"requires":true,"packages":{"":{"name":"audit-fixture",'
        '"version":"1.0.0","dependencies":{"audit-dependency":"1.0.0"}},'
        '"node_modules/audit-dependency":{"version":"1.0.0"}}}\n',
        encoding="utf-8",
    )
    contract = {
        "schema": "paper-factory-phase9-test-suite-contract-v1",
        "required_environments": ["fresh", "source"],
        "suites": [
            {
                "id": "full_repository",
                "kind": "pytest",
                "requirements": ["P9-EVIDENCE-CLOSURE"],
                "description": "real dependency-inventory summary regression",
                "required_targets": ["tools/run_full_repo_with_frontend_deps.py"],
            }
        ],
    }
    contract_path = repository / "suite-contract.json"
    contract_path.write_bytes(canonical_bytes(contract) + b"\n")
    _git(repository, "init", "-q")
    _git(repository, "config", "user.name", "Phase9 Evidence Test")
    _git(repository, "config", "user.email", "phase9-evidence@example.invalid")
    (repository / "parent.txt").write_text("parent\n", encoding="utf-8")
    _git(repository, "add", "--", "parent.txt")
    _git(repository, "commit", "-qm", "parent")
    _git(repository, "add", "--", ".")
    _git(repository, "commit", "-qm", "full candidate")

    dependency = tmp_path / "dependency-tree"
    package = dependency / "audit-dependency"
    package.mkdir(parents=True)
    (package / "package.json").write_text(
        '{"name":"audit-dependency","version":"1.0.0"}\n', encoding="utf-8"
    )
    (package / "index.js").write_text("module.exports = 42;\n", encoding="utf-8")
    audit = tmp_path / "audit"
    audit.mkdir()
    fresh = tmp_path / "full-fresh"
    shutil.copytree(repository, fresh, ignore=shutil.ignore_patterns(".git"))

    def run(environment: str, source: Path) -> int:
        identifier = f"{environment}_full_repository_final"
        return run_audit_command(
            [
                "--id", identifier,
                "--suite", "full_repository",
                "--environment", environment,
                "--kind", "pytest",
                "--repository", str(repository),
                "--audit-root", str(audit),
                "--cwd", str(source),
                "--log", str(audit / f"test_logs/{identifier}.log"),
                "--record", str(audit / f"command_records/{identifier}.json"),
                "--", sys.executable, "-B",
                "tools/run_full_repo_with_frontend_deps.py",
                "--source-root", str(source),
                "--dependency-target", str(dependency),
                "--python", sys.executable,
                "--basetemp", str(
                    audit / "runtime"
                    / f"{identifier}-pytest" / "basetemp"
                ),
            ]
        )

    assert run("source", repository) == 0
    assert run("fresh", fresh) == 0
    record = json.loads(
        (audit / "command_records/source_full_repository_final.json").read_bytes()
    )
    descriptor = record["dependency_inventory"]
    bound = json.loads((audit / descriptor["path"]).read_bytes())
    assert bound["kind"] == "FRONTEND_NODE_MODULES"
    assert bound["regular_file_count"] == 2
    assert bound["total_file_bytes"] > 0
    assert len(bound["files"]) == bound["path_count"]
    assert record["dependency_stable"] is True
    assert record["execution_sandbox"]["dependency_mount"] == {
        "path": str(dependency), "access": "READ_ONLY",
    }
    assert not (repository / "web/frontend/node_modules").exists()
    summary = _build_summary_for_policy(
        audit_root=audit,
        records_root=audit / "command_records",
        suite_contract_path=contract_path,
        expected_suite_specs={
            "full_repository": {
                "kind": "pytest",
                "requirements": ["P9-EVIDENCE-CLOSURE"],
                "required_targets": ["tools/run_full_repo_with_frontend_deps.py"],
            }
        },
        runtime_validation=True,
    )
    assert summary["result"] == "PASS"
    assert summary["source_fresh_exact"] is True
    assert summary["pairs"][0]["exact_dependency_match"] is True


def test_full_runner_allows_absent_dev_dependency_but_not_runtime(tmp_path):
    source = tmp_path / "source"
    dependency = tmp_path / "dependency"
    (source / "web/frontend").mkdir(parents=True)
    runtime_package = dependency / "runtime"
    runtime_package.mkdir(parents=True)
    (runtime_package / "package.json").write_text(
        '{"name":"runtime","version":"1.0.0"}\n', encoding="utf-8"
    )
    lock = (
        '{"lockfileVersion":3,"packages":{"":{},'
        '"node_modules/dev-only":{"version":"1.0.0","dev":true},'
        '"node_modules/runtime":{"version":"1.0.0"}}}\n'
    )
    (source / "web/frontend/package-lock.json").write_text(lock, encoding="utf-8")
    _lock_hash, verified, absent = _verify_locked_dependencies(source, dependency)
    assert (verified, absent) == (1, 1)

    runtime_lock = lock.replace(',"dev":true', "")
    (source / "web/frontend/package-lock.json").write_text(
        runtime_lock, encoding="utf-8"
    )
    with pytest.raises(RuntimeError, match="locked dependency is absent"):
        _verify_locked_dependencies(source, dependency)
