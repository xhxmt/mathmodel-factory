"""Bounded-advance execution contract (S6).

Why this exists
---------------
Project A's ``work/`` directory holds 128 hand-written driver scripts, 24 of which
call ``engine.run(max_steps=1)``.  Each one re-implements, slightly differently,
the invariants the engine should have guaranteed:

    assert state.revision == 301 and state.active_step == 11 ...
    protected = json.loads((W / 'protected_files.json').read_text())
    def verify(): ...compare sha256 per file...
    after = FactoryService(ROOT).engine(P).run(max_steps=1)
    verify()
    (W / 'progress.json').write_text(...)

They are not one wrapper that can be deleted.  They are a layer that grew because
there was no supported way to say "advance this project, but only within this
scope, from this exact state, without touching these artifacts" - and because
there was no way to detect that a run had made no progress.  Their hard-coded
revisions rot silently, and none of them is in version control.

This module supplies the missing contract.  It does **not** add a second runner:
``FactoryEngine.run`` already advances continuously, and the contract only
constrains and records one bounded invocation of it.

What the contract pins
----------------------
    expected_revision     compare-and-swap against the live revision
    expected_cursor       stage / subtask / source step, so a matching revision
                          with a different position is still refused
    allowed_source_steps  the authorisation boundary (already enforced by run)
    max_subtasks          an explicit bound for one invocation
    protected_manifest    project-relative path -> sha256, verified at entry *and*
                          immediately before a successful checkpoint commits
    run_policy            how far this invocation is allowed to go
    actor                 who authorised it

The two protected verifications answer different questions.  Entry answers "was
the state already what the caller expected?"  Pre-commit answers "did this run
break it?"  Only checking the latter cannot tell an already-dirty project from one
this run dirtied.

Identity
--------
``bounded_run_contract_sha256`` is a canonical hash over the contract's
authorising content, so an event can answer afterwards *which* authorisation a
run executed under.  The caller's own scaffolding - entry/progress journals - is
explicitly outside it.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping

BOUNDED_RUN_SCHEMA = "factory-bounded-run-contract-v1"


class BoundedRunError(ValueError):
    """The contract itself is unusable (bad scope, unsafe manifest, or stale CAS)."""


class ProtectedManifestViolation(RuntimeError):
    """A protected artifact does not match the contract's expectation."""


class RunPolicy:
    ADVANCE_UNTIL_BLOCKED = "advance_until_blocked"
    BOUNDED_SUBTASKS = "bounded_subtasks"


#: Values that mean "this invocation made no forward progress".  Reported rather
#: than raised, because the caller decides whether that needs a human.
UNCHANGED_BOUNDARY = "UNCHANGED_BOUNDARY"
NEEDS_INSPECTION = "NEEDS_INSPECTION"


