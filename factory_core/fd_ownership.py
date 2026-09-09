"""Auditable file-descriptor ownership and failure-preserving cleanup.

The shadow stores create SQLite files with ``O_EXCL`` and then move the
creator descriptor through several roles.  A raw integer plus ``-1`` sentinels
is not an ownership protocol: an asynchronous ``BaseException`` can land
between assignment and sentinel update, leaving two apparent owners or none.

``OwnedDescriptor`` keeps ownership in one shared state object.  Transfer is a
single token change.  Before that change only the old token may close the
descriptor; afterwards only the new token may close it.  Closing retains the
descriptor until the unique kernel call is attempted, then permanently
relinquishes the integer without probing a possibly reused descriptor number.

Cleanup failures must not replace a primary exception.  They are logged,
attached to the primary exception, and added as exception notes.  Close and
unlink each have explicit before/after fault seams around one direct libc call;
there is no unsafe retry that could affect a reused descriptor or replacement
path.  An injected diagnostic is still reported after the owned resource has
been cleaned.
"""

from __future__ import annotations

import ctypes
from dataclasses import dataclass
import logging
import os
from typing import Callable, Iterable


LOGGER = logging.getLogger(__name__)
_LIBC = ctypes.CDLL(None, use_errno=True)
_LIBC_CLOSE = _LIBC.close
_LIBC_CLOSE.argtypes = (ctypes.c_int,)
_LIBC_CLOSE.restype = ctypes.c_int
_LIBC_UNLINKAT = _LIBC.unlinkat
_LIBC_UNLINKAT.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int)
_LIBC_UNLINKAT.restype = ctypes.c_int


class DescriptorOwnershipError(RuntimeError):
    """The caller used a descriptor with a stale or invalid owner token."""


@dataclass(frozen=True)
class CleanupFailure:
    """One cleanup operation that failed while another exception was active."""

    label: str
    error: BaseException


def _add_cleanup_failure(
    primary: BaseException,
    *,
    label: str,
    error: BaseException,
) -> None:
    """Retain a cleanup failure without replacing ``primary``."""

    existing = getattr(primary, "__factory_cleanup_failures__", ())
    try:
        setattr(
            primary,
            "__factory_cleanup_failures__",
            (*existing, CleanupFailure(label=label, error=error)),
        )
    except BaseException:
        # Some third-party exception implementations disallow attributes.  The
        # note and log remain independent diagnostic channels.
        pass
    try:
        primary.add_note(
            f"cleanup failure [{label}]: {type(error).__name__}: {error}"
        )
    except BaseException:
        pass
    try:
        LOGGER.error(
            "cleanup failure while preserving %s: %s",
            type(primary).__name__,
            label,
            exc_info=(type(error), error, error.__traceback__),
        )
    except BaseException:
        # Logging is diagnostic only.  A hostile/broken handler must never
        # replace the primary exception whose identity we are preserving.
        pass


def run_cleanup(
    callbacks: Iterable[tuple[str, Callable[[], object]]],
    *,
    primary: BaseException | None = None,
) -> None:
    """Run every cleanup callback and preserve the primary exception.

    When ``primary`` is supplied, every cleanup error is attached and logged,
    then this function returns so the caller can use a bare ``raise``.  Without
    a primary exception, one cleanup error is re-raised with its traceback and
    multiple failures are reported as a ``BaseExceptionGroup``.
    """

    failures: list[CleanupFailure] = []
    for label, callback in callbacks:
        try:
            callback()
        except BaseException as error:
            recovery = getattr(callback, "recover_after_failure", None)
            if recovery is not None:
                try:
                    recovery(error)
                except BaseException as recovery_error:
                    _add_cleanup_failure(
                        error,
                        label=f"retry incomplete cleanup [{label}]",
                        error=recovery_error,
                    )
            failure = CleanupFailure(label=label, error=error)
            failures.append(failure)
            if primary is not None:
                _add_cleanup_failure(primary, label=label, error=error)
    if primary is not None or not failures:
        return
    if len(failures) == 1:
        error = failures[0].error
        raise error.with_traceback(error.__traceback__)
    raise BaseExceptionGroup(
        "multiple resource cleanup failures",
        [failure.error for failure in failures],
    )


def _descriptor_close_failure_point(stage: str, descriptor: int) -> None:
    """Deterministic test seam surrounding the one kernel close operation."""


@dataclass
class _CloseAttempt:
    kernel_call_attempted: bool = False


def _attempt_raw_close(descriptor: int, state: _CloseAttempt) -> None:
    # One source line is intentional: there is no nested Python frame between
    # publication and the pre-bound libc syscall.
    state.kernel_call_attempted = True; result = _LIBC_CLOSE(descriptor)
    if result != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), descriptor)


