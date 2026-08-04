from __future__ import annotations

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


def has_unresolved_blocking(path: Path) -> bool:
    if not path.is_file():
        return False
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
            if severity == "BLOCKING" and status not in RESOLVED_STATUSES:
                return True
            continue
        normalized = {cell.upper() for cell in cells}
        if "BLOCKING" in normalized and not (normalized & RESOLVED_STATUSES):
            return True
    return False


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
