"""Strict, default-off configuration for the Phase 7+8 shadow pipeline."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
from typing import Mapping

from .phase78_deadline import validate_deadline_ms


_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class Phase78ConfigurationError(RuntimeError):
    code = "PHASE78_CONFIGURATION_INVALID"


@dataclass(frozen=True, slots=True)
class Phase78Settings:
    enabled: bool = False
    authority_database: Path | None = None
    authority_source_fence_sha256: str | None = None
    phase6_database: Path | None = None
    phase7_database: Path | None = None
    phase8_database: Path | None = None
    work_database: Path | None = None
    work_spool: Path | None = None
    project_root: Path | None = None
    cas_root: Path | None = None
    scratch_root: Path | None = None
    deadline_ms: int = 30_000
    lease_seconds: int = 30

    def required_path(self, name: str) -> Path:
        if not self.enabled:
            raise Phase78ConfigurationError("Phase 7+8 shadow pipeline is disabled")
        value = getattr(self, name, None)
        if not isinstance(value, Path):
            raise Phase78ConfigurationError(f"missing Phase 7+8 path: {name}")
        return value


def _boolean(raw: object) -> bool:
    if raw is None or raw == "":
        return False
    if not isinstance(raw, str):
        raise Phase78ConfigurationError("PHASE78_ENABLED has an invalid value")
    normalized = raw.strip().lower()
    if normalized in _TRUE:
        return True
    if normalized in _FALSE:
        return False
    raise Phase78ConfigurationError(
        "PHASE78_ENABLED must be one of: 1, true, yes, on, 0, false, no, off"
    )

def phase78_enabled(environ: Mapping[str, str] | None = None) -> bool:
    values = os.environ if environ is None else environ
    return _boolean(values.get("PHASE78_ENABLED"))


def _absolute(values: Mapping[str, str], name: str) -> Path:
    raw = values.get(name, "")
    if not isinstance(raw, str) or not raw.strip():
        raise Phase78ConfigurationError(f"{name} is required when PHASE78_ENABLED=true")
    path = Path(raw)
    if not path.is_absolute():
        raise Phase78ConfigurationError(
            f"{name} must be an absolute path when PHASE78_ENABLED=true"
        )
    return path


def _positive_integer(
    values: Mapping[str, str], name: str, default: int, maximum: int
) -> int:
    raw = values.get(name)
    if raw is None or raw == "":
        return default
    if not isinstance(raw, str) or not raw.isascii() or not raw.isdecimal():
        raise Phase78ConfigurationError(f"{name} must be a positive integer")
    result = int(raw)
    if not 1 <= result <= maximum:
        raise Phase78ConfigurationError(
            f"{name} must be between 1 and {maximum}"
        )
    return result


def load_phase78_settings(
    environ: Mapping[str, str] | None = None,
) -> Phase78Settings:
    """Load settings without touching any Phase 7+8 path while disabled."""

    values = os.environ if environ is None else environ
    enabled = _boolean(values.get("PHASE78_ENABLED"))
    if not enabled:
        # Deliberately do not parse or construct path/deadline values.  This is
        # the first resource boundary for CLI, service, scheduler and worker.
        return Phase78Settings(enabled=False)

    fence = values.get("PHASE78_AUTHORITY_SOURCE_FENCE_SHA256", "")
    if not isinstance(fence, str) or _SHA256.fullmatch(fence) is None:
        raise Phase78ConfigurationError(
            "PHASE78_AUTHORITY_SOURCE_FENCE_SHA256 must be lowercase SHA-256 hex"
        )
    deadline_ms = _positive_integer(
        values, "PHASE78_DEADLINE_MS", 30_000, 300_000
    )
    # Keep the public deadline validator as the single numeric range contract.
    try:
        validate_deadline_ms(deadline_ms)
    except ValueError as exc:  # pragma: no cover - guarded by parser above
        raise Phase78ConfigurationError(str(exc)) from exc
    return Phase78Settings(
        enabled=True,
        authority_database=_absolute(values, "PHASE78_AUTHORITY_DB_FILE"),
        authority_source_fence_sha256=fence,
        phase6_database=_absolute(values, "PHASE78_PHASE6_DB_FILE"),
        phase7_database=_absolute(values, "PHASE78_PHASE7_DB_FILE"),
        phase8_database=_absolute(values, "PHASE78_PHASE8_DB_FILE"),
        work_database=_absolute(values, "PHASE78_WORK_DB_FILE"),
        work_spool=_absolute(values, "PHASE78_WORK_SPOOL"),
        project_root=_absolute(values, "PHASE78_PROJECT_ROOT"),
        cas_root=_absolute(values, "PHASE78_CAS_ROOT"),
        scratch_root=_absolute(values, "PHASE78_SCRATCH_ROOT"),
        deadline_ms=deadline_ms,
        lease_seconds=_positive_integer(
            values, "PHASE78_LEASE_SECONDS", 30, 86_400
        ),
    )
