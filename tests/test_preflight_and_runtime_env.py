"""Phase-0 run safety: disk-backed TMPDIR and the read-only route preflight."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

from factory_core import preflight as pf
from factory_core import runtime_env

MOUNTS = "\n".join(
    [
        "/dev/vda4 / ext4 rw,relatime 0 0",
        "tmpfs /tmp tmpfs rw,nosuid,nodev,size=4096000k 0 0",
        "tmpfs /run/user/1000 tmpfs rw 0 0",
        "/dev/vdb1 /data\\040disk xfs rw 0 0",
    ]
)


# --------------------------------------------------------------------------
# runtime_env
# --------------------------------------------------------------------------


def test_filesystem_type_uses_the_longest_matching_mount():
    assert runtime_env.filesystem_type("/tmp/x/y", MOUNTS) == "tmpfs"
    assert runtime_env.filesystem_type("/tmpfoo", MOUNTS) == "ext4"
    assert runtime_env.filesystem_type("/home/u/project", MOUNTS) == "ext4"
    assert runtime_env.filesystem_type("/data disk/run", MOUNTS) == "xfs"


def test_filesystem_type_is_unknown_without_a_mount_table(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_env, "_DEFAULT_MOUNTS", tmp_path / "absent")
    assert runtime_env.filesystem_type("/tmp") is None


def test_memory_tmpdir_is_moved_under_run_state(tmp_path):
    overrides = runtime_env.disk_tmpdir_overrides(tmp_path, {"TMPDIR": "/tmp"}, mounts_text=MOUNTS)
    target = tmp_path.resolve() / "run_state" / "tmp"
    assert overrides == {"TMPDIR": str(target)}
    assert target.is_dir()


def test_disk_tmpdir_is_left_alone(tmp_path):
    disk = tmp_path / "disk"
    disk.mkdir()
    mounts = MOUNTS + f"\n/dev/vdc1 {tmp_path} ext4 rw 0 0"
    assert runtime_env.disk_tmpdir_overrides(tmp_path, {"TMPDIR": str(disk)}, mounts_text=mounts) == {}
    assert not (tmp_path / "run_state").exists()


def test_a_stale_tmpdir_is_judged_by_where_tempfile_falls_back(tmp_path):
    # TMPDIR names a disk path that does not exist: tempfile would skip it and
    # use /tmp, which is tmpfs here, so the run must still be redirected.
    environ = {"TMPDIR": str(tmp_path / "absent-disk-dir")}
    assert runtime_env.effective_tempdir(environ) == "/tmp"
    overrides = runtime_env.disk_tmpdir_overrides(tmp_path, environ, mounts_text=MOUNTS)
    assert overrides == {"TMPDIR": str(tmp_path.resolve() / "run_state" / "tmp")}


def test_context_restores_tempfile_cache_even_when_tmpdir_is_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("TMPDIR", raising=False)
    monkeypatch.setattr(tempfile, "tempdir", None)
    with runtime_env.disk_backed_tmpdir(tmp_path, mounts_text=MOUNTS):
        pass
    assert tempfile.tempdir is None


def test_unknown_filesystem_is_left_alone(tmp_path):
    assert runtime_env.disk_tmpdir_overrides(tmp_path, {"TMPDIR": "/tmp"}, mounts_text="") == {}


def test_operator_choice_wins(tmp_path):
    chosen = tmp_path / "chosen"
    overrides = runtime_env.disk_tmpdir_overrides(
        tmp_path, {"TMPDIR": "/tmp", "FACTORY_TMPDIR": str(chosen)}, mounts_text=MOUNTS
    )
    assert overrides == {"TMPDIR": str(chosen)}
    assert chosen.is_dir()


def test_read_only_caller_creates_nothing(tmp_path):
    overrides = runtime_env.disk_tmpdir_overrides(
        tmp_path, {"TMPDIR": "/tmp"}, mounts_text=MOUNTS, create=False
    )
    assert overrides["TMPDIR"].endswith("run_state/tmp")
    assert not (tmp_path / "run_state").exists()


def test_context_applies_then_restores_the_process_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", "/tmp")
    monkeypatch.delenv("FACTORY_TMPDIR", raising=False)
    cached = tempfile.tempdir
    with runtime_env.disk_backed_tmpdir(tmp_path, mounts_text=MOUNTS) as overrides:
        assert os.environ["TMPDIR"] == overrides["TMPDIR"]
        assert tempfile.gettempdir() == overrides["TMPDIR"]
    assert os.environ["TMPDIR"] == "/tmp"
    assert tempfile.tempdir == cached


def test_worker_launch_carries_the_disk_tmpdir(tmp_path, monkeypatch):
    """The spawned engine worker - and everything it starts - inherits TMPDIR."""

    import subprocess

    from factory_core import service as service_module

    captured = {}

    class FakePopen:
        def __init__(self, argv, **kwargs):
            captured.update(kwargs)
            self.pid = 4242

    monkeypatch.setattr(service_module, "disk_tmpdir_overrides", lambda root: {"TMPDIR": "/disk/tmp"})
    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    launcher = service_module.WorkerLauncher(tmp_path)
    project = tmp_path / "ongoing" / "demo"
    project.mkdir(parents=True)
    with pytest.raises(Exception):
        # No state database exists; the launcher fails after spawning, which is
        # all this test needs to observe.
        launcher.spawn(project)
    assert captured["env"]["TMPDIR"] == "/disk/tmp"


# --------------------------------------------------------------------------
# preflight
# --------------------------------------------------------------------------


def _deepseek_entry():
    return {"backend": "openai", "model": "deepseek-chat", "effort": "", "base_url": "https://api.deepseek.com",
            "key_env": "DEEPSEEK_API_KEY"}


def _http(status, body=b"{}"):
    calls = []

    def get(url, headers, timeout):
        calls.append((url, dict(headers)))
        return status, body

    get.calls = calls
    return get


def test_a_402_balance_fails_the_model_without_leaking_the_key(tmp_path):
    http = _http(402)
    key = "test-only-placeholder-value"
    check = pf.check_model("deepseek-chat", _deepseek_entry(), tmp_path,
                           environ={"DEEPSEEK_API_KEY": key}, http_get=http)
    assert check.status == pf.FAIL
    assert "402" in check.detail
    assert key not in json.dumps(check.__dict__)
    [(url, headers)] = http.calls
    assert url == pf.DEEPSEEK_BALANCE_URL
    # The credential travels only in the request header, never in the report.
    assert headers["Authorization"].split(" ", 1) == ["Bearer", key]


def test_an_exhausted_balance_reported_as_200_still_fails(tmp_path):
    body = json.dumps({"is_available": False, "balance_infos": []}).encode()
    check = pf.check_model("deepseek-chat", _deepseek_entry(), tmp_path,
                           environ={"DEEPSEEK_API_KEY": "k"}, http_get=_http(200, body))
    assert check.status == pf.FAIL


def test_an_available_balance_passes(tmp_path):
    body = json.dumps({"is_available": True, "balance_infos": [{"currency": "CNY", "total_balance": "9.50"}]}).encode()
    check = pf.check_model("deepseek-chat", _deepseek_entry(), tmp_path,
                           environ={"DEEPSEEK_API_KEY": "k"}, http_get=_http(200, body))
    assert check.status == pf.PASS
    assert "9.50 CNY" in check.detail


def test_a_missing_credential_fails_before_any_network_call(tmp_path):
    http = _http(200)
    check = pf.check_model("deepseek-chat", _deepseek_entry(), tmp_path, environ={}, http_get=http)
    assert check.status == pf.FAIL
    assert "DEEPSEEK_API_KEY is not in the environment" in check.detail
    assert http.calls == []


def test_an_unreachable_provider_is_unknown_not_pass(tmp_path):
    def down(url, headers, timeout):
        raise OSError("network is unreachable")

    check = pf.check_model("deepseek-chat", _deepseek_entry(), tmp_path,
                           environ={"DEEPSEEK_API_KEY": "k"}, http_get=down)
    assert check.status == pf.UNKNOWN


def test_offline_mode_never_claims_a_provider_works(tmp_path):
    check = pf.check_model("deepseek-chat", _deepseek_entry(), tmp_path,
                           environ={"DEEPSEEK_API_KEY": "k"}, http_get=None)
    assert check.status == pf.UNKNOWN


def test_a_missing_codex_cli_fails(tmp_path):
    entry = {"backend": "codex", "model": "", "effort": "", "base_url": "", "key_env": ""}
    check = pf.check_model("codex", entry, tmp_path, environ={"CODEX_CLI_PATH": "/nonexistent/codex"}, http_get=None)
    assert check.status == pf.FAIL


def test_dsh_on_an_old_node_fails(tmp_path, monkeypatch):
    node = tmp_path / "node"
    node.write_text("#!/bin/sh\necho v20.19.2\n")
    node.chmod(0o755)
    entry = {"backend": "dsh", "model": "deepseek-flash", "effort": "", "base_url": "", "key_env": "DEEPSEEK_API_KEY"}
    check = pf.check_model("dsh-deepseek", entry, tmp_path,
                           environ={"DSH_NODE": str(node), "DSH_CLI": str(node), "DEEPSEEK_API_KEY": "k"},
                           http_get=None)
    assert check.status == pf.FAIL
    assert "v20" in check.detail


def test_step_route_verdicts(tmp_path, monkeypatch):
    statuses = {"a": pf.FAIL, "b": pf.PASS, "c": pf.FAIL, "d": pf.UNKNOWN}
    monkeypatch.setattr(pf, "_entry", lambda root, model_id: {"backend": "x"})
    monkeypatch.setattr(pf, "check_model", lambda model_id, *a, **k: pf.Check(f"model:{model_id}", statuses[model_id], ""))
    chains = {0: ["b", "a"], 1: ["a", "b"], 2: ["a", "c"], 3: ["d", "a"]}
    monkeypatch.setattr(pf, "step_candidates", lambda root, project, step, defaults, **_kwargs: chains[step])
    steps, models = pf.check_routes(tmp_path, "_default", [0, 1, 2, 3], environ={}, http_get=None)
    assert [check.status for check in steps] == [pf.PASS, pf.WARN, pf.FAIL, pf.WARN]
    assert sorted(check.name for check in models) == ["model:a", "model:b", "model:c", "model:d"]


def test_cli_exits_nonzero_when_the_only_judge_route_has_no_balance(tmp_path, monkeypatch, capsys):
    """Exit condition of phase 0: a 402 is reported before any Step runs."""

    from factory_core import cli

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only-placeholder-0000")
    monkeypatch.setattr(cli, "_default_http_get", lambda: _http(402))
    code = cli.main(["preflight", "--steps", "13", "--min-free-gb", "0"])
    report = json.loads(capsys.readouterr().out)
    assert code == 3
    assert report["status"] == pf.FAIL
    step = next(check for check in report["checks"] if check["name"].startswith("step:13"))
    assert step["status"] == pf.FAIL
    assert "test-only-placeholder-0000" not in json.dumps(report)


def test_scratch_space_reports_a_shortfall(tmp_path):
    checks = pf.check_scratch_space(tmp_path, None, min_free_bytes=2**62, environ={"TMPDIR": str(tmp_path)},
                                    mounts_text=MOUNTS)
    assert [check.status for check in checks] == [pf.FAIL]


@pytest.mark.parametrize("body", [
    b"[]",
    b'"ok"',
    b"{}",
    json.dumps({"balance_infos": "n/a"}).encode(),
    json.dumps({"is_available": True, "balance_infos": "n/a"}).encode(),
    json.dumps({"balance_infos": [{"currency": "CNY", "total_balance": "9"}]}).encode(),
])
def test_a_balance_body_of_the_wrong_shape_is_unknown_never_pass(tmp_path, body):
    check = pf.check_model("deepseek-chat", _deepseek_entry(), tmp_path,
                           environ={"DEEPSEEK_API_KEY": "k"}, http_get=_http(200, body))
    assert check.status == pf.UNKNOWN, body


def _proxy_entry():
    return {"backend": "openai", "model": "m", "effort": "", "base_url": "https://proxy.example/v1",
            "key_env": "PROXY_KEY"}


@pytest.mark.parametrize("body, status", [
    (json.dumps({"object": "list", "data": [{"id": "m"}]}).encode(), pf.PASS),
    (b"<html>sign in</html>", pf.UNKNOWN),
    (b"{}", pf.UNKNOWN),
    (b"[]", pf.UNKNOWN),
])
def test_an_openai_compatible_route_passes_only_on_a_model_list(tmp_path, body, status):
    check = pf.check_model("proxy-model", _proxy_entry(), tmp_path,
                           environ={"PROXY_KEY": "k"}, http_get=_http(200, body))
    assert check.status == status, body


def test_a_directory_that_refuses_a_write_is_not_usable(tmp_path, monkeypatch):
    import errno

    real_write = os.write

    def full_disk(descriptor, data):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(runtime_env.os, "write", full_disk)
    assert runtime_env._usable_directory(str(tmp_path)) is False
    assert list(tmp_path.iterdir()) == []  # the probe file was removed
    monkeypatch.setattr(runtime_env.os, "write", real_write)
    assert runtime_env._usable_directory(str(tmp_path)) is True
    assert list(tmp_path.iterdir()) == []


def test_dsh_launcher_must_be_a_file(tmp_path):
    node = tmp_path / "node"
    node.write_text("#!/bin/sh\necho v24.1.0\n")
    node.chmod(0o755)
    launcher_dir = tmp_path / "dsh"
    launcher_dir.mkdir()
    entry = {"backend": "dsh", "model": "deepseek-flash", "effort": "", "base_url": "", "key_env": "DEEPSEEK_API_KEY"}
    check = pf.check_model("dsh-deepseek", entry, tmp_path,
                           environ={"DSH_NODE": str(node), "DSH_CLI": str(launcher_dir), "DEEPSEEK_API_KEY": "k"},
                           http_get=None)
    assert check.status == pf.FAIL
    assert "launcher not found" in check.detail


def test_a_file_where_the_scratch_directory_belongs_fails(tmp_path):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    for tmpdir in (blocker, blocker / "nested"):
        [check] = pf.check_scratch_space(tmp_path, None, min_free_bytes=0, environ={"FACTORY_TMPDIR": str(tmpdir)},
                                         mounts_text=MOUNTS)
        assert check.status == pf.FAIL, tmpdir
        assert "is not a directory" in check.detail


def _project_with_joint_modeling(root: Path, enabled: bool) -> Path:
    import sqlite3

    project = root / "ongoing" / "joint_demo"
    database = project / ".factory" / "state.db"
    database.parent.mkdir(parents=True)
    connection = sqlite3.connect(database)
    connection.execute("create table events (revision integer, type text, created_at real, step integer, "
                       "attempt integer, payload_json text)")
    payload = {"joint_modeling": {"enabled": enabled, "model": "claude"}}
    connection.execute("insert into events values (7, 'JOINT_MODELING_CONFIGURED', 1.0, null, null, ?)",
                       (json.dumps(payload),))
    connection.commit()
    connection.close()
    return project


def test_joint_modeling_configuration_is_read_without_touching_the_database(tmp_path):
    project = _project_with_joint_modeling(tmp_path, True)
    database = project / ".factory" / "state.db"
    before = database.read_bytes()
    assert pf.joint_modeling_enabled(project) is True
    assert database.read_bytes() == before
    assert sorted(path.name for path in database.parent.iterdir()) == ["state.db"]
    assert pf.joint_modeling_enabled(_project_with_joint_modeling(tmp_path / "off", False)) is False
    assert pf.joint_modeling_enabled(tmp_path / "no-project") is False


def test_step_2_with_joint_modeling_checks_the_pinned_executor(tmp_path, monkeypatch):
    from factory_core import joint_modeling_executor

    project = _project_with_joint_modeling(tmp_path, True)
    monkeypatch.setattr(joint_modeling_executor, "executor_blocker", lambda: "claude CLI not found")
    monkeypatch.setattr(pf, "step_candidates", lambda *a, **k: pytest.fail("Step 2 must not use the candidate chain"))
    steps, models = pf.check_routes(tmp_path, project.name, [2], project_dir=project, environ={}, http_get=None)
    assert [check.status for check in steps] == [pf.FAIL]
    assert "claude CLI not found" in steps[0].detail
    assert [check.name for check in models] == ["model:joint-claude"]

    monkeypatch.setattr(joint_modeling_executor, "executor_blocker", lambda: "")
    steps, _ = pf.check_routes(tmp_path, project.name, [2], project_dir=project, environ={}, http_get=None)
    assert [check.status for check in steps] == [pf.PASS]


@pytest.mark.parametrize("argv", [
    ["preflight", "--min-free-gb", "-1"],
    ["preflight", "--min-free-gb", "nan"],
    ["preflight", "--steps", "4-x"],
    ["preflight", "--steps", "17"],
    ["preflight", "--steps", "9-3"],
])
def test_cli_rejects_bad_preflight_arguments_as_usage_errors(argv, capsys):
    from factory_core import cli

    with pytest.raises(SystemExit) as excinfo:
        cli.main(argv)
    assert excinfo.value.code == 2
    assert "Traceback" not in capsys.readouterr().err
