import json

from web.backend import project_actions


class State:
    status = type("Status", (), {"value": "paused"})()
    revision = 7


class Worker:
    pid = 4242
    log_path = "/tmp/worker.log"


def test_web_pause_action_calls_factory_service_directly(monkeypatch, tmp_path):
    captured = {}

    class Service:
        def __init__(self, root):
            captured["root"] = root

        def pause(self, project, *, expected_revision=None):
            captured["call"] = (project, expected_revision)
            return State()

    monkeypatch.setattr(project_actions, "FactoryService", Service)

    result = project_actions.run_action(
        tmp_path, "pause", "demo", expected_revision=6
    )

    assert result.ok is True
    assert captured == {"root": tmp_path, "call": ("demo", 6)}
    assert json.loads(result.stdout) == {"revision": 7, "status": "paused"}


def test_web_rejects_unknown_project_action_without_service(monkeypatch, tmp_path):
    monkeypatch.setattr(
        project_actions,
        "FactoryService",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not construct")),
    )

    result = project_actions.run_action(tmp_path, "delete", "demo")

    assert result.ok is False
    assert "unsupported" in result.stderr


def test_web_action_forwards_expected_revision(monkeypatch, tmp_path):
    captured = {}

    class Service:
        def __init__(self, _root):
            pass

        def kill(self, project, *, expected_revision=None):
            captured["call"] = (project, expected_revision)
            return State()

    monkeypatch.setattr(project_actions, "FactoryService", Service)

    result = project_actions.run_action(
        tmp_path, "kill", "demo", expected_revision=42
    )

    assert result.ok is True
    assert captured["call"] == ("demo", 42)


def test_web_resume_launches_worker_and_returns_final_revision(monkeypatch, tmp_path):
    captured = {}

    class RunningState:
        status = type("Status", (), {"value": "running"})()
        revision = 9

    class Service:
        def __init__(self, root):
            captured["root"] = root

        def resume_and_start(self, project, *, expected_revision=None):
            captured["call"] = (project, expected_revision)
            return RunningState(), Worker()

    monkeypatch.setattr(project_actions, "FactoryService", Service)

    result = project_actions.run_action(
        tmp_path, "resume", "demo", expected_revision=7
    )

    assert result.ok is True
    assert captured == {"root": tmp_path, "call": ("demo", 7)}
    assert json.loads(result.stdout) == {
        "log": "/tmp/worker.log",
        "revision": 9,
        "status": "running",
        "worker_pid": 4242,
    }


def test_web_action_reports_service_error(monkeypatch, tmp_path):
    class Service:
        def __init__(self, _root):
            pass

        def pause(self, *_args, **_kwargs):
            raise RuntimeError("revision conflict")

    monkeypatch.setattr(project_actions, "FactoryService", Service)

    result = project_actions.run_action(tmp_path, "pause", "demo")

    assert result.ok is False
    assert result.stdout == ""
    assert result.stderr == "revision conflict"


def test_web_grant_reopen_records_the_grant_then_resumes(monkeypatch, tmp_path):
    calls = []

    class Granted:
        revision = 9

    class Service:
        def __init__(self, _root):
            pass

        def grant_semantic_reopen(self, project, *, reason, expected_revision=None):
            calls.append(("grant", project, reason, expected_revision))
            return Granted()

        def resume_and_start(self, project, *, expected_revision=None):
            calls.append(("resume", project, expected_revision))
            return State(), Worker()

    monkeypatch.setattr(project_actions, "FactoryService", Service)

    result = project_actions.run_action(
        tmp_path, "grant_reopen", "demo", expected_revision=8, reason="upstream fix is real"
    )

    assert result.ok is True
    assert calls == [("grant", "demo", "upstream fix is real", 8), ("resume", "demo", 9)]


def test_web_grant_reopen_requires_a_reason(monkeypatch, tmp_path):
    monkeypatch.setattr(
        project_actions,
        "FactoryService",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not construct")),
    )

    result = project_actions.run_action(tmp_path, "grant_reopen", "demo", reason="  ")

    assert result.ok is False
    assert "requires a reason" in result.stderr


def test_web_retry_step_records_the_retry_then_starts(monkeypatch, tmp_path):
    calls = []

    class Retried:
        revision = 11

    class Handle:
        state = State()
        pid = 4242
        log_path = "/tmp/worker.log"

    class Service:
        def __init__(self, _root):
            pass

        def retry_step(self, project, *, step_id, reason, expected_revision=None):
            calls.append(("retry", project, step_id, reason, expected_revision))
            return Retried()

        def start(self, project, *, expected_revision=None):
            calls.append(("start", project, expected_revision))
            return Handle()

    monkeypatch.setattr(project_actions, "FactoryService", Service)

    result = project_actions.run_action(
        tmp_path, "retry_step", "demo", expected_revision=10, reason="fixed", step=14
    )

    assert result.ok is True, result.stderr
    assert calls == [("retry", "demo", 14, "fixed", 10), ("start", "demo", 11)]


def test_web_retry_step_requires_a_step(monkeypatch, tmp_path):
    monkeypatch.setattr(
        project_actions,
        "FactoryService",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not construct")),
    )

    result = project_actions.run_action(tmp_path, "retry_step", "demo", reason="fixed")

    assert result.ok is False
    assert "requires a step" in result.stderr


class _Ready:
    status = type("Status", (), {"value": "ready"})()
    revision = 11


def test_web_retry_step_reports_a_recorded_retry_whose_start_failed(monkeypatch, tmp_path):
    class Service:
        def __init__(self, _root):
            pass

        def retry_step(self, project, *, step_id, reason, expected_revision=None):
            return _Ready()

        def start(self, project, *, expected_revision=None):
            raise RuntimeError("runner launch failed")

    monkeypatch.setattr(project_actions, "FactoryService", Service)

    result = project_actions.run_action(
        tmp_path, "retry_step", "demo", expected_revision=10, reason="fixed", step=14
    )

    assert result.ok is False
    assert json.loads(result.stdout) == {"revision": 11, "started": False, "status": "ready"}
    assert "recorded at revision 11" in result.stderr
    assert "runner launch failed" in result.stderr
    assert "resume the project" in result.stderr


def test_web_grant_reopen_reports_a_recorded_grant_whose_resume_failed(monkeypatch, tmp_path):
    class Paused:
        status = type("Status", (), {"value": "paused"})()
        revision = 9

    class Service:
        def __init__(self, _root):
            pass

        def grant_semantic_reopen(self, project, *, reason, expected_revision=None):
            return Paused()

        def resume_and_start(self, project, *, expected_revision=None):
            raise RuntimeError("consultation projection drift")

    monkeypatch.setattr(project_actions, "FactoryService", Service)

    result = project_actions.run_action(
        tmp_path, "grant_reopen", "demo", expected_revision=8, reason="upstream fix is real"
    )

    assert result.ok is False
    assert json.loads(result.stdout)["revision"] == 9
    assert "grant was recorded at revision 9" in result.stderr
    assert "Do not repeat the grant" in result.stderr
