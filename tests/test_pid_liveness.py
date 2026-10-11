"""Liveness probing, and the fail-closed policy around it.

``runner_pid`` exists so a second writer cannot start on a project another runner
is advancing.  Deciding the recorded process is *gone* is therefore the dangerous
answer, and ``os.kill(pid, 0)`` does not answer it with one code:

* ``ESRCH`` means gone;
* ``EPERM`` means it exists and belongs to somebody else - reading that as death
  would emit ``RUNNER_INTERRUPTED`` for a live runner and let a second one take
  over a project still being advanced, which is the bug these tests pin;
* anything unreadable is treated as still there, because a false "live" costs a
  refused start and a false "dead" costs two writers.

The module also exists because two copies had drifted apart: ``engine`` swallowed
every ``OSError`` and ``service`` swallowed two specific ones, and both read
``EPERM`` as death.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from factory_core import engine as engine_module
from factory_core import liveness
from factory_core import service as service_module
from factory_core.liveness import pid_is_live


def _child():
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])


# ------------------------------------------------------------------ the answers
def test_a_live_process_of_ours_is_live():
    child = _child()
    try:
        assert pid_is_live(child.pid) is True
    finally:
        child.terminate()
        child.wait(timeout=10)


def test_a_reaped_process_is_not_live():
    child = _child()
    child.terminate()
    child.wait(timeout=10)
    # the child is reaped, so its pid is gone; allow for the pid being reused by
    # something else in the meantime by asserting only the reaped case
    assert pid_is_live(child.pid) is False


def test_permission_denied_means_alive_not_dead(monkeypatch):
    """The whole point: EPERM is evidence of existence, not of death."""

    def denied(_pid, _signal):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(liveness.os, "kill", denied)
    assert pid_is_live(1) is True
    assert pid_is_live(4242) is True


def test_no_such_process_is_not_live(monkeypatch):
    def missing(_pid, _signal):
        raise ProcessLookupError(3, "No such process")

    monkeypatch.setattr(liveness.os, "kill", missing)
    assert pid_is_live(4242) is False


def test_an_unreadable_answer_fails_closed(monkeypatch):
    """A bad answer must not be read as "the runner is gone"."""

    def unreadable(_pid, _signal):
        raise OSError(22, "Invalid argument")

    monkeypatch.setattr(liveness.os, "kill", unreadable)
    assert pid_is_live(4242) is True


def test_no_recorded_runner_is_not_live():
    """The one answer that really is "no runner".

    ``None`` is an absent recording, not a corrupt one, and every caller guards
    with ``runner_pid is not None`` before asking.
    """

    assert pid_is_live(None) is False


@pytest.mark.parametrize("pid", [0, -1, "not-a-pid", "", 10**30])
def test_a_corrupt_pid_fails_closed(pid):
    """A value that cannot be probed counts as alive, not as dead.

    Deciding a runner is gone is what permits a second writer, so it needs
    positive evidence - ESRCH and nothing else.  A corrupt recording is
    unreadable, and unreadable must not be read as "gone".  ``10**30`` is the case
    that would otherwise escape entirely: ``os.kill`` raises ``OverflowError`` for
    a pid wider than the platform's ``pid_t`` rather than ``OSError``, so an
    uncaught one aborts runner startup.

    0 and negative values are in the same class and are never probed:
    ``os.kill(-1, 0)`` addresses every process the caller may signal.
    """

    assert pid_is_live(pid) is True


def test_an_oversized_pid_does_not_escape(monkeypatch):
    """Pinned directly, because it is an exception rather than a return value."""

    def overflow(_pid, _signal):
        raise OverflowError("Python int too large to convert to C long")

    monkeypatch.setattr(liveness.os, "kill", overflow)
    assert pid_is_live(2**64) is True


# ------------------------------------------------- the policy reaches the engine
def test_a_foreign_live_runner_blocks_a_second_writer(tmp_path, monkeypatch):
    """An engine-level assertion, not just a unit one.

    With EPERM read as death, this project would be reported as having an
    interrupted runner and a second writer would be allowed in.  Now the engine
    refuses with ``RunnerBusy``.
    """

    from factory_core.domain import RunnerBusy, WorkflowStatus
    from factory_core.storage import SQLiteStateStore

    root = tmp_path / "project"
    root.mkdir()
    store = SQLiteStateStore(root)
    state = store.initialize(project_id="liveness", project_type="modeling")
    store.transition(
        expected_revision=state.revision,
        event_type="RUNNER_ATTACHED_FOR_TEST",
        changes={"runner_pid": 1, "runner_lease_id": "foreign-lease"},
    )

    # PID 1 is live but not ours: os.kill(1, 0) raises PermissionError for a
    # non-root caller, which is exactly the case under test.
    engine = engine_module.FactoryEngine(root, store=SQLiteStateStore(root))
    with pytest.raises(RunnerBusy):
        engine.run(max_steps=1)

    # and the record is untouched by the refusal
    after = SQLiteStateStore(root).load()
    assert after.runner_pid == 1
    assert after.status is WorkflowStatus.READY


def test_both_entry_points_share_one_implementation():
    """The two copies had drifted; there is one now.

    ``engine`` used to swallow every OSError and ``service`` only two specific
    ones, so the same pid could be judged differently depending on which path
    asked.  Both now delegate, and neither contains a probe of its own.
    """

    assert engine_module.FactoryEngine._pid_is_live(1) is True
    assert service_module.FactoryService._pid_is_live(1) is True
    assert engine_module.FactoryEngine._pid_is_live(None) is False
    assert service_module.FactoryService._pid_is_live(None) is False

    engine_source = Path(engine_module.__file__).read_text(encoding="utf-8")
    service_source = Path(service_module.__file__).read_text(encoding="utf-8")
    assert "os.kill(pid, 0)" not in engine_source
    assert "os.kill(pid, 0)" not in service_source
