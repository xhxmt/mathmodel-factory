from __future__ import annotations

import ast
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from factory_core.phase9_forensic_replay import PHASE9_ACCEPTANCE_CASES
from scripts import phase9_prep_manifest as prep


def _run_git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
        text=True,
    )
    return result.stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, str, str]:
    repository = tmp_path / "repo"
    repository.mkdir(parents=True)
    _run_git(repository, "init", "-q")
    _run_git(repository, "config", "user.email", "phase9-prep@example.invalid")
    _run_git(repository, "config", "user.name", "Phase9 Prep Test")
    (repository / "tracked.txt").write_text("frozen\n", encoding="utf-8")
    (repository / ".gitignore").write_text("ignored.tmp\n", encoding="utf-8")
    _run_git(repository, "add", "tracked.txt", ".gitignore")
    _run_git(repository, "commit", "-q", "-m", "frozen base")
    frozen_commit = _run_git(repository, "rev-parse", "HEAD^{commit}")
    frozen_tree = _run_git(repository, "rev-parse", "HEAD^{tree}")
    _run_git(repository, "switch", "-q", "-c", "codex/phase9-prep-test")
    return repository, frozen_commit, frozen_tree


def _manifest(repository: Path, frozen_commit: str, frozen_tree: str) -> dict:
    return {
        "schema_version": prep.INPUT_SCHEMA,
        "identity": {
            "frozen_base_commit": frozen_commit,
            "frozen_base_tree": frozen_tree,
            "expected_branch": "codex/phase9-prep-test",
            "expected_worktree_root": str(repository.resolve()),
        },
        "pro_review": {
            "status": "PENDING",
            "scope": "NORMAL_LINUX_DEFAULT_OFF_NON_AUTHORITATIVE_NO_DISPATCH",
            "slim_package_sha256": "a" * 64,
        },
        "formal_boundary": {
            "phase_label": "PHASE9_PREP",
            "formal_phase9_authorized": False,
            "proposed_run_generation": "run4-forensic-proposed-test-01",
            "run_generation_state": "NOT_CREATED",
            "run_mode": "FORENSIC_REPLAY",
            "modeling_consultation_contract": "LEGACY_NOT_APPLICABLE",
            "delivery_capability": "DISABLED",
            "old_generation_access": "READ_ONLY",
            "initial_resume_target": "STEP13_PACKET_REBUILD",
        },
        "capabilities": {key: False for key in prep.CAPABILITY_KEYS},
        "deferred_checks": {key: "DEFERRED" for key in prep.DEFERRED_CHECK_KEYS},
        "planned_paths": {
            key: f"/home/tfisher/.codex/phase9-runtime/test-run/{key.replace('_dir', '')}"
            for key in prep.PLANNED_PATH_KEYS
        },
        "protected_roots": {
            "main_checkout": "/srv/paper-factory/main",
            "phase78_frozen_worktree": "/srv/paper-factory/phase78-frozen",
            "phase78_audit_artifacts": "/srv/paper-factory/phase78-frozen/audit-artifacts",
            "phase9_source_worktree": str(repository.resolve()),
        },
    }


