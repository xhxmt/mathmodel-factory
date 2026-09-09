from __future__ import annotations

import fcntl
import os
from pathlib import Path
import time

import pytest

from factory_core.phase78_deadline import Phase78DeadlineError, TotalDeadline
from tests.test_phase6_snapshot_grants import _evaluate, _issue, _store_with_snapshot


def test_current_proof_lock_wait_consumes_the_shared_total_deadline(
    tmp_path: Path,
) -> None:
    """The Phase78 caller's one budget also bounds Phase6 SQLite locking."""

    store, path, snapshot = _store_with_snapshot(tmp_path)
    grant = _issue(store, snapshot.snapshot_id).grant
    allowed = _evaluate(store, grant.grant_id)
    assert allowed.access_proof is not None

    lock_fd = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        started = time.monotonic()
        with pytest.raises(Phase78DeadlineError):
            store.verify_current_access_proof(
                allowed.access_proof,
                deadline=TotalDeadline(30),
            )
        assert time.monotonic() - started < 0.5
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)

    assert store.verify_current_access_proof(
        allowed.access_proof, deadline=TotalDeadline(1_000)
    ) == allowed.access_proof
