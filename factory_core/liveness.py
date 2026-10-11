"""Whether a recorded runner process is still there.

``runner_pid`` is persisted so a second writer cannot start on a project another
runner is already advancing.  Deciding that the recorded process is *gone* is
therefore the dangerous answer: it is what permits a second writer.  The check
must fail closed.

``os.kill(pid, 0)`` is the portable probe, and its error codes do not all mean
the same thing:

* ``ESRCH`` - no such process.  The runner really is gone.
* ``EPERM`` - the process exists and belongs to somebody else.  This is *not*
  evidence of death, and reading it as death is the specific bug this module
  exists to prevent: on a project directory shared between users, a live runner
  owned by another user would look dead, the engine would emit
  ``RUNNER_INTERRUPTED``, and a second runner could take over a project that is
  still being advanced.
* success - the process exists and is ours.

Anything else is unreadable, and an unreadable answer is treated as "still
there" for the same reason: the cost of a false "live" is a refused start, and
the cost of a false "dead" is two writers on one project.

Two copies of this logic had drifted apart before - ``engine.py`` swallowed every
``OSError`` while ``service.py`` swallowed only ``PermissionError`` and
``ProcessLookupError``, and both read ``EPERM`` as death.  There is one
implementation now, and it is here so a third copy cannot appear.
"""
from __future__ import annotations

import os

__all__ = ["pid_is_live"]


def pid_is_live(pid: int | None) -> bool:
    """Whether ``pid`` names a process that exists.

    Returns ``True`` for a live process the caller may not signal, because the
    question is existence, not permission.
    """

    if pid is None:
        return False
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        # Not a process id.  A persisted 0 or negative value is corrupt rather
        # than a runner, and probing it would be meaningless - os.kill(-1, 0)
        # addresses every process the caller may signal.
        return False

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        # ESRCH: no such process.
        return False
    except PermissionError:
        # EPERM: it exists; we simply may not signal it.
        return True
    except OSError:
        # Unreadable.  Fail closed: refuse to call a runner dead on a bad answer.
        return True
    return True