def _write_manifest(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def _error_code(error: pytest.ExceptionInfo[prep.PrepManifestError]) -> str:
    return error.value.code


def _validate(value: dict) -> dict:
    return prep.validate_manifest(value, enforce_fixed_contract=False)


def test_valid_manifest_and_git_report_are_deterministic(tmp_path: Path) -> None:
    repository, frozen_commit, frozen_tree = _repository(tmp_path)
    manifest = _manifest(repository, frozen_commit, frozen_tree)

    identity = _validate(manifest)
    index = repository / ".git" / "index"
    index_bytes = index.read_bytes()
    index_stat = index.stat()
    first_facts = prep.collect_git_facts(repository, identity)
    second_facts = prep.collect_git_facts(repository, identity)
    first = prep.build_report(manifest, first_facts, identity)
    second = prep.build_report(manifest, second_facts, identity)

    assert prep.canonical_bytes(first) == prep.canonical_bytes(second)
    assert first["status"] == "PREP_MANIFEST_VALID"
    assert first["repository"]["frozen_base_is_ancestor"] is True
    assert first["repository"]["untracked_count"] == 0
    assert first["repository"]["untracked_paths_sha256"] == hashlib.sha256(b"").hexdigest()
    assert set(first["deferred_checks"]) == set(prep.DEFERRED_CHECK_KEYS)
    assert index.read_bytes() == index_bytes
    assert index.stat().st_mtime_ns == index_stat.st_mtime_ns
    assert all(
        value is False
        for key, value in first["boundary"].items()
        if key
        not in {
            "manifest_declared_run_generation_state",
            "proposed_run_generation",
        }
    )


def test_cli_rejects_non_fixed_contract_without_index_refresh_or_env_leak(
    tmp_path: Path,
) -> None:
    repository, frozen_commit, frozen_tree = _repository(tmp_path)
    manifest_path = tmp_path / "manifest.json"
    _write_manifest(manifest_path, _manifest(repository, frozen_commit, frozen_tree))
    index = repository / ".git" / "index"
    before_bytes = index.read_bytes()
    before_stat = index.stat()
    environment = dict(os.environ)
    import_trap = tmp_path / "import-trap"
    import_trap.mkdir()
    sitecustomize_sentinel = tmp_path / "sitecustomize-ran"
    (import_trap / "sitecustomize.py").write_text(
        f"from pathlib import Path\nPath({str(sitecustomize_sentinel)!r}).touch()\n",
        encoding="utf-8",
    )
    environment.update(
        {
            "PHASE78_ENABLED": "false",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(import_trap),
            "PHASE9_TEST_SECRET": "must-not-appear-in-output",
        }
    )
    command = [
        sys.executable,
        "-I",
        "-S",
        "-B",
        str(Path(prep.__file__).resolve()),
        str(manifest_path),
        "--repo",
        str(repository),
    ]

    outputs = []
    for _ in range(5):
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            check=False,
        )
        assert result.returncode == 2
        assert result.stderr == b""
        assert b"must-not-appear-in-output" not in result.stdout
        outputs.append(result.stdout)

    assert outputs == [outputs[0]] * 5
    invalid = json.loads(outputs[0])
    assert invalid["status"] == "INVALID"
    assert invalid["error_code"] == "FIXED_CONTRACT_MISMATCH"
    assert invalid["formal_phase9_authorized"] is False
    assert not sitecustomize_sentinel.exists()
    assert index.read_bytes() == before_bytes
    after_stat = index.stat()
    assert after_stat.st_mtime_ns == before_stat.st_mtime_ns
    assert after_stat.st_size == before_stat.st_size


def test_fixed_contract_cannot_be_redefined_by_manifest(tmp_path: Path) -> None:
    repository, frozen_commit, frozen_tree = _repository(tmp_path)
    value = _manifest(repository, frozen_commit, frozen_tree)

    with pytest.raises(prep.PrepManifestError) as error:
        prep.validate_manifest(value)
    assert _error_code(error) == "FIXED_CONTRACT_MISMATCH"


