"""Run-wide model allowlist (``FACTORY_MODEL_ALLOWLIST``).

A single-model run (for example an acceptance run that must use one model for
every Step and every Judge role) needs more than per-Step assignments in
``web/model_config.json``: the dispatcher deliberately appends each Step's
catalog defaults after the assigned primary and fallback, so a failing
assigned model would silently continue on a different one.

When ``FACTORY_MODEL_ALLOWLIST`` is set to a comma- or space-separated list of
model ids, every candidate outside it is skipped with a recorded reason, the
same way ``CODEX_ONLY`` skips non-codex backends.  A Step whose candidates are
all outside the list fails visibly instead of switching model.  Unset or empty
means no restriction.
"""

from __future__ import annotations

import os
import re
from typing import Mapping

ALLOWLIST_ENV = "FACTORY_MODEL_ALLOWLIST"


def model_allowlist(environ: Mapping[str, str] | None = None) -> frozenset[str] | None:
    """The allowed model ids, or ``None`` when the run is unrestricted."""

    raw = (os.environ if environ is None else environ).get(ALLOWLIST_ENV, "")
    ids = frozenset(item for item in re.split(r"[,\s]+", raw.strip()) if item)
    return ids or None


def model_allowed(model_id: str, environ: Mapping[str, str] | None = None) -> bool:
    allowlist = model_allowlist(environ)
    return allowlist is None or str(model_id) in allowlist


def skip_reason(environ: Mapping[str, str] | None = None) -> str:
    allowlist = model_allowlist(environ) or frozenset()
    return f"skipped: {ALLOWLIST_ENV} allows only {', '.join(sorted(allowlist))}"
