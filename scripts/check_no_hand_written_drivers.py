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
  * a script that reads a driver's source and writes modified source back - the
    clone idiom the two retired patchers used, which is why the retirement unit
    was the chain rather than the file

Explicitly allowed, because they are not workflow advances: ``subprocess.run``,
``os``/``re``/``asyncio``/executor ``run`` calls, ``self.run`` and other ordinary
method calls, the engine's own internal pipeline and service calls, and any
occurrence inside a comment, docstring or string literal.

Usage:
    check_no_hand_written_drivers.py <work-dir> [--json <path>]

Exit status is 0 when the criterion holds, 1 when it does not.

Two limits are deliberate and worth stating.  A scan that finds no Python file
**fails**: an empty scan is not a clean one, and reporting OK for a directory
nothing was read from is the same "agreement reported because nothing was looked
at" that this checker exists to catch.  And the advance pattern tolerates one level
of nested parentheses, so ``.engine(Path(P)).run(`` is detected while a second level
of nesting is not - the pattern is a detector, not a parser, and that direction of
error is a miss rather than a false pass.
"""
from __future__ import annotations

import argparse
import io
import json
import re
import sys
import tokenize
from pathlib import Path

#: One level of nested parentheses, so an argument that is itself a call still
#: matches: ``.engine(Path(P)).run(`` is an advance, and ``[^)]*`` stopped at the
#: first ``)`` and saw nothing.  A second level of nesting (``f(g(h()))``) is not
#: covered - the pattern is a detector, not a parser, and a miss there is a miss
#: rather than a false pass, which is the direction that matters here.
_ARG = r"(?:[^()]|\([^()]*\))*"

#: A workflow advance through the engine, in any of the inline spellings.
WORKFLOW_ADVANCE = re.compile(
    rf"(?:FactoryService|FactoryEngine)\s*\({_ARG}\)\s*\.engine\({_ARG}\)\s*\.run\("
    rf"|\.engine\({_ARG}\)\s*\.run\("
    r"|\bengine\.run\("
)

#: A name bound to a factory, a service or an engine.  ``service =
#: FactoryService(ROOT)`` followed by ``service.engine(P).run(...)`` is a workflow
#: advance that no inline pattern sees, and the first version of this checker
#: reported such a file as clean.
#:
#: ``Engine\s*\(`` alone was too broad in both directions.  Too broad because it
#: matched any name *ending* in ``Engine``, so ``game = GameEngine(config)`` bound
#: ``game`` and every later ``game.run()`` - an ordinary method call - was reported;
#: the lookbehind keeps ``FactoryEngine(`` and a bare ``Engine(`` while refusing
#: ``GameEngine(``.  Too broad in the other direction too, because it was applied
#: to raw text: a *docstring* that quotes the old shape
#: (``service = FactoryService(ROOT)``) bound ``service`` for the whole file, so a
#: live ``service.run(...)`` that never touched a factory was flagged.  The scan
#: now runs this against text with comments and string literals blanked out.
SERVICE_BINDING = re.compile(
    r"^\s*(\w+)\s*=\s*[^\n]*(?:FactoryService|FactoryEngine|\.engine\s*\(|(?<![\w.])Engine\s*\()[^\n]*$",
    re.MULTILINE,
)


def workflow_advance_sites(text: str) -> list[re.Match]:
    """Every workflow-advance expression in one file's source.

    The inline spellings plus any advance reached through a name bound to a
    factory or an engine.  Both are needed: the bound form is how a script written
    in two statements gets past a checker that only knows one-liners.

    Bindings are read from the source with comments and string literals blanked,
    so a file's own documentation cannot manufacture a name; and the two channels
    are de-duplicated by position, because ``engine = service.engine(P)`` followed
    by ``engine.run(...)`` is one advance, not two.
    """

    code = blanked(text, ignored_spans(text))
    matches = list(WORKFLOW_ADVANCE.finditer(text))
    names = sorted({match.group(1) for match in SERVICE_BINDING.finditer(code)})
    if names:
        bound = re.compile(
            r"\b(?:"
            + "|".join(re.escape(name) for name in names)
            + rf")\s*\.\s*(?:engine\({_ARG}\)\s*\.\s*)?run\("
        )
        matches.extend(bound.finditer(text))
    # The two channels describe the same call with *different* spans: for
    # ``service = FactoryService(ROOT)`` then ``service.engine(P).run(...)`` the
    # bound match starts at ``service`` and the inline one at ``engine``.  Merging
    # on identical spans alone therefore still reported one advance twice, so the
    # rule is overlap - keep the earliest (widest) span and drop the ones inside it.
    kept: list[re.Match] = []
    for match in sorted(matches, key=lambda match: (match.start(), -match.end())):
        if kept and match.start() < kept[-1].end():
            continue
        kept.append(match)
    return kept

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


def driver_source_rewrite_sites(text: str) -> list[tuple[int, int, str]]:
    """The driver files a script reads and writes back, with match offsets.

    Returns ``(match start, match end, driver)``.  Both offsets are needed: the
    match *begins* inside the filename string and *ends* in code (at the
    ``.write_text(`` of the output), so the "is this a comment or a string"
    question has to be asked at the end - the same rule the ``progress.json``
    journal needs, for the same reason.  Asking it at the start classified every
    clone site as documentation and let it through.
    """

    sites: list[tuple[int, int, str]] = []
    for match in DRIVER_SOURCE_REWRITE.finditer(text):
        # Only the text before the ``.py`` that is read: the match runs on to the
        # ``.write_text(`` of the *output*, so the write target appears inside it
        # and naming that would answer a different question.
        window = text[max(0, match.start() - 160) : match.start() + 3]
        names = re.findall(r"([\w./-]+\.py)", window)
        if names:
            # One clone site is one finding.  The window can mention more than one
            # path, and emitting an entry per name turned a single site into several
            # violations whose ``driver`` named something the site did not rewrite.
            sites.append((match.start(), match.end(), names[0]))
    return sites


def rewrites_driver_source(text: str) -> list[str]:
    """The driver files a script reads and writes back, if any.

    The match begins at the ``.py`` itself, so the window reaches back far enough
    to include the path that precedes it; otherwise the first filename found is
    the one being *written*, and the answer names the output instead of the input.
    """

    files: list[str] = []
    for _start, _end, name in driver_source_rewrite_sites(text):
        if name not in files:
            files.append(name)
    return files


def is_backup(relative: str) -> bool:
    name = Path(relative).name
    return any(marker in name for marker in BACKUP_MARKERS)


def blanked(text: str, spans: list[tuple[int, int]]) -> str:
    """``text`` with the ignored spans replaced by spaces, offsets preserved.

    Blanking rather than deleting keeps every other offset - and so every line and
    column - exactly where it was, so a position read from this copy still indexes
    the original.
    """

    if not spans:
        return text
    characters = list(text)
    for start, end in spans:
        for index in range(max(0, start), min(len(characters), end)):
            if characters[index] != "\n":
                characters[index] = " "
    return "".join(characters)


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
        # The clone idiom: a script that reads a driver and writes its source back.
        # ``rewrites_driver_source`` existed for this but ``scan`` never called it,
        # so the one pattern that regenerates a retired driver was the only one the
        # standing check did not look for.
        for start, end, rewritten in driver_source_rewrite_sites(text):
            # The string test is taken at the END of the match: it starts inside the
            # filename literal and finishes in code, at the output's ``.write_text(``.
            # Taking it at the start would read every clone site as documentation.
            if ignored(end - 1):
                residual.append({"file": relative, "kind": "in_comment_or_string"})
                continue
            entry = {
                "file": relative,
                "line": text[:start].count("\n") + 1,
                "kind": "driver_source_rewrite",
                "snippet": text.splitlines()[text[:start].count("\n")].strip()[:100],
                "driver": rewritten,
            }
            if relative not in exemptions:
                violations.append(entry)
            else:
                residual.append(entry)

    # A scan that found nothing to scan has not cleared anything.  Reporting OK on
    # an empty tree - or on a mistyped path that happens to exist - is the same
    # failure this checker exists to find: agreement reported because nothing was
    # looked at.
    vacuous = not files
    return {
        "root": str(root),
        "files_scanned": len(files),
        "exemptions": sorted(exemptions),
        "violations": violations,
        "residual": residual,
        "vacuous": vacuous,
        "ok": not violations and not vacuous,
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

    if report["vacuous"]:
        print(
            f"FAIL: no Python file under {report['root']}, so nothing was checked. "
            "An empty scan is not a clean one - point the checker at the project's "
            "work/ directory."
        )
        return 1

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