def test_checked_in_templates_keep_pending_fixed_contract_and_receipt_slots() -> None:
    source_root = Path(prep.__file__).resolve().parents[1]
    manifest = prep.load_manifest(
        source_root / "docs/operations/PHASE9_PREP_MANIFEST.template.json"
    )
    identity = prep.validate_manifest(manifest)
    assert tuple(identity["allowed_changed_paths"]) == prep.FIXED_ALLOWED_CHANGED_PATHS
    assert identity["expected_changed_path_modes"] == prep.FIXED_CHANGED_PATH_MODES
    assert identity["expected_gitlinks"] == prep.FIXED_GITLINKS
    assert identity["expected_attribute_files"] == prep.FIXED_ATTRIBUTE_FILES

    evidence = prep.load_manifest(
        source_root / "docs/operations/PHASE9_FORENSIC_EVIDENCE.template.json"
    )
    assert evidence["template_state"] == "NOT_COLLECTED"
    assert evidence["formal_phase9_authorized"] is False
    assert evidence["coordinate"]["run_generation"] is None
    assert evidence["coordinate"]["proposed_run_generation"] == (
        prep.FIXED_PROPOSED_RUN_GENERATION
    )
    assert evidence["entry_gate"]["state_inventory_receipt_sha256"] is None
    assert evidence["terminal"]["state_inventory_receipt_sha256"] is None
    assert evidence["formal_authority_provenance_contract"] == {
        "migration_suffix": "A2_0019_PHASE9_REPLAY_EVIDENCE_ATTESTATION",
        "migration_state": "NOT_APPLIED",
        "replay_evidence_authorization_state": "NOT_ISSUED",
        "replay_evidence_consumption_state": "NOT_CONSUMED",
        "runtime_observation_authorization_state": "NOT_ISSUED",
        "runtime_observation_consumption_state": "NOT_CONSUMED",
        "trusted_acceptance_event_state": "NOT_COLLECTED",
        "runtime_receipt_state": "NOT_COLLECTED",
        "component_receipt_state": "NOT_COLLECTED",
        "acceptance_receipt_state": "NOT_COLLECTED",
        "caller_authored_summary_is_authority_evidence": False,
        "prepopulated_sql_graph_is_authority_evidence": False,
    }
    assert tuple(evidence["acceptance_cases"]) == PHASE9_ACCEPTANCE_CASES
    assert all("_" not in case_id for case_id in evidence["acceptance_cases"])
    for record in evidence["acceptance_cases"].values():
        assert record == {
            "status": "NOT_RUN",
            "test_result_sha256": None,
            "receipt_sha256": None,
        }


def test_duplicate_unknown_and_non_object_json_are_rejected(tmp_path: Path) -> None:
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"schema_version":"one","schema_version":"two"}\n')
    with pytest.raises(prep.PrepManifestError) as error:
        prep.load_manifest(duplicate)
    assert _error_code(error) == "DUPLICATE_JSON_KEY"

    non_object = tmp_path / "array.json"
    non_object.write_text("[]\n")
    with pytest.raises(prep.PrepManifestError) as error:
        prep.load_manifest(non_object)
    assert _error_code(error) == "INVALID_OBJECT"

    repository, frozen_commit, frozen_tree = _repository(tmp_path / "nested")
    value = _manifest(repository, frozen_commit, frozen_tree)
    value["unexpected"] = True
    with pytest.raises(prep.PrepManifestError) as error:
        _validate(value)
    assert _error_code(error) == "INVALID_KEYS"


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        (lambda value: value["capabilities"].__setitem__("provider_call", True), "INVALID_BOOLEAN"),
        (
            lambda value: value["deferred_checks"].__setitem__("outbox_state", "PASS"),
            "INVALID_LITERAL",
        ),
        (
            lambda value: value["formal_boundary"].__setitem__(
                "run_generation_state", "CREATED"
            ),
            "INVALID_LITERAL",
        ),
        (
            lambda value: value["formal_boundary"].__setitem__(
                "formal_phase9_authorized", True
            ),
            "INVALID_BOOLEAN",
        ),
        (
            lambda value: value["pro_review"].__setitem__(
                "status", "NORMAL_FLOW_PASS"
            ),
            "INVALID_LITERAL",
        ),
    ],
)
def test_prep_boundary_cannot_be_promoted(
    tmp_path: Path, mutation, expected_code: str
) -> None:
    repository, frozen_commit, frozen_tree = _repository(tmp_path)
    value = _manifest(repository, frozen_commit, frozen_tree)
    mutation(value)
    with pytest.raises(prep.PrepManifestError) as error:
        _validate(value)
    assert _error_code(error) == expected_code


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        (
            lambda value: value["planned_paths"].__setitem__("cas_dir", "relative/cas"),
            "INVALID_PATH",
        ),
        (
            lambda value: value["planned_paths"].__setitem__(
                "cas_dir", value["protected_roots"]["phase78_frozen_worktree"] + "/cas"
            ),
            "PLANNED_PATH_PROTECTED",
        ),
        (
            lambda value: value["planned_paths"].__setitem__(
                "cas_dir", value["planned_paths"]["database_dir"] + "/cas"
            ),
            "PLANNED_PATH_OVERLAP",
        ),
        (
            lambda value: value["planned_paths"].__setitem__(
                "cas_dir", value["planned_paths"]["database_dir"]
            ),
            "PLANNED_PATH_COLLISION",
        ),
        (
            lambda value: value["protected_roots"].__setitem__(
                "main_checkout",
                value["planned_paths"]["database_dir"] + "/nested-source",
            ),
            "PLANNED_PATH_PROTECTED",
        ),
    ],
)
def test_planned_paths_fail_closed(tmp_path: Path, mutation, expected_code: str) -> None:
    repository, frozen_commit, frozen_tree = _repository(tmp_path)
    value = _manifest(repository, frozen_commit, frozen_tree)
    mutation(value)
    with pytest.raises(prep.PrepManifestError) as error:
        _validate(value)
    assert _error_code(error) == expected_code


