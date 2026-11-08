from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


RESOLVED_STATUSES = frozenset({"RESOLVED", "REFRAMED", "DROPPED", "CLOSED"})
SECTION_HEADING = "## Incremental audit findings"
HEADER = (
    "| Issue ID | First Raised In | Category | Severity | Blocking Until | "
    "Status | Notes |"
)
SEPARATOR = "|---|---|---|---|---|---|---|"


@dataclass(frozen=True)
class LedgerFinding:
    issue_id: str
    raised_in: str
    category: str
    severity: str
    blocking_until: str
    status: str
    notes: str

    def row(self) -> str:
        values = (
            self.issue_id,
            self.raised_in,
            self.category,
            self.severity,
            self.blocking_until,
            self.status,
            self.notes,
        )
        safe = [
            value.replace("|", "/").replace("\n", " ").strip()
            for value in values
        ]
        return "| " + " | ".join(safe) + " |"


def _cells(line: str) -> list[str]:
    return [
        cell.strip().replace("*", "").replace("`", "")
        for cell in line.strip().strip("|").split("|")
    ]


def _unresolved_severity_rows(path: Path, severities: set[str]):
    if not path.is_file():
        return
    targets = {str(value).upper() for value in severities}
    header: dict[str, int] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if "|" not in line:
            header = {}
            continue
        cells = _cells(line)
        lowered = [cell.lower() for cell in cells]
        if "severity" in lowered and "status" in lowered:
            header = {name: lowered.index(name) for name in ("severity", "status")}
            continue
        if not cells or all(set(cell) <= {"-", ":"} for cell in cells if cell):
            continue
        if header:
            try:
                severity = cells[header["severity"]].upper()
                status = cells[header["status"]].upper()
            except IndexError:
                continue
            if severity in targets and status not in RESOLVED_STATUSES:
                yield cells
            continue
        normalized = {cell.upper() for cell in cells}
        if normalized & targets and not (normalized & RESOLVED_STATUSES):
            yield cells


def has_unresolved_severity(path: Path, severities: set[str]) -> bool:
    return next(_unresolved_severity_rows(path, severities), None) is not None


def has_unresolved_blocking(path: Path, acknowledged: frozenset[str] = frozenset()) -> bool:
    """An unresolved BLOCKING row remains.

    ``acknowledged`` are issue ids a verified content-freeze approval took on
    (``acknowledged_human_issues``); only rows a human or Step 14/15 resolves
    (``OpenIssue.deferred_past_review``) can be acknowledged, so every other
    BLOCKING row still blocks.
    """

    total = sum(1 for _ in _unresolved_severity_rows(path, {"BLOCKING"}))
    if not total or not acknowledged:
        return bool(total)
    taken = {issue.issue_id for issue in deferred_blocking_issues(path)} & set(acknowledged)
    return total > len(taken)


def has_unresolved_critical(path: Path) -> bool:
    return has_unresolved_severity(path, {"BLOCKING", "MAJOR"})


#: Steps after the v2 Stage 8 exit that own a deferred review row.
DEFERRED_REVIEW_STEPS = frozenset({14, 15})


@dataclass(frozen=True)
class OpenIssue:
    issue_id: str
    category: str
    severity: str
    notes: str = ""
    blocking_until: str = ""

    @property
    def deferred_past_review(self) -> bool:
        """The row names only a resolver after the v2 Stage 8 exit.

        ``Blocking Until`` starting with ``Human`` (contest-team data such as
        cover fields, which no agent may invent) or naming only Step 14/15
        (content those later Steps produce, such as the abstract). Such a row
        cannot be resolved by any Stage 8 or recompute Step, so it does not
        block the review exit; it stays OPEN for its resolver, Gate 2 and the
        final audit. Every other value, including Step 16, keeps blocking.
        """

        text = self.blocking_until.strip()
        if text.lower().startswith("human"):
            return True
        steps = {int(value) for value in re.findall(r"step\s*(\d+)", text, re.IGNORECASE)}
        return bool(steps) and steps <= DEFERRED_REVIEW_STEPS


