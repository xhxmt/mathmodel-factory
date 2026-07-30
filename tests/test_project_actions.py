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