@pytest.mark.parametrize(
    ("dirty_kind", "expected_code"),
    [
        ("unstaged", "UNSTAGED_TRACKED_CHANGES"),
        ("staged", "STAGED_CHANGES"),
        ("untracked", "UNTRACKED_FILES"),
        ("ignored", "IGNORED_UNTRACKED_FILES"),
    ],
)
def test_every_dirty_worktree_class_is_rejected(
    tmp_path: Path, dirty_kind: str, expected_code: str
) -> None:
    repository, frozen_commit, frozen_tree = _repository(tmp_path)
    identity = _validate(_manifest(repository, frozen_commit, frozen_tree))
    if dirty_kind in {"unstaged", "staged"}:
        (repository / "tracked.txt").write_text("changed\n", encoding="utf-8")
    if dirty_kind == "staged":
        _run_git(repository, "add", "tracked.txt")
    if dirty_kind == "untracked":
        (repository / "untracked.txt").write_text("new\n", encoding="utf-8")
    if dirty_kind == "ignored":
        (repository / "ignored.tmp").write_text("ignored\n", encoding="utf-8")

    with pytest.raises(prep.PrepManifestError) as error:
        prep.collect_git_facts(repository, identity)
    assert _error_code(error) == expected_code


def test_assume_unchanged_index_flag_is_rejected(tmp_path: Path) -> None:
    repository, frozen_commit, frozen_tree = _repository(tmp_path)
    identity = _validate(_manifest(repository, frozen_commit, frozen_tree))
    _run_git(repository, "update-index", "--assume-unchanged", "tracked.txt")

    with pytest.raises(prep.PrepManifestError) as error:
        prep.collect_git_facts(repository, identity)
    assert _error_code(error) == "INDEX_SPECIAL_FLAG"


def test_committed_change_outside_prep_allowlist_is_rejected(tmp_path: Path) -> None:
    repository, frozen_commit, frozen_tree = _repository(tmp_path)
    identity = _validate(_manifest(repository, frozen_commit, frozen_tree))
    (repository / "tracked.txt").write_text("committed drift\n", encoding="utf-8")
    _run_git(repository, "add", "tracked.txt")
    _run_git(repository, "commit", "-q", "-m", "out of scope")

    with pytest.raises(prep.PrepManifestError) as error:
        prep.collect_git_facts(repository, identity)
    assert _error_code(error) == "PREP_CHANGE_SCOPE_MISMATCH"


def test_allowed_prep_path_cannot_be_a_symlink(tmp_path: Path) -> None:
    repository, frozen_commit, frozen_tree = _repository(tmp_path)
    identity = _validate(_manifest(repository, frozen_commit, frozen_tree))
    identity["allowed_changed_paths"] = ["prep.txt"]
    identity["expected_changed_path_modes"] = {"prep.txt": "100644"}
    (repository / "prep-target.txt").write_text("target\n", encoding="utf-8")
    (repository / "prep.txt").symlink_to("prep-target.txt")
    _run_git(repository, "add", "prep.txt")
    _run_git(repository, "commit", "-q", "-m", "symlink prep path")

    with pytest.raises(prep.PrepManifestError) as error:
        prep.collect_git_facts(repository, identity)
    assert _error_code(error) == "PREP_PATH_MODE_MISMATCH"


