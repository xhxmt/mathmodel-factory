#!/usr/bin/env python3
"""Aggregate independent judge outputs with non-averagable correctness vetoes."""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path


VERDICT_RE = re.compile(r"^VERDICT:\s*([A-Z_]+)\s*$", re.MULTILINE)
FATAL_RE = re.compile(r"^FATAL_FLAWS:\s*(\d+)\s*$", re.MULTILINE)
SCORE_RE = re.compile(r"^SCORE:\s*(\d+(?:\.\d+)?)\s*$", re.MULTILINE)


@dataclass(frozen=True)
class RoleResult:
    role: str
    status: str
    verdict: str | None
    fatal_flaws: int | None
    score: float | None
    source: str
    text: str


@dataclass(frozen=True)
class AggregateResult:
    verdict: str
    status: str
    paper_score: float | None
    vetoes: tuple[str, ...]
    indeterminate_roles: tuple[str, ...]
    roles: tuple[RoleResult, ...]


def _read_role(path: Path, role: str) -> RoleResult:
    if not path.is_file():
        return RoleResult(role, "INDETERMINATE", None, None, None, str(path), "")
    text = path.read_text(encoding="utf-8", errors="replace")
    verdict_match = VERDICT_RE.search(text)
    verdict = verdict_match.group(1) if verdict_match else None
    fatal_match = FATAL_RE.search(text)
    fatal = int(fatal_match.group(1)) if fatal_match else None
    score_match = SCORE_RE.search(text)
    score = float(score_match.group(1)) if score_match else None

    if role in {"math", "execution"}:
        if verdict not in {"PASS", "FAIL"} or fatal is None:
            status = "INDETERMINATE"
        elif verdict == "FAIL" or fatal > 0:
            status = "FAIL"
        else:
            status = "PASS"
    else:
        if verdict not in {"PASS", "REVISE"} or score is None or not 0 <= score <= 100:
            status = "INDETERMINATE"
        elif verdict == "REVISE" or score < 70:
            status = "REVISE"
        else:
            status = "PASS"
    return RoleResult(role, status, verdict, fatal, score, str(path), text)


def aggregate_outputs(
    *, math_path: Path, execution_path: Path, paper_path: Path
) -> AggregateResult:
    roles = (
        _read_role(math_path, "math"),
        _read_role(execution_path, "execution"),
        _read_role(paper_path, "paper"),
    )
    hard_roles = roles[:2]
    vetoes = tuple(item.role for item in hard_roles if item.status == "FAIL")
    indeterminate = tuple(item.role for item in roles if item.status == "INDETERMINATE")
    paper = roles[2]

    if vetoes:
        status = "FAIL"
        verdict = "REOPEN_REVISION_MODEL"
    elif indeterminate:
        status = "INDETERMINATE"
        verdict = "REOPEN_REVISION_MODEL"
    elif paper.status == "REVISE":
        status = "REVISE"
        verdict = "REOPEN_REVISION_TEXT"
    else:
        status = "PASS"
        verdict = "PASS"
    return AggregateResult(
        verdict=verdict,
        status=status,
        paper_score=paper.score,
        vetoes=vetoes,
        indeterminate_roles=indeterminate,
        roles=roles,
    )


def write_aggregate_report(result: AggregateResult, output: Path, base_name: str) -> None:
    score = result.paper_score if result.paper_score is not None else 0
    veto_text = ", ".join(result.vetoes) if result.vetoes else "none"
    indeterminate_text = (
        ", ".join(result.indeterminate_roles) if result.indeterminate_roles else "none"
    )
    lines = [
        f"VERDICT: {result.verdict}",
        "",
        f"# Step 13 Independent Judge Aggregate - `{base_name}`",
        "",
        f"AGGREGATE_STATUS: {result.status}",
        f"整体得分: {score:g}/100",
        f"Correctness vetoes: {veto_text}",
        f"Indeterminate roles: {indeterminate_text}",
        "",
        "## Aggregation Rule",
        "",
        "Math and execution failures are hard vetoes.",
        "The paper score is reported but never averages away a correctness veto.",
        "Missing or malformed role output is INDETERMINATE and cannot pass.",
    ]
    for role in result.roles:
        lines.extend(
            [
                "",
                f"## {role.role.title()} Auditor",
                "",
                f"Parsed status: {role.status}",
                f"Parsed verdict: {role.verdict or 'MISSING'}",
                f"Fatal flaws: {role.fatal_flaws if role.fatal_flaws is not None else 'MISSING'}",
                f"Score: {role.score if role.score is not None else 'N/A'}",
                f"Source: `{role.source}`",
                "",
                role.text.strip() or "(missing output)",
            ]
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--math", required=True)
    parser.add_argument("--execution", required=True)
    parser.add_argument("--paper", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--json")
    args = parser.parse_args()
    result = aggregate_outputs(
        math_path=Path(args.math),
        execution_path=Path(args.execution),
        paper_path=Path(args.paper),
    )
    write_aggregate_report(result, Path(args.output), args.base)
    if args.json:
        data = asdict(result)
        Path(args.json).write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    print(f"{result.status}: {result.verdict}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
