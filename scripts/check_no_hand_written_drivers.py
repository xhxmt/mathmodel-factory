#!/usr/bin/env python3
"""Fail if a work-style project tree has grown a hand-written driver again.

S6 added ``run_bounded`` so that "advance this project, within this scope, from
this exact state, without touching these artifacts" is one authorised call
instead of a script.  G4.5c migrated the twenty drivers that existed.  This
checker is what keeps the layer from growing back: it turns that migration into
a standing constraint rather than a one-off cleanup.

It is deliberately a *semantic* check, not a count of ``.run(``.  Twenty-five of
the eighty-six ``.run(`` occurrences in the tree are ``subprocess.run`` and
thirteen are ordinary method calls; chasing them to zero would be cosmetic.  What
must be zero is a *workflow advance* outside the supported entry point:

  * ``FactoryService(...).engine(...).run(...)`` / ``FactoryEngine(...).run(...)``
  * ``service.engine(...).run(...)`` / ``engine.run(max_steps=...)``
  * a private ``StepRegistry`` subclass whose ``get``/``next_after`` are overridden
    to change Step ceilings, which is how the drivers used to bound attempts
  * a hand-written ``progress.json`` journal written next to a workflow advance

Explicitly allowed, because they are not workflow advances: ``subprocess.run``,
``os``/``re``/``asyncio``/executor ``run`` calls, ``self.run`` and other ordinary
method calls, the engine's own internal pipeline and service calls, and any
occurrence inside a comment, docstring or string literal.

Usage:
    check_no_hand_written_drivers.py <work-dir> [--json <path>]

Exit status is 0 when the criterion holds, 1 when it does not.
"""
from __future__ import annotations

import argparse
import io
import json
import re
import sys
import tokenize
from pathlib import Path

#: A workflow advance through the engine, in any of the inline spellings.
WORKFLOW_ADVANCE = re.compile(
    r"(?:FactoryService|FactoryEngine)\s*\([^)]*\)\s*\.engine\([^)]*\)\s*\.run\("
    r"|\.engine\([^)]*\)\s*\.run\("
    r"|\bengine\.run\("
)

#: A name bound to a factory, a service or an engine.  ``service =
#: FactoryService(ROOT)`` followed by ``service.engine(P).run(...)`` is a workflow
#: advance that no inline pattern sees, and the first version of this checker
#: reported such a file as clean.
SERVICE_BINDING = re.compile(
    r"^\s*(\w+)\s*=\s*[^\n]*(?:FactoryService|FactoryEngine|\.engine\s*\(|Engine\s*\()[^\n]*$",
    re.MULTILINE,
)


def workflow_advance_sites(text: str) -> list[re.Match]:
    """Every workflow-advance expression in one file's source.

    The inline spellings plus any advance reached through a name bound to a
    factory or an engine.  Both are needed: the bound form is how a script
    written in two statements gets past a checker that only knows one-liners.
    """

    matches = list(WORKFLOW_ADVANCE.finditer(text))
    names = sorted({match.group(1) for match in SERVICE_BINDING.finditer(text)})
    if names:
        bound = re.compile(
            r"\b(?:"
            + "|".join(re.escape(name) for name in names)
            + r")\s*\.\s*(?:engine\([^)]*\)\s*\.\s*)?run\("
        )
        matches.extend(bound.finditer(text))
    return sorted(matches, key=lambda match: match.start())

#: A private registry whose Step ceilings are rewritten - the driver idiom.
REGISTRY_SHIM = re.compile(
    r"class\s+\w*Registry\w*\s*\(\s*StepRegistry\s*\)"
    r"|dataclasses\.replace\(\s*definition\s*,\s*max_attempts"
    r"|\.registry\s*=\s*\w*Registry"
)

#: A hand-written journal written by a workflow driver.  The filename lives in a
#: string, so the match is anchored on the code that follows it - checking the
#: start of this pattern would classify it as "inside a string" and miss it.
PROGRESS_JOURNAL = re.compile(r"['\"]progress\.json['\"]\s*\)\s*\.write_text\(")

#: Files whose name marks them as a preserved copy rather than live code.  They
#: are enumerated by the caller so the exemption list cannot grow silently.
BACKUP_MARKERS = (".before", "original", "_before.py")


#: A script that reads a driver's source and writes modified source back.  This is
#: the clone idiom the two retired patchers used, and the reason the retirement
#: unit was the chain rather than the file: rewriting a driver breaks the patcher
#: that generates it.  Named here so a test can exercise the definition instead of
#: restating the pattern in its own regex.
DRIVER_SOURCE_REWRITE = re.compile(
    r"\.py['\"]\s*\)\s*\.read_text\(\)[\s\S]{0,400}?\.write_text\("
)