def test_staged_gitlink_pointer_change_is_rejected(tmp_path: Path) -> None:
    repository, _, _ = _repository(tmp_path)
    target_commit = _run_git(repository, "rev-parse", "HEAD^{commit}")
    _run_git(
        repository,
        "update-index",
        "--add",
        "--cacheinfo",
        f"160000,{target_commit},vendor",
    )
    _run_git(repository, "commit", "-q", "-m", "add gitlink")
    frozen_commit = _run_git(repository, "rev-parse", "HEAD^{commit}")
    frozen_tree = _run_git(repository, "rev-parse", "HEAD^{tree}")
    identity = _validate(_manifest(repository, frozen_commit, frozen_tree))
    identity["expected_gitlinks"] = {"vendor": target_commit}
    (repository / "vendor").mkdir()
    prep.collect_git_facts(repository, identity)

    _run_git(
        repository,
        "update-index",
        "--cacheinfo",
        f"160000,{frozen_commit},vendor",
    )
    with pytest.raises(prep.PrepManifestError) as error:
        prep.collect_git_facts(repository, identity)
    assert _error_code(error) == "GITLINK_INVENTORY_MISMATCH"


def test_wrong_branch_root_and_frozen_tree_are_rejected(tmp_path: Path) -> None:
    repository, frozen_commit, frozen_tree = _repository(tmp_path)

    wrong_branch = _manifest(repository, frozen_commit, frozen_tree)
    wrong_branch["identity"]["expected_branch"] = "codex/wrong"
    with pytest.raises(prep.PrepManifestError) as error:
        prep.collect_git_facts(repository, _validate(wrong_branch))
    assert _error_code(error) == "BRANCH_MISMATCH"

    wrong_tree = _manifest(repository, frozen_commit, "0" * 40)
    with pytest.raises(prep.PrepManifestError) as error:
        prep.collect_git_facts(repository, _validate(wrong_tree))
    assert _error_code(error) == "FROZEN_IDENTITY_MISMATCH"

    other = tmp_path / "other"
    other.mkdir()
    wrong_root = _manifest(repository, frozen_commit, frozen_tree)
    wrong_root["identity"]["expected_worktree_root"] = str(other.resolve())
    wrong_root["protected_roots"]["phase9_source_worktree"] = str(other.resolve())
    with pytest.raises(prep.PrepManifestError) as error:
        prep.collect_git_facts(repository, _validate(wrong_root))
    assert _error_code(error) == "REPOSITORY_ROOT_MISMATCH"


def test_coordinate_change_during_read_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, frozen_commit, frozen_tree = _repository(tmp_path)
    identity = _validate(_manifest(repository, frozen_commit, frozen_tree))
    real_git = prep._git
    head_reads = 0

    def changing_git(repo, arguments, **kwargs):
        nonlocal head_reads
        result = real_git(repo, arguments, **kwargs)
        if tuple(arguments) == ("rev-parse", "HEAD^{commit}"):
            head_reads += 1
            if head_reads == 2:
                return subprocess.CompletedProcess(
                    result.args,
                    0,
                    b"0" * 40 + b"\n",
                    b"",
                )
        return result

    monkeypatch.setattr(prep, "_git", changing_git)
    with pytest.raises(prep.PrepManifestError) as error:
        prep.collect_git_facts(repository, identity)
    assert _error_code(error) == "REPOSITORY_CHANGED_DURING_READ"


