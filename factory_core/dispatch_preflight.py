"""Resource check before a Step is dispatched (optimization plan, phase 3d).

Two acceptance runs stopped mid-Step on causes that were visible before any
model was called: a provider balance that answered 402, and a scratch file
system that filled.  The engine now runs this check after a Step's prepare and
before ``STEP_STARTED``; a blocking result pauses the run with subcode
``PROVIDER_UNAVAILABLE`` and consumes no attempt.

Only a decided ``FAIL`` blocks: every candidate model of the Step fails (no
executable, no credential, a rejected credential, an exhausted balance), or a
scratch location is below the free-space minimum.  ``UNKNOWN`` and ``WARN``
(an unverifiable provider, a fallback in use, a rate limit) never block, and a
check that itself crashes never blocks either -- this is an early warning,
not a new way to stop a healthy run.  The report carries no credential.
"""

from __future__ import annotations

import logging
import os
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Mapping

from .preflight import FAIL, HttpGet, _urllib_get, check_routes, check_scratch_space

if TYPE_CHECKING:
    from .registry import StepDefinition

logger = logging.getLogger(__name__)

PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
DISPATCH_MIN_FREE_BYTES = 2 * 2**30
ENABLE_ENV = "FACTORY_DISPATCH_PREFLIGHT"

DispatchPreflight = Callable[["StepDefinition"], "dict | None"]

# Subtasks whose Step id does not name the models they call, so probing that
# Step's routes could block a healthy run:
# - ``final_judge`` (v2): runs the final audit as execution Step 16, so its
#   Judge roles use the Step-16 assignments, not Step 13's (Step 16 itself has
#   no default model and always passes);
# - ``conditional_math_preflight_skip`` (v1 Step 13 when no math changed):
#   writes the skip receipt and calls no model at all.
_ROUTED_ELSEWHERE = frozenset({"final_judge", "conditional_math_preflight_skip"})


def enabled(environ: Mapping[str, str] | None = None) -> bool:
    value = (os.environ if environ is None else environ).get(ENABLE_ENV, "1")
    return value.strip().lower() not in {"0", "false", "no", "off"}


def check_dispatch(
    factory_root: Path,
    project_dir: Path,
    step_id: int,
    *,
    environ: Mapping[str, str] | None = None,
    http_get: HttpGet | None = _urllib_get,
    mounts_text: str | None = None,
    min_free_bytes: int = DISPATCH_MIN_FREE_BYTES,
    timeout: float = 10.0,
) -> dict | None:
    """The blocking report for dispatching ``step_id`` now, or ``None``."""

    try:
        scratch = check_scratch_space(
            factory_root, project_dir, min_free_bytes=min_free_bytes,
            environ=environ, mounts_text=mounts_text,
        )
        steps, models = check_routes(
            factory_root, project_dir.name, [int(step_id)], project_dir=project_dir,
            environ=environ, http_get=http_get, timeout=timeout,
        )
    except Exception:  # noqa: BLE001 - a crashing check must not stop a healthy run
        # Not blocking, but not silent either: a broken preflight would
        # otherwise switch the gate off without a trace.
        logger.warning(
            "dispatch preflight for Step %s crashed; dispatching without it",
            step_id,
            exc_info=True,
        )
        return None
    blocking = [check for check in scratch + steps if check.status == FAIL]
    if not blocking:
        return None
    # Name why each candidate of a blocked Step fails (e.g. "HTTP 402: the
    # account has no balance"), not only that all of them do.
    by_model = {check.name: check for check in models}
    reasons = []
    for check in blocking:
        reasons.append(f"{check.name}: {check.detail}")
        for model_id in check.data.get("candidates", []):
            model = by_model.get(f"model:{model_id}")
            if model is not None and model.status == FAIL:
                reasons.append(f"{model.name}: {model.detail}")
    return {
        "schema": "factory-dispatch-preflight-v1",
        "step": int(step_id),
        "blocking": [asdict(check) for check in blocking],
        "models": [asdict(check) for check in models],
        "message": "; ".join(reasons),
    }


def for_project(factory_root: Path, project_dir: Path) -> DispatchPreflight | None:
    """The engine hook for one project, or ``None`` when the check is disabled."""

    if not enabled():
        return None
    root, project = Path(factory_root), Path(project_dir)

    def hook(definition) -> dict | None:
        if definition.name in _ROUTED_ELSEWHERE:
            return None
        return check_dispatch(root, project, definition.id)

    return hook
