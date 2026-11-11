"""Which Step owns a PDF visual-gate finding (acceptance A, F19(b)).

``scripts/pdf_visual_gate.py`` reports layout and figure defects of the
compiled paper.  When only those findings made the final Judge reopen the text,
the repair cycle resumed at the revision Step, although the figures are drawn
by Step 8 and the revision Step has no figure style contract.

The finding codes split into two owners:

* **figure** -- text drawn below 4.5pt (LaTeX never sets body text that small:
  ``\\tiny`` is 5pt at a 10pt base, so it comes from a scaled-down figure) and
  raster images below the resolution floor.  They are resolved through the
  ownership registry as ``figures/**`` and recomputed by that owner's Step.
* **layout** -- everything else (content outside the page, blank pages, edge
  clipping, overfull boxes, page limit, overlays, undefined references, fonts):
  ``paper.tex`` and the compile, which the revision Step owns.

A heuristic can misplace a finding (a table shrunk with ``\\resizebox`` also
yields sub-4.5pt text).  Re-running the figure Step then costs one extra Step
but loses nothing: the revision Step still runs afterwards with every finding.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

#: Blocking codes whose fix lives in the figure files.
FIGURE_FINDING_CODES = frozenset({"UNREADABLY_SMALL_TEXT", "LOW_IMAGE_RESOLUTION"})

#: The registry path every figure finding stands for.
FIGURE_ARTIFACT = "figures/visual_gate_finding.pdf"


def load_visual_gate(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def blocking_findings(report: dict[str, Any] | None) -> list[dict[str, Any]]:
    """The blocking findings of a FAIL report; nothing for any other status."""

    if not isinstance(report, dict) or report.get("status") != "FAIL":
        return []
    findings = report.get("findings")
    if not isinstance(findings, list):
        return []
    return [
        item
        for item in findings
        if isinstance(item, dict) and item.get("severity") == "blocking"
    ]


def finding_owner(code: object) -> str:
    return "figure" if str(code) in FIGURE_FINDING_CODES else "layout"


def figure_owner_steps(catalog) -> list[int]:
    """The Steps of the Stage the ownership registry gives ``figures/**``."""

    from ..artifact_policy import artifact_policy_owner_stage

    owner = artifact_policy_owner_stage(FIGURE_ARTIFACT)
    if owner is None:
        return []
    return sorted(
        subtask.source_step_id
        for subtask in catalog.stage_for_id(owner).subtasks
        if subtask.kind == "step"
    )


def figure_rework_plan(report: dict[str, Any] | None, catalog) -> dict[str, Any] | None:
    """The figure Steps to recompute for a report's blocking figure findings."""

    codes = sorted(
        {
            str(item.get("code"))
            for item in blocking_findings(report)
            if finding_owner(item.get("code")) == "figure"
        }
    )
    if not codes:
        return None
    steps = figure_owner_steps(catalog)
    if not steps:
        return None
    return {"steps": steps, "codes": codes, "artifact": FIGURE_ARTIFACT}