@dataclass(frozen=True)
class BoundedRunContract:
    """One authorised, bounded invocation of the existing runner."""

    expected_revision: int
    expected_cursor: tuple[int | None, str | None, int | None] | None = None
    allowed_source_steps: frozenset[int] | None = None
    max_subtasks: int | None = None
    protected_manifest: Mapping[str, str] = field(default_factory=dict)
    run_policy: str = RunPolicy.ADVANCE_UNTIL_BLOCKED
    actor: str = "operator"
    #: The previous invocation's boundary fingerprint, when the caller has one.
    #: Passing it in avoids persisting a ``seen`` journal while still letting the
    #: caller detect a repeated boundary.
    previous_boundary_fingerprint: str | None = None
    #: Per-source-step ceilings for this invocation only.  They may only
    #: *tighten* what the registry already allows - the effective value is the
    #: stricter of the two - so a contract can never grant more attempts or more
    #: reopens than the Step catalogue does.
    #:
    #: These exist because a hand-written driver was reaching into the registry
    #: to cap them (``dataclasses.replace(definition, max_attempts=attempt + 1,
    #: max_reopens=0)``).  That is an authorisation, so it belongs in the
    #: authorisation rather than in a private copy of the engine's registry.
    max_attempts_per_step: Mapping[int, int] | None = None
    max_reopens_per_step: Mapping[int, int] | None = None
    #: Path -> ``(size, mtime_ns)`` for artifacts that are too large to hash on
    #: every entry and pre-commit check.  Checked exactly like
    #: ``protected_manifest``, but *weaker*: equal size and mtime do not prove
    #: equal content, and a writer that preserves both can defeat it.  It exists
    #: because a caller protecting a very large artifact faces a real tradeoff
    #: between hashing cost and identity strength, and that choice belongs in the
    #: authorisation rather than in a private check the engine cannot see.
    protected_identity: Mapping[str, tuple[int, int]] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.expected_revision, int) or self.expected_revision < 0:
            raise BoundedRunError("expected_revision must be a non-negative integer")
        if self.max_subtasks is not None and (
            not isinstance(self.max_subtasks, int) or self.max_subtasks < 1
        ):
            raise BoundedRunError("max_subtasks must be a positive integer when given")
        if self.run_policy not in {
            RunPolicy.ADVANCE_UNTIL_BLOCKED,
            RunPolicy.BOUNDED_SUBTASKS,
        }:
            raise BoundedRunError(f"unknown run_policy: {self.run_policy!r}")
        if self.run_policy == RunPolicy.BOUNDED_SUBTASKS and self.max_subtasks is None:
            raise BoundedRunError(
                "run_policy=bounded_subtasks requires an explicit max_subtasks bound"
            )
        if self.allowed_source_steps is not None:
            object.__setattr__(
                self, "allowed_source_steps", frozenset(self.allowed_source_steps)
            )
            if not self.allowed_source_steps:
                raise BoundedRunError("allowed_source_steps cannot be empty")
        object.__setattr__(
            self,
            "max_attempts_per_step",
            _validate_step_ceilings(
                "max_attempts_per_step", self.max_attempts_per_step, minimum=1
            ),
        )
        object.__setattr__(
            self,
            "max_reopens_per_step",
            _validate_step_ceilings(
                "max_reopens_per_step", self.max_reopens_per_step, minimum=0
            ),
        )
        object.__setattr__(
            self,
            "protected_manifest",
            {str(k): str(v) for k, v in dict(self.protected_manifest).items()},
        )
        identity: dict[str, tuple[int, int]] = {}
        for relative, expectation in dict(self.protected_identity or {}).items():
            _validate_project_relative(str(relative))
            try:
                size, mtime_ns = expectation  # type: ignore[misc]
            except (TypeError, ValueError) as exc:
                raise BoundedRunError(
                    f"protected_identity[{relative!r}] must be (size, mtime_ns)"
                ) from exc
            for label, value in (("size", size), ("mtime_ns", mtime_ns)):
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise BoundedRunError(
                        f"protected_identity[{relative!r}] {label} must be a "
                        f"non-negative integer, not {value!r}"
                    )
            if str(relative) in self.protected_manifest:
                raise BoundedRunError(
                    f"{relative!r} appears in both protected_manifest and "
                    "protected_identity; one path gets one kind of check"
                )
            identity[str(relative)] = (int(size), int(mtime_ns))
        object.__setattr__(self, "protected_identity", identity or None)
        for relative in self.protected_manifest:
            _validate_project_relative(relative)
        for digest in self.protected_manifest.values():
            if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise BoundedRunError(
                    f"protected manifest digest must be a lowercase sha256: {digest!r}"
                )

    # -- identity ---------------------------------------------------------- #
    def canonical_payload(self) -> dict[str, Any]:
        """The authorising content, canonically ordered.

        Deliberately excludes ``previous_boundary_fingerprint``: that is the
        caller's bookkeeping, not part of what was authorised.
        """

        return {
            "schema": BOUNDED_RUN_SCHEMA,
            "expected_revision": int(self.expected_revision),
            "expected_cursor": list(self.expected_cursor) if self.expected_cursor else None,
            "allowed_source_steps": (
                sorted(int(step) for step in self.allowed_source_steps)
                if self.allowed_source_steps is not None
                else None
            ),
            "max_subtasks": self.max_subtasks,
            "protected_manifest": {
                key: self.protected_manifest[key]
                for key in sorted(self.protected_manifest)
            },
            "max_attempts_per_step": _ceilings_payload(self.max_attempts_per_step),
            "max_reopens_per_step": _ceilings_payload(self.max_reopens_per_step),
            "protected_identity": (
                {
                    path: {"size": self.protected_identity[path][0],
                           "mtime_ns": self.protected_identity[path][1]}
                    for path in sorted(self.protected_identity)
                }
                if self.protected_identity
                else None
            ),
            "run_policy": self.run_policy,
            "actor": self.actor,
        }

    @property
    def contract_sha256(self) -> str:
        return hashlib.sha256(
            json.dumps(
                self.canonical_payload(), ensure_ascii=True, sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    @property
    def run_id(self) -> str:
        """Short, stable handle for one authorisation."""

        return self.contract_sha256[:32]

    def event_payload(self) -> dict[str, Any]:
        """What gets bound into RUN_STARTED and the terminal/boundary events."""

        return {
            "bounded_run_schema": BOUNDED_RUN_SCHEMA,
            "bounded_run_id": self.run_id,
            "bounded_run_contract_sha256": self.contract_sha256,
            "run_policy": self.run_policy,
            "actor": self.actor,
            "expected_revision": int(self.expected_revision),
            "max_subtasks": self.max_subtasks,
            "allowed_source_steps": (
                sorted(int(step) for step in self.allowed_source_steps)
                if self.allowed_source_steps is not None
                else None
            ),
            "max_attempts_per_step": _ceilings_payload(self.max_attempts_per_step),
            "max_reopens_per_step": _ceilings_payload(self.max_reopens_per_step),
            "protected_identity": (
                {
                    path: {"size": self.protected_identity[path][0],
                           "mtime_ns": self.protected_identity[path][1]}
                    for path in sorted(self.protected_identity)
                }
                if self.protected_identity
                else None
            ),
            "protected_manifest_sha256": hashlib.sha256(
                json.dumps(
                    self.canonical_payload()["protected_manifest"],
                    ensure_ascii=True, sort_keys=True, separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(),
        }


def _validate_step_ceilings(
    name: str, ceilings: Mapping[int, int] | None, *, minimum: int
) -> dict[int, int] | None:
    """Normalise a per-step ceiling mapping, or refuse it.

    An empty mapping is treated as absent: a caller that scoped nothing has not
    authorised anything extra, and the contract's identity should not depend on
    the difference between ``None`` and ``{}``.
    """

    if not ceilings:
        return None
    validated: dict[int, int] = {}
    for step, value in dict(ceilings).items():
        if isinstance(step, bool) or not isinstance(step, int) or step < 0:
            raise BoundedRunError(
                f"{name} keys must be non-negative source step ids, not {step!r}"
            )
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise BoundedRunError(
                f"{name}[{step}] must be an integer >= {minimum}, not {value!r}"
            )
        validated[int(step)] = int(value)
    return validated


def _ceilings_payload(ceilings: Mapping[int, int] | None) -> dict[str, int] | None:
    """Canonical, JSON-ready form.  Sorted so the contract hash is stable."""

    if not ceilings:
        return None
    return {str(step): ceilings[step] for step in sorted(ceilings)}


class ScopedRegistry:
    """A registry view that tightens Step ceilings for one bounded invocation.

    Installed by the engine around the advance loop when, and only when, the
    contract carries a ceiling.  Every resolution path goes through it, which is
    the point: the engine reads ``max_attempts`` and ``max_reopens`` from a
    ``StepDefinition`` at several sites, and patching each of them would leave
    the next one added unprotected.

    Both ceilings are strictest-wins, so the wrapper can only ever narrow what
    the registry already granted - a contract cannot widen an authorisation.
    """

    def __init__(self, inner, contract: "BoundedRunContract") -> None:
        self.inner = inner
        self.contract = contract

    def _scoped(self, definition):
        if definition is None:
            return None
        attempts = (self.contract.max_attempts_per_step or {}).get(definition.id)
        reopens = (self.contract.max_reopens_per_step or {}).get(definition.id)
        if attempts is None and reopens is None:
            return definition
        return replace(
            definition,
            max_attempts=(
                definition.max_attempts if attempts is None
                else min(definition.max_attempts, attempts)
            ),
            max_reopens=(
                definition.max_reopens if reopens is None
                else min(definition.max_reopens, reopens)
            ),
        )

    def get(self, step_id: int):
        return self._scoped(self.inner.get(step_id))

    def next_after(self, completed_step: int):
        return self._scoped(self.inner.next_after(completed_step))

    def stage_subtask(self, key: str):
        return self._scoped(self.inner.stage_subtask(key))

    def __iter__(self):
        return (self._scoped(definition) for definition in self.inner)

    def __getattr__(self, name: str):
        # only reached when ``inner``/``contract`` are not found, so anything the
        # registry grows later is delegated rather than silently lost
        inner = self.__dict__.get("inner")
        if inner is None:
            raise AttributeError(name)
        return getattr(inner, name)


def _validate_project_relative(relative: str) -> None:
    """Refuse anything that could escape the project.

    A protected manifest is an authorisation to *not* touch content, so a path
    that escapes the project would either protect nothing or protect something
    the caller never named.  Absolute paths, ``..`` traversal and the empty path
    are all refused here; symlink traversal is refused at verification time,
    where the filesystem is available.
    """

    if not relative or not isinstance(relative, str):
        raise BoundedRunError("protected manifest paths must be non-empty strings")
    if relative.startswith("/") or relative.startswith("\\"):
        raise BoundedRunError(f"protected manifest path must be project-relative: {relative!r}")
    if len(relative) > 1 and relative[1] == ":":
        raise BoundedRunError(f"protected manifest path must be project-relative: {relative!r}")
    normalized = relative.replace("\\", "/")
    # Inspect the raw components: PurePosixPath collapses a leading "./", so the
    # check has to happen before that normalization or "./x" would slip through.
    raw_parts = [part for part in normalized.split("/")]
    if any(part == "." for part in raw_parts):
        raise BoundedRunError(f"protected manifest path may not contain '.': {relative!r}")
    pure = PurePosixPath(normalized)
    if pure.is_absolute():
        raise BoundedRunError(f"protected manifest path must be project-relative: {relative!r}")
    if any(part == ".." for part in pure.parts):
        raise BoundedRunError(f"protected manifest path may not traverse upwards: {relative!r}")


@dataclass(frozen=True)
class ProtectedVerification:
    ok: bool
    checked: int
    missing: tuple[str, ...] = ()
    changed: tuple[str, ...] = ()
    unsafe: tuple[str, ...] = ()
    #: Paths whose (size, mtime_ns) no longer matches.  Kept apart from
    #: ``changed`` so a caller can tell a content change from an identity change.
    identity_changed: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "checked": self.checked,
            "missing": list(self.missing),
            "changed": list(self.changed),
            "unsafe": list(self.unsafe),
            "identity_changed": list(self.identity_changed),
        }

    def describe(self) -> str:
        parts = []
        if self.unsafe:
            parts.append("unsafe: " + ", ".join(self.unsafe[:4]))
        if self.missing:
            parts.append("missing: " + ", ".join(self.missing[:4]))
        if self.changed:
            parts.append("changed: " + ", ".join(self.changed[:4]))
        if self.identity_changed:
            parts.append("identity changed: " + ", ".join(self.identity_changed[:4]))
        return "; ".join(parts) or "ok"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_protected_manifest(
    project_dir,
    manifest: Mapping[str, str],
    identity: Mapping[str, tuple[int, int]] | None = None,
) -> ProtectedVerification:
    """Compare the project's current bytes against the contract's expectations.

    A path is reported ``unsafe`` when it is not a regular file inside the
    project - because it is a symlink, because a parent component is a symlink
    resolving elsewhere, or because it simply is not there in a usable form.
    Treating an unreadable escaped path as "unchanged" would be the worst
    possible answer.
    """

    project = Path(project_dir).resolve()
    missing: list[str] = []
    changed: list[str] = []
    unsafe: list[str] = []
    identity_changed: list[str] = []
    identity = dict(identity or {})
    for relative in sorted(identity):
        candidate = project / relative
        try:
            if candidate.is_symlink():
                unsafe.append(relative)
                continue
            resolved = candidate.resolve(strict=True)
        except OSError:
            missing.append(relative)
            continue
        if not resolved.is_file():
            missing.append(relative)
            continue
        try:
            resolved.relative_to(project)
        except ValueError:
            unsafe.append(relative)
            continue
        stat = resolved.stat()
        size, mtime_ns = identity[relative]
        if stat.st_size != size or stat.st_mtime_ns != mtime_ns:
            identity_changed.append(relative)
    for relative in sorted(manifest):
        candidate = project / relative
        try:
            if candidate.is_symlink():
                unsafe.append(relative)
                continue
            resolved = candidate.resolve(strict=True)
        except OSError:
            missing.append(relative)
            continue
        if not resolved.is_file():
            missing.append(relative)
            continue
        try:
            resolved.relative_to(project)
        except ValueError:
            unsafe.append(relative)
            continue
        if _sha256_file(resolved) != manifest[relative]:
            changed.append(relative)
    return ProtectedVerification(
        ok=not (missing or changed or unsafe or identity_changed),
        checked=len(manifest) + len(identity),
        missing=tuple(missing),
        changed=tuple(changed),
        unsafe=tuple(unsafe),
        identity_changed=tuple(identity_changed),
    )


def boundary_fingerprint(
    state,
    *,
    blocked_reason: str = "",
    dependency_fingerprint: str = "",
) -> str:
    """A fingerprint of the *stopping position*, for repeated-boundary detection.

    Includes a blocking dependency fingerprint rather than a bare identity: the
    same solver job with a new completion receipt is a different boundary, and a
    fingerprint that ignored that would report "unchanged" for a real change.
    """

    payload = {
        "schema": "factory-boundary-fingerprint-v1",
        "revision": int(getattr(state, "revision", 0) or 0),
        "status": str(getattr(getattr(state, "status", None), "value", getattr(state, "status", ""))),
        "stage": getattr(state, "active_stage", None),
        "subtask": getattr(state, "active_subtask", None),
        "source_step": getattr(state, "source_step_id", None),
        "last_completed_stage": getattr(state, "last_completed_stage", None),
        "last_completed_step": getattr(state, "last_completed_step", None),
        "blocked_reason": blocked_reason,
        "dependency_fingerprint": dependency_fingerprint,
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@dataclass(frozen=True)
class BoundedRunResult:
    """The structured outcome of one bounded invocation."""

    run_id: str
    contract_sha256: str
    run_policy: str
    actor: str
    start_revision: int
    end_revision: int
    status: str
    stop_reason: str
    completed_subtasks: int
    made_progress: bool
    boundary_fingerprint: str
    unchanged_boundary: bool
    entry_verification: ProtectedVerification
    final_verification: ProtectedVerification
    blocked_reason: str = ""
    contract_violation: str | None = None

    @property
    def needs_inspection(self) -> bool:
        return self.unchanged_boundary and not self.made_progress

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "factory-bounded-run-result-v1",
            "run_id": self.run_id,
            "contract_sha256": self.contract_sha256,
            "run_policy": self.run_policy,
            "actor": self.actor,
            "start_revision": self.start_revision,
            "end_revision": self.end_revision,
            "status": self.status,
            "stop_reason": self.stop_reason,
            "completed_subtasks": self.completed_subtasks,
            "made_progress": self.made_progress,
            "boundary_fingerprint": self.boundary_fingerprint,
            "unchanged_boundary": self.unchanged_boundary,
            "outcome": NEEDS_INSPECTION if self.needs_inspection else UNCHANGED_BOUNDARY if self.unchanged_boundary else "ADVANCED",
            "blocked_reason": self.blocked_reason,
            "contract_violation": self.contract_violation,
            "entry_verification": self.entry_verification.to_dict(),
            "final_verification": self.final_verification.to_dict(),
        }


def cursor_of(state) -> tuple[int | None, str | None, int | None]:
    """The position a contract's ``expected_cursor`` is compared against."""

    return (
        getattr(state, "active_stage", None),
        getattr(state, "active_subtask", None),
        getattr(state, "source_step_id", None),
    )


def check_cursor(
    state, expected_cursor: tuple[int | None, str | None, int | None] | None
) -> None:
    """Refuse a cursor mismatch even when the revision happens to match."""

    if expected_cursor is None:
        return
    actual = cursor_of(state)
    if tuple(expected_cursor) != tuple(actual):
        raise BoundedRunError(
            "cursor mismatch: contract expects "
            f"(stage, subtask, source_step)={tuple(expected_cursor)} but the project is at {actual}"
        )


def classify_stop_reason(state, *, previous_status: str, completed: int, bounded: int | None) -> str:
    """A short machine-readable reason for why the bounded run returned."""

    status = str(getattr(getattr(state, "status", None), "value", getattr(state, "status", "")))
    if status == "completed":
        return "PROJECT_COMPLETED"
    if status == "paused":
        return "BOUNDARY_OR_SCOPE"
    if status == "failed":
        return "FAILED"
    if status == "blocked":
        return "BLOCKED"
    if status in {"awaiting_selection", "awaiting_consultation"}:
        return "HUMAN_DECISION"
    if bounded is not None and completed >= bounded:
        return "MAX_SUBTASKS"
    if status == previous_status and completed == 0:
        # Nothing advanced and the position did not move: reporting
        # NO_FURTHER_WORK here would hide a repeated boundary.
        return "UNCHANGED"
    if status == "ready":
        return "NO_FURTHER_WORK"
    return "ADVANCED"


def reconcile_contract_violation(
    contract: BoundedRunContract, state, *, start_revision: int, completed: int
) -> str | None:
    """Post-conditions the contract can check once the run has returned."""

    if contract.max_subtasks is not None and completed > contract.max_subtasks:
        return (
            f"advanced {completed} subtasks, exceeding the contracted bound of "
            f"{contract.max_subtasks}"
        )
    if (
        contract.allowed_source_steps is not None
        and state.active_step is not None
        and state.active_step not in contract.allowed_source_steps
        and getattr(state, "status", None)
        and str(getattr(state.status, "value", state.status)) == "running"
    ):
        return (
            f"selected source step {state.active_step} outside the authorised "
            f"{sorted(contract.allowed_source_steps)}"
        )
    if state.revision < start_revision:
        return f"revision moved backwards from {start_revision} to {state.revision}"
    return None


__all__ = [
    "BOUNDED_RUN_SCHEMA",
    "BoundedRunContract",
    "BoundedRunError",
    "BoundedRunResult",
    "NEEDS_INSPECTION",
    "ProtectedManifestViolation",
    "ProtectedVerification",
    "RunPolicy",
    "UNCHANGED_BOUNDARY",
    "boundary_fingerprint",
    "check_cursor",
    "classify_stop_reason",
    "cursor_of",
    "reconcile_contract_violation",
    "verify_protected_manifest",
]