def open_review_issues(path: Path, severities: set[str]) -> list[OpenIssue]:
    """Unresolved rows outside ``## Incremental audit findings``.

    The machine section is excluded: its ``AUDIT-*`` rows are written and
    closed only by the audit profiles, so a Step that runs no profile cannot
    act on them (a hard row blocked its own Step already; a warning row is
    reviewed by the final audit). Only tables with Severity and Status
    columns are read; a row without a Category column reports ``""``.
    """

    if not path.is_file():
        return []
    targets = {str(value).upper() for value in severities}
    issues: list[OpenIssue] = []
    in_machine_section = False
    header: dict[str, int] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("## "):
            in_machine_section = line.strip() == SECTION_HEADING
            header = {}
            continue
        if "|" not in line:
            header = {}
            continue
        if in_machine_section:
            continue
        cells = _cells(line)
        lowered = [cell.lower() for cell in cells]
        if "severity" in lowered and "status" in lowered:
            header = {
                name: lowered.index(name)
                for name in ("issue id", "category", "severity", "status", "notes", "blocking until")
                if name in lowered
            }
            continue
        if not header or not cells or all(set(cell) <= {"-", ":"} for cell in cells if cell):
            continue
        try:
            severity = cells[header["severity"]].upper()
            status = cells[header["status"]].upper()
        except IndexError:
            continue
        if severity not in targets or status in RESOLVED_STATUSES:
            continue
        category = cells[header["category"]].lower() if "category" in header and header["category"] < len(cells) else ""
        issue_id = cells[header["issue id"]] if "issue id" in header and header["issue id"] < len(cells) else cells[0]
        notes = cells[header["notes"]] if "notes" in header and header["notes"] < len(cells) else ""
        until = (cells[header["blocking until"]]
                 if "blocking until" in header and header["blocking until"] < len(cells) else "")
        issues.append(OpenIssue(issue_id, category, severity, notes, until))
    return issues


def deferred_blocking_issues(path: Path) -> list[OpenIssue]:
    """Open BLOCKING rows only a human or Step 14/15 resolves (F16/F17).

    These are the rows a content-freeze approval must acknowledge in a
    catalog v2 run; the final audit then does not count them.
    """

    return [
        issue
        for issue in open_review_issues(path, {"BLOCKING"})
        if issue.deferred_past_review
    ]


def acknowledged_human_issues(project: Path) -> frozenset[str]:
    """Issue ids the current, verified content-freeze approval acknowledged.

    Read through the non-mutating approval reader; an unreadable, missing,
    stale or rejected decision acknowledges nothing.
    """

    from ..storage import SQLiteStateStore

    try:
        store = SQLiteStateStore(project)
        if not store.path.is_file():
            return frozenset()
        _required, approvals = store.read_finalization_approvals()
    except (OSError, ValueError):
        return frozenset()
    decision = approvals.get("content_freeze") or {}
    if decision.get("approved") is not True:
        return frozenset()
    raw = decision.get("acknowledged_human_issues")
    if not isinstance(raw, list):
        return frozenset()
    return frozenset(str(value) for value in raw if isinstance(value, str) and value)


def sync_incremental_findings(path: Path, findings: list[LedgerFinding]) -> None:
    if path.is_file():
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    else:
        lines = [f"# Audit Issue Ledger — `{path.parent.name}`", ""]

    if SECTION_HEADING not in lines:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend([SECTION_HEADING, "", HEADER, SEPARATOR])

    by_id = {finding.issue_id: finding for finding in findings}
    existing: set[str] = set()
    section_index = lines.index(SECTION_HEADING)
    for index in range(section_index + 1, len(lines)):
        line = lines[index]
        if index > section_index + 1 and line.startswith("## "):
            break
        if "|" not in line:
            continue
        cells = _cells(line)
        if not cells:
            continue
        issue_id = cells[0]
        if issue_id in by_id:
            lines[index] = by_id[issue_id].row()
            existing.add(issue_id)

    insert_at = len(lines)
    for index in range(section_index + 1, len(lines)):
        if lines[index].startswith("## "):
            insert_at = index
            break
    new_rows = [
        finding.row() for finding in findings if finding.issue_id not in existing
    ]
    if new_rows:
        lines[insert_at:insert_at] = new_rows
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