def rewrites_driver_source(text: str) -> list[str]:
    """The driver files a script reads and writes back, if any.

    The match begins at the ``.py`` itself, so the window reaches back far enough
    to include the path that precedes it; otherwise the first filename found is
    the one being *written*, and the answer names the output instead of the input.
    """

    files: list[str] = []
    for match in DRIVER_SOURCE_REWRITE.finditer(text):
        # Only the text before the ``.py`` that is read: the match runs on to the
        # ``.write_text(`` of the *output*, so the write target appears inside it
        # and naming that would answer a different question.
        window = text[max(0, match.start() - 160) : match.start() + 3]
        for name in re.findall(r"([\w./-]+\.py)", window):
            if name not in files:
                files.append(name)
    return files


def is_backup(relative: str) -> bool:
    name = Path(relative).name
    return any(marker in name for marker in BACKUP_MARKERS)


def ignored_spans(text: str) -> list[tuple[int, int]]:
    """Character ranges occupied by comments and string literals.

    A checker must not read a script's own documentation: the migrated drivers
    explain the patterns they removed, and a naive scan flags those explanations.
    """

    starts: list[int] = []
    offset = 0
    for line in text.splitlines(keepends=True):
        starts.append(offset)
        offset += len(line)
    starts.append(offset)
    spans: list[tuple[int, int]] = []
    try:
        for token in tokenize.generate_tokens(io.StringIO(text).readline):
            if token.type in (tokenize.COMMENT, tokenize.STRING):
                spans.append(
                    (
                        starts[token.start[0] - 1] + token.start[1],
                        starts[token.end[0] - 1] + token.end[1],
                    )
                )
    except (tokenize.TokenError, IndentationError):
        pass
    return spans


def scan(root: Path, *, exemptions: frozenset[str] = frozenset()) -> dict:
    """Classify every residual occurrence and report the violations."""

    violations: list[dict] = []
    residual: list[dict] = []
    files = sorted(root.rglob("*.py"))
    for path in files:
        relative = path.relative_to(root).as_posix()
        text = path.read_text(encoding="utf-8", errors="replace")
        spans = ignored_spans(text)

        def ignored(position: int) -> bool:
            return any(start <= position < end for start, end in spans)

        def record(
            pattern: re.Pattern | list,
            kind: str,
            violation: bool,
            *,
            at_end: bool = False,
        ) -> None:
            """``at_end`` for patterns whose match begins inside a string but ends in
            code - the progress.json journal, where the code is the ``.write_text(``
            that follows the filename.  A list is accepted so a rule can combine
            several patterns (see ``workflow_advance_sites``)."""

            matches = pattern if isinstance(pattern, list) else list(pattern.finditer(text))
            for match in matches:
                position = match.end() - 1 if at_end else match.start()
                if ignored(position):
                    residual.append({"file": relative, "kind": "in_comment_or_string"})
                    continue
                entry = {
                    "file": relative,
                    "line": text[: match.start()].count("\n") + 1,
                    "kind": kind,
                    "snippet": text.splitlines()[text[: match.start()].count("\n")].strip()[:100],
                }
                if violation and relative not in exemptions:
                    violations.append(entry)
                else:
                    residual.append(entry)

        record(workflow_advance_sites(text), "workflow_advance", True)
        record(REGISTRY_SHIM, "registry_shim", True)
        if workflow_advance_sites(text) and PROGRESS_JOURNAL.search(text):
            record(PROGRESS_JOURNAL, "progress_journal_with_advance", True, at_end=True)

    return {
        "root": str(root),
        "files_scanned": len(files),
        "exemptions": sorted(exemptions),
        "violations": violations,
        "residual": residual,
        "ok": not violations,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("work_dir", type=Path)
    parser.add_argument("--json", type=Path, default=None)
    parser.add_argument(
        "--exempt",
        action="append",
        default=[],
        help="a project-relative file exempted from the check; repeatable",
    )
    args = parser.parse_args(argv)

    if not args.work_dir.is_dir():
        print(f"not a directory: {args.work_dir}", file=sys.stderr)
        return 2

    report = scan(args.work_dir, exemptions=frozenset(args.exempt))
    if args.json:
        args.json.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    if report["ok"]:
        print(
            f"OK: no hand-written workflow advance in {report['files_scanned']} files "
            f"under {report['root']}"
            + (f" ({len(report['exemptions'])} exempted)" if report["exemptions"] else "")
        )
        return 0

    print(f"FAIL: {len(report['violations'])} hand-written driver pattern(s):")
    for entry in report["violations"]:
        print(f"  {entry['file']}:{entry['line']}  [{entry['kind']}]  {entry['snippet']}")
    print()
    print("Use service.advance_bounded(project, BoundedRunContract(...)) instead.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