def test_manifest_symlink_hardlink_invalid_utf8_and_oversize_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.json"
    source.write_text("{}\n", encoding="utf-8")
    symlink = tmp_path / "symlink.json"
    symlink.symlink_to(source)
    with pytest.raises(prep.PrepManifestError) as error:
        prep.load_manifest(symlink)
    assert _error_code(error) == "MANIFEST_NOT_REGULAR"

    hardlink = tmp_path / "hardlink.json"
    os.link(source, hardlink)
    with pytest.raises(prep.PrepManifestError) as error:
        prep.load_manifest(source)
    assert _error_code(error) == "MANIFEST_LINK_COUNT"

    invalid_utf8 = tmp_path / "invalid.json"
    invalid_utf8.write_bytes(b"{\xff}\n")
    with pytest.raises(prep.PrepManifestError) as error:
        prep.load_manifest(invalid_utf8)
    assert _error_code(error) == "MANIFEST_NOT_UTF8"

    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b"x" * (prep.MAX_MANIFEST_BYTES + 1))
    with pytest.raises(prep.PrepManifestError) as error:
        prep.load_manifest(oversized)
    assert _error_code(error) == "MANIFEST_SIZE"

    huge_integer = tmp_path / "huge-integer.json"
    huge_integer.write_text('{"value":' + "1" * 5000 + "}\n", encoding="utf-8")
    with pytest.raises(prep.PrepManifestError) as error:
        prep.load_manifest(huge_integer)
    assert _error_code(error) == "MANIFEST_NOT_JSON"

    deeply_nested = tmp_path / "deep.json"
    deeply_nested.write_text("{}\n", encoding="utf-8")

    def recursion_error(*args, **kwargs):
        raise RecursionError("synthetic parser depth")

    monkeypatch.setattr(prep.json, "loads", recursion_error)
    with pytest.raises(prep.PrepManifestError) as error:
        prep.load_manifest(deeply_nested)
    assert _error_code(error) == "MANIFEST_NOT_JSON"


def test_phase78_enabled_environment_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PHASE78_ENABLED", "true")
    with pytest.raises(prep.PrepManifestError) as error:
        prep.validate_environment()
    assert _error_code(error) == "PHASE78_ENABLED"

    for false_value in ("", "0", "false", "NO", "off"):
        monkeypatch.setenv("PHASE78_ENABLED", false_value)
        prep.validate_environment()


def test_entrypoint_requires_isolated_python_flags(tmp_path: Path) -> None:
    repository, frozen_commit, frozen_tree = _repository(tmp_path)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(_manifest(repository, frozen_commit, frozen_tree)),
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            sys.executable,
            str(Path(prep.__file__).resolve()),
            str(manifest_path),
            "--repo",
            str(repository),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert result.returncode == 2
    assert result.stderr == b""
    invalid = json.loads(result.stdout)
    assert invalid["status"] == "INVALID"
    assert invalid["error_code"] == "PYTHON_ISOLATION_REQUIRED"
    assert invalid["formal_phase9_authorized"] is False


def test_external_diff_configuration_is_never_executed(tmp_path: Path) -> None:
    repository, frozen_commit, frozen_tree = _repository(tmp_path)
    identity = _validate(_manifest(repository, frozen_commit, frozen_tree))
    sentinel = tmp_path / "external-diff-ran"
    _run_git(repository, "config", "diff.external", f"touch {sentinel}")
    (repository / "tracked.txt").write_text("changed\n", encoding="utf-8")

    with pytest.raises(prep.PrepManifestError) as error:
        prep.collect_git_facts(repository, identity)
    assert _error_code(error) == "UNSTAGED_TRACKED_CHANGES"
    assert not sentinel.exists()


def test_fsmonitor_configuration_is_never_executed(tmp_path: Path) -> None:
    repository, frozen_commit, frozen_tree = _repository(tmp_path)
    identity = _validate(_manifest(repository, frozen_commit, frozen_tree))
    sentinel = tmp_path / "fsmonitor-ran"
    hook = tmp_path / "fsmonitor-hook"
    hook.write_text(
        f"#!/bin/sh\ntouch '{sentinel}'\nexit 0\n",
        encoding="utf-8",
    )
    hook.chmod(0o700)
    _run_git(repository, "config", "core.fsmonitor", str(hook))

    facts = prep.collect_git_facts(repository, identity)

    assert facts["unstaged_tracked_changes"] is False
    assert not sentinel.exists()


