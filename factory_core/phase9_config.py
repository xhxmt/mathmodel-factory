"""Strict default-off configuration for Phase9-A evidence finalization."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
from typing import Mapping


_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class Phase9ConfigurationError(RuntimeError):
    code = "PHASE9_CONFIGURATION_INVALID"


@dataclass(frozen=True, slots=True)
class Phase9Settings:
    enabled: bool = False
    authority_database: Path | None = None
    authority_source_fence_sha256: str | None = None
    source_repository: Path | None = None
    evidence_root: Path | None = None


def _boolean(value: object) -> bool:
    if value is None or value == "":
        return False
    if not isinstance(value, str):
        raise Phase9ConfigurationError("PHASE9_ENABLED has an invalid value")
    normalized = value.strip().lower()
    if normalized in _TRUE:
        return True
    if normalized in _FALSE:
        return False
    raise Phase9ConfigurationError(
        "PHASE9_ENABLED must be one of: 1, true, yes, on, 0, false, no, off"
    )


def phase9_enabled(environ: Mapping[str, str] | None = None) -> bool:
    values = os.environ if environ is None else environ
    return _boolean(values.get("PHASE9_ENABLED"))


def _absolute(values: Mapping[str, str], name: str) -> Path:
    raw = values.get(name, "")
    if not isinstance(raw, str) or not raw.strip():
        raise Phase9ConfigurationError(f"{name} is required when PHASE9_ENABLED=true")
    path = Path(raw)
    if not path.is_absolute():
        raise Phase9ConfigurationError(f"{name} must be an absolute path")
    return path


def load_phase9_settings(
    environ: Mapping[str, str] | None = None,
) -> Phase9Settings:
    """Return before parsing or touching any configured path while disabled."""

    values = os.environ if environ is None else environ
    if not _boolean(values.get("PHASE9_ENABLED")):
        return Phase9Settings(enabled=False)
    fence = values.get("PHASE9_AUTHORITY_SOURCE_FENCE_SHA256", "")
    if not isinstance(fence, str) or _SHA256.fullmatch(fence) is None:
        raise Phase9ConfigurationError(
            "PHASE9_AUTHORITY_SOURCE_FENCE_SHA256 must be lowercase SHA-256 hex"
        )
    return Phase9Settings(
        enabled=True,
        authority_database=_absolute(values, "PHASE9_AUTHORITY_DB_FILE"),
        authority_source_fence_sha256=fence,
        source_repository=_absolute(values, "PHASE9_SOURCE_REPOSITORY"),
        evidence_root=_absolute(values, "PHASE9_EVIDENCE_ROOT"),
    )
