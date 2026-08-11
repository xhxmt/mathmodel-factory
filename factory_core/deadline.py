from __future__ import annotations

import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

from .contest import ContestDeadlineExceeded


_ACTIVE_DEADLINE: ContextVar[int | None] = ContextVar(
    "factory_active_deadline", default=None
)


@contextmanager
def deadline_scope(deadline_epoch: int | None) -> Iterator[None]:
    token = _ACTIVE_DEADLINE.set(
        None if deadline_epoch is None else int(deadline_epoch)
    )
    try:
        yield
    finally:
        _ACTIVE_DEADLINE.reset(token)


def ensure_deadline(*, now: int | None = None) -> None:
    deadline = _ACTIVE_DEADLINE.get()
    if deadline is not None and int(time.time() if now is None else now) >= deadline:
        raise ContestDeadlineExceeded("contest lifecycle deadline reached")


def cap_timeout(requested: int, *, now: int | None = None) -> int:
    deadline = _ACTIVE_DEADLINE.get()
    if deadline is None:
        return int(requested)
    remaining = deadline - int(time.time() if now is None else now)
    if remaining <= 0:
        raise ContestDeadlineExceeded("contest lifecycle deadline reached")
    return min(int(requested), remaining)