def close_descriptor_once(
    descriptor: int,
    *,
    _state: _CloseAttempt | None = None,
) -> None:
    """Perform exactly one kernel close while preserving injected failures.

    Retrying ``close(2)`` after an ambiguous error is unsafe because another
    thread may already have reused the integer descriptor.  The injectable
    failure seam therefore runs before or after one direct libc call.  A
    pre-call injected error still triggers that one call and is then re-raised;
    a post-call error observes an already-closed descriptor.  The syscall
    itself is never blindly retried.
    """

    state = _state if _state is not None else _CloseAttempt()
    try:
        _descriptor_close_failure_point("before", descriptor)
    except BaseException as primary:
        try:
            _attempt_raw_close(descriptor, state)
        except BaseException as close_error:
            _add_cleanup_failure(
                primary,
                label=f"kernel close for fd {descriptor}",
                error=close_error,
            )
        raise
    _attempt_raw_close(descriptor, state)
    _descriptor_close_failure_point("after", descriptor)


class OwnedDescriptor:
    """One descriptor with an explicit, atomically transferred owner token."""

    __slots__ = (
        "_adopted_descriptor",
        "_descriptor",
        "_managed",
        "_owner",
        "label",
    )

    def __init__(
        self,
        descriptor: int,
        *,
        owner: str,
        label: str,
        _defer_finalizer: bool = False,
    ) -> None:
        self._managed = False
        if not isinstance(descriptor, int) or isinstance(descriptor, bool) or descriptor < 0:
            raise DescriptorOwnershipError("descriptor must be a nonnegative integer")
        if not isinstance(owner, str) or not owner:
            raise DescriptorOwnershipError("owner token must be non-empty")
        if not isinstance(label, str) or not label:
            raise DescriptorOwnershipError("descriptor label must be non-empty")
        self._adopted_descriptor = descriptor
        self._descriptor: int | None = descriptor
        self._owner = owner
        self.label = label
        self._managed = not _defer_finalizer

    @classmethod
    def from_opener(
        cls,
        opener: Callable[[], int],
        *,
        owner: str,
        label: str,
    ) -> "OwnedDescriptor":
        """Acquire and publish one descriptor under an exception-safe lease.

        The raw descriptor remains the fallback owner until construction of the
        lease has completed.  If construction or an asynchronous trace failure
        interrupts publication, this frame closes whichever representation was
        established.  Callers therefore never receive a raw integer that must
        subsequently be wrapped in a separate ownership step.
        """

        descriptor = -1
        lease: OwnedDescriptor | None = None
        try:
            descriptor = opener()
            lease = cls(
                descriptor,
                owner=owner,
                label=label,
                _defer_finalizer=True,
            )
            lease._managed = True
            return lease
        except BaseException as primary:
            callbacks: list[tuple[str, Callable[[], object]]] = []
            if lease is not None:
                callbacks.append((f"close {label}", lambda: lease.close(owner)))
            elif descriptor >= 0:
                callbacks.append(
                    (f"close raw {label}", lambda: close_descriptor_once(descriptor))
                )
            run_cleanup(callbacks, primary=primary)
            raise

    def __del__(self) -> None:
        """Last-resort close for an interrupted return/store boundary.

        Normal paths close explicitly so failures can be retained with their
        primary exception.  This guard covers the narrow CPython case where a
        fully constructed lease is discarded before a caller can publish it.
        """

        descriptor = getattr(self, "_descriptor", None)
        owner = getattr(self, "_owner", None)
        if (
            descriptor is None
            or not isinstance(owner, str)
            or not getattr(self, "_managed", False)
        ):
            return
        try:
            self.close(owner)
        except BaseException:
            try:
                LOGGER.exception(
                    "last-resort close failed for an unpublished descriptor"
                )
            except BaseException:
                pass

    @property
    def owner(self) -> str:
        return self._owner

    @property
    def closed(self) -> bool:
        return self._descriptor is None

    def adopted_descriptor(self, descriptor: int) -> bool:
        """Whether this lease ever accepted the given raw descriptor."""

        return self._adopted_descriptor == descriptor

    def fileno(self, owner: str) -> int:
        if self._descriptor is None:
            raise DescriptorOwnershipError(f"{self.label} descriptor is closed")
        if self._owner != owner:
            raise DescriptorOwnershipError(
                f"{self.label} descriptor belongs to {self._owner}, not {owner}"
            )
        return self._descriptor

    def transfer(self, *, owner: str, new_owner: str) -> None:
        """Transfer responsibility with one owner-token state change."""

        self.fileno(owner)
        if not isinstance(new_owner, str) or not new_owner:
            raise DescriptorOwnershipError("new owner token must be non-empty")
        self._owner = new_owner

    def close(self, owner: str) -> bool:
        """Close only for the current owner; stale owners are explicit no-ops."""

        try:
            if self._descriptor is None or self._owner != owner:
                return False
            descriptor = self._descriptor
            close_state = _CloseAttempt()
            close_descriptor_once(descriptor, _state=close_state)
            # Keep the descriptor published until the kernel close has
            # completed.  An asynchronous failure before/during the call can
            # therefore be retried by the same owner; a failure after the call
            # is reconciled below through the explicit attempt state.
            self._descriptor = None
            self._owner = "closed"
        except BaseException as primary:
            descriptor = locals().get("descriptor")
            close_state = locals().get("close_state")
            if (
                descriptor is None
                and self._descriptor is not None
                and self._owner == owner
            ):
                descriptor = self._descriptor
            if close_state is None:
                close_state = _CloseAttempt()
            if descriptor is not None and not close_state.kernel_call_attempted:
                try:
                    close_descriptor_once(descriptor, _state=close_state)
                except BaseException as recovery_error:
                    _add_cleanup_failure(
                        primary,
                        label=f"recover interrupted close of {self.label}",
                        error=recovery_error,
                    )
            if descriptor is not None and close_state.kernel_call_attempted:
                # Once close(2) has been attempted on Linux, this lease must
                # relinquish the integer.  Looking it up with fstat is unsafe:
                # another thread may already have reused it for a foreign file.
                self._descriptor = None
                self._owner = "closed"
            raise
        return True

    def cleanup(self, owner: str) -> "_OwnedDescriptorCleanup":
        """Return a cleanup action that recovers an interrupted call entry.

        A trace-raised ``BaseException`` can occur on the first executable line
        of :meth:`close`, before that method's own ``try`` is active.  The
        cleanup runner recognizes this action and retries only while this exact
        lease still belongs to ``owner``.  It never retries an attempted kernel
        close or acts on a reused descriptor number.
        """

        return _OwnedDescriptorCleanup(self, owner)

    def duplicate(self, *, owner: str, new_owner: str, label: str) -> "OwnedDescriptor":
        """Duplicate without changing the source owner's responsibility."""

        descriptor = self.fileno(owner)
        duplicate = OwnedDescriptor.from_opener(
            lambda: os.dup(descriptor), owner=new_owner, label=label
        )
        try:
            os.set_inheritable(duplicate.fileno(new_owner), False)
        except BaseException as primary:
            run_cleanup(
                [(f"close {label} after inheritable failure", lambda: duplicate.close(new_owner))],
                primary=primary,
            )
            raise
        return duplicate