def test_repository_local_clean_filter_is_rejected_before_execution(
    tmp_path: Path,
) -> None:
    repository, frozen_commit, frozen_tree = _repository(tmp_path)
    identity = _validate(_manifest(repository, frozen_commit, frozen_tree))
    sentinel = tmp_path / "clean-filter-ran"
    hook = tmp_path / "clean-filter"
    hook.write_text(
        f"#!/bin/sh\ntouch '{sentinel}'\ncat\n",
        encoding="utf-8",
    )
    hook.chmod(0o700)
    _run_git(repository, "config", "filter.evil.clean", str(hook))
    info_attributes = Path(
        _run_git(repository, "rev-parse", "--git-path", "info/attributes")
    )
    if not info_attributes.is_absolute():
        info_attributes = repository / info_attributes
    info_attributes.parent.mkdir(parents=True, exist_ok=True)
    info_attributes.write_text("*.txt filter=evil\n", encoding="utf-8")
    (repository / "tracked.txt").write_text("changed\n", encoding="utf-8")

    with pytest.raises(prep.PrepManifestError) as error:
        prep.collect_git_facts(repository, identity)
    assert _error_code(error) == "LOCAL_ATTRIBUTES_FORBIDDEN"
    assert not sentinel.exists()


def test_git_timeout_failure_and_output_limit_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, _, _ = _repository(tmp_path)
    monkeypatch.setattr(prep, "GIT_TIMEOUT_SECONDS", 0)
    with pytest.raises(prep.PrepManifestError) as error:
        prep._git(repository, ("rev-parse", "HEAD^{commit}"))
    assert _error_code(error) == "GIT_UNAVAILABLE"

    monkeypatch.setattr(prep, "GIT_TIMEOUT_SECONDS", 10)
    monkeypatch.setattr(prep, "MAX_GIT_OUTPUT_BYTES", 1)
    with pytest.raises(prep.PrepManifestError) as error:
        prep._git(repository, ("rev-parse", "HEAD^{commit}"))
    assert _error_code(error) == "GIT_OUTPUT_LIMIT"


def test_git_process_uses_exact_sanitized_argv_and_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, _, _ = _repository(tmp_path)
    real_popen = prep.subprocess.Popen
    observed: dict = {}

    def capture(command, **kwargs):
        observed["command"] = command
        observed["kwargs"] = kwargs
        return real_popen(command, **kwargs)

    monkeypatch.setattr(prep.subprocess, "Popen", capture)
    result = prep._git(repository, ("rev-parse", "HEAD^{commit}"))

    assert result.returncode == 0
    assert observed["command"][0:3] == (
        "git",
        "--no-optional-locks",
        "--no-replace-objects",
    )
    assert "core.fsmonitor=false" in observed["command"]
    assert "core.attributesFile=/dev/null" in observed["command"]
    assert observed["kwargs"]["env"] == prep._git_environment()
    assert observed["kwargs"]["stdin"] is subprocess.DEVNULL
    assert observed["kwargs"]["start_new_session"] is True
    assert "shell" not in observed["kwargs"]

    with pytest.raises(prep.PrepManifestError) as error:
        prep._git(repository, ("fetch", "origin"))
    assert _error_code(error) == "GIT_COMMAND_FORBIDDEN"


def test_source_has_no_runtime_or_write_dependencies() -> None:
    source_path = Path(prep.__file__).resolve()
    source = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_roots.add(node.module.split(".", 1)[0])

    assert imported_roots.isdisjoint(
        {
            "factory_core",
            "sqlite3",
            "socket",
            "requests",
            "urllib",
            "http",
            "asyncio",
        }
    )
    assert imported_roots <= set(sys.stdlib_module_names) | {"__future__"}
    assert ".write_text(" not in source
    assert ".write_bytes(" not in source
    assert "--output" not in source
    assert "--apply" not in source
    assert "--confirm" not in source
    assert 'add_argument("--run"' not in source