class _OwnedDescriptorCleanup:
    """Idempotent cleanup callback with an explicit incomplete-state probe."""

    __slots__ = ("_lease", "_owner")

    def __init__(self, lease: OwnedDescriptor, owner: str) -> None:
        self._lease = lease
        self._owner = owner

    def __call__(self) -> bool:
        return self._lease.close(self._owner)

    def recover_after_failure(self, primary: BaseException) -> None:
        if not self._lease.closed and self._lease.owner == self._owner:
            self._lease.close(self._owner)


class RetryableCleanup:
    """Retry one idempotent cleanup after an interrupted Python call entry.

    This wrapper is deliberately opt-in.  It is appropriate for SQLite close
    and other callbacks whose implementation is explicitly retry-safe.  It is
    never used for namespace mutation such as unlink/rename.
    """

    __slots__ = ("_callback",)

    def __init__(self, callback: Callable[[], object]) -> None:
        self._callback = callback

    def __call__(self) -> object:
        return self._callback()

    def recover_after_failure(self, primary: BaseException) -> None:
        self._callback()


def close_raw_descriptor_if_unowned(
    descriptor: int,
    *leases: OwnedDescriptor | None,
) -> bool:
    """Close a raw acquisition only if no published lease ever adopted it.

    A retained raw integer is evidence of the pre-transfer owner, not a second
    owner.  Keeping it until unwind makes the raw-to-lease publication window
    auditable; the permanent adopted-descriptor identity prevents a later
    cleanup path from double-closing a descriptor number that the lease has
    already closed.
    """

    if descriptor < 0:
        return False
    if any(
        lease is not None and lease.adopted_descriptor(descriptor)
        for lease in leases
    ):
        return False
    close_descriptor_once(descriptor)
    return True


def _raw_unlinkat(parent_fd: int, name: str) -> None:
    result = _LIBC_UNLINKAT(parent_fd, os.fsencode(name), 0)
    if result != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), name)


def _unlink_failure_point(stage: str, parent_fd: int, name: str) -> None:
    """Deterministic test seam surrounding the one kernel unlink operation."""


def resilient_unlink_at(parent_fd: int, name: str) -> None:
    """Perform one anchored unlink without an unsafe name-based retry.

    A retry after an ambiguous ``unlink`` error could delete a foreign entry
    recreated under the same name.  Like descriptor close, fault injection is
    separated from the unique libc operation.  A pre-call failure performs no
    namespace operation so the owning store can revalidate the exact inode; a
    post-call failure sees the original name absent and is never retried here.
    """

    try:
        _unlink_failure_point("before", parent_fd, name)
    except BaseException:
        # No namespace operation has happened, so do not act on the name after
        # an injected pause.  The owning store revalidates the quarantine's
        # exact inode before invoking this helper again.  Blindly continuing
        # here could unlink a foreign same-name replacement.
        raise
    _raw_unlinkat(parent_fd, name)
    _unlink_failure_point("after", parent_fd, name)
