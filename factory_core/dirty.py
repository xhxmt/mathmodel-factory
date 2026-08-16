from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from .artifact_ownership import artifact_ownership
from .paper_sources import mask_inactive_latex, resolve_latex_dependency_graph


DIRTY_CLASSIFIER_SCHEMA = "factory-dirty-classifier-v4"


class DirtyFlag(str, Enum):
    MODEL = "MODEL_DIRTY"
    MATH = "MATH_DIRTY"
    RESULT = "RESULT_DIRTY"
    PROSE = "PROSE_DIRTY"
    VISUAL = "VISUAL_DIRTY"
    CITATION = "CITATION_DIRTY"
    FORMAT = "FORMAT_DIRTY"


@dataclass(frozen=True)
class DirtyChange:
    flag: DirtyFlag
    owner_stage: int
    cause_artifact: str
    baseline_fingerprint: str
    current_fingerprint: str

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["flag"] = self.flag.value
        return value


_EXCLUDED_PARTS = {
    "archive",
    "__pycache__",
    "logs",
    "tmp",
    "releases",
    "staging",
    "audits",
    "judge_outputs",
    "judge_packets",
}
_TRACKED_ROOTS = {
    "paper",
    "problem",
    "models",
    "results",
    "figures",
    "tables",
    "scripts",
    "style",
    "data/raw",
    "data/final",
    "run_state/solver_jobs",
    "selection/decisions",
    ".factory/solver_receipts",
}
_TRACKED_TOP_SUFFIXES = {".md", ".tex", ".json", ".bib", ".csv", ".xlsx"}
_IGNORED_NAMES = {
    "checkpoint.md",
    "delivery_manifest.json",
    "human_review.md",
    "numbers_manifest.json",
    "status.json",
}
_MATH_RE = re.compile(
    r"\$\$.*?\$\$|(?<!\$)\$(?!\$).*?(?<!\$)\$(?!\$)|"
    r"\\\((?:.|\n)*?\\\)|\\\[(?:.|\n)*?\\\]|"
    r"\\begin\{(?:math|displaymath|equation\*?|align\*?|alignat\*?|flalign\*?|"
    r"gather\*?|multline\*?|eqnarray\*?)\}(?:.|\n)*?"
    r"\\end\{(?:math|displaymath|equation\*?|align\*?|alignat\*?|flalign\*?|"
    r"gather\*?|multline\*?|eqnarray\*?)\}",
    re.DOTALL,
)
_CITATION_RE = re.compile(r"\\(?:cite|citep|citet|autocite)\*?(?:\[[^]]*\])?\{[^}]+\}")
_LATEX_COMMAND_RE = re.compile(r"\\[A-Za-z@]+\*?(?:\[[^]]*\])?")
_MATH_DEFINITION_RE = re.compile(
    r"(?:\\(?:global|long|outer|protected)\s*)*\\(?P<command>"
    # LaTeX2e/xparse definitions are matched by definition family rather than
    # a command-name whitelist.  This intentionally includes new definition
    # families without requiring a classifier release for each package.
    r"(?:new|renew|provide|declare|define)[A-Za-z@]+|"
    # TeX primitives and aliases.
    r"(?:g|e|x)?def|let|"
    # expl3 variable/control-sequence constructors and setters.
    r"[A-Za-z]+_(?:new|set|gset|const|generate)(?::[A-Za-z]+)?|"
    # Macro-valued math helpers and counter definitions/assignments.
    r"pgfmath[A-Za-z@]*macro|(?:new|set|addto|counterwithin|numberwithin)[A-Za-z@]*counter"
    r")(?P<star>\*)?(?![A-Za-z@])",
    re.IGNORECASE,
)
_DEF_STYLE_RE = re.compile(
    r"^(?:(?:g|e|x)?def|let|"
    r"[A-Za-z]+_(?:new|set|gset|const|generate)(?::[A-Za-z]+)?)$",
    re.IGNORECASE,
)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_hash(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return _sha256_bytes(encoded)


def classifier_contract_sha256() -> str:
    source = Path(__file__).read_bytes()
    ownership = Path(__file__).with_name("artifact_ownership.py").read_bytes()
    return _sha256_bytes(
        DIRTY_CLASSIFIER_SCHEMA.encode("ascii")
        + b"\0"
        + source
        + b"\0"
        + ownership
    )


def _tracked(relative: str, path: Path) -> bool:
    parts = Path(relative).parts
    if any(part in _EXCLUDED_PARTS for part in parts):
        return False
    if path.name in _IGNORED_NAMES or path.name.endswith((".latest.txt", ".latest.json")):
        return False
    if len(parts) == 1:
        return path.suffix.lower() in _TRACKED_TOP_SUFFIXES
    return any(relative == root or relative.startswith(root + "/") for root in _TRACKED_ROOTS)


def tracked_artifact_paths(project_dir: str | Path) -> tuple[str, ...]:
    """List ordinary authored paths covered by dirty/finalization governance."""

    project = Path(project_dir).resolve()
    tracked: list[str] = []
    for path in sorted(project.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(project).as_posix()
        if _tracked(relative, path):
            tracked.append(relative)
    return tuple(tracked)


def _balanced_group_end(
    text: str, start: int, opening: str, closing: str
) -> int | None:
    if start >= len(text) or text[start] != opening:
        return None
    depth = 0
    for index in range(start, len(text)):
        character = text[index]
        if character == opening and (index == 0 or text[index - 1] != "\\"):
            depth += 1
        elif character == closing and (index == 0 or text[index - 1] != "\\"):
            depth -= 1
            if depth == 0:
                return index + 1
    return None


def _skip_space(text: str, position: int) -> int:
    while position < len(text) and text[position].isspace():
        position += 1
    return position


def _math_definition_chunks(text: str) -> list[str]:
    """Extract authored definitions that may alter rendered mathematics.

    Definitions are included even when their macro is not currently referenced.
    Proving non-use across TeX expansion is not reliable enough for a workflow
    skip decision, so an active definition change intentionally fails closed.
    """

    source = mask_inactive_latex(text)
    chunks: list[str] = []
    for match in _MATH_DEFINITION_RE.finditer(source):
        command = match.group("command")
        position = _skip_space(source, match.end())
        if _DEF_STYLE_RE.match(command):
            body_start = source.find("{", position)
            end = (
                _balanced_group_end(source, body_start, "{", "}")
                if body_start >= 0
                else None
            )
        else:
            if position < len(source) and source[position] == "{":
                position = _balanced_group_end(source, position, "{", "}") or position
            else:
                macro = re.match(r"\\[A-Za-z@]+", source[position:])
                if macro is None:
                    continue
                position += macro.end()
            position = _skip_space(source, position)
            end = position
            consumed_group = False
            # Consume the complete definition invocation, not merely its first
            # group.  xparse/environment definitions commonly place the value
            # in the third or fourth braced group.
            while position < len(source) and source[position] in "[{":
                opening = source[position]
                closing = "]" if opening == "[" else "}"
                group_end = _balanced_group_end(
                    source, position, opening, closing
                )
                if group_end is None:
                    break
                consumed_group = True
                end = group_end
                position = _skip_space(source, group_end)
            if not consumed_group:
                end = None
        if end is None:
            end = source.find("\n", match.end())
            if end < 0:
                end = len(source)
        chunks.append(re.sub(r"\s+", " ", source[match.start():end]).strip())
    return chunks


def _paper_semantics(text: str) -> dict[str, str]:
    math_chunks = _MATH_RE.findall(text)
    math_definitions = _math_definition_chunks(text)
    citations = _CITATION_RE.findall(text)
    without_math = _MATH_RE.sub(" ", text)
    without_citations = _CITATION_RE.sub(" ", without_math)
    prose = _LATEX_COMMAND_RE.sub(" ", without_citations)
    prose = re.sub(r"[{}%&_#~^\\]", " ", prose)
    prose = re.sub(r"\s+", " ", prose).strip()
    format_only = re.sub(r"\s+", " ", without_math).strip()
    return {
        "math": _canonical_hash(
            {"formulas": math_chunks, "definitions": math_definitions}
        ),
        "citation": _canonical_hash(citations),
        "prose": _sha256_bytes(prose.encode("utf-8", errors="replace")),
        "format": _sha256_bytes(format_only.encode("utf-8", errors="replace")),
    }


def capture_artifact_manifest(project_dir: str | Path) -> dict[str, str]:
    project = Path(project_dir).resolve()
    dependency_graph = resolve_latex_dependency_graph(project)
    paper_sources = {
        path.relative_to(project).as_posix()
        for path in dependency_graph.sources
    }
    paper_dependencies = {
        path.relative_to(project).as_posix()
        for path in dependency_graph.files
    }
    manifest: dict[str, str] = {}
    for path in sorted(project.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(project).as_posix()
        if not _tracked(relative, path):
            continue
        if (
            relative.startswith("paper/")
            and path.suffix.lower() in {".tex", ".bib"}
            and relative not in paper_dependencies
        ):
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        manifest[relative] = _sha256_bytes(data)
        if relative in paper_sources:
            semantics = _paper_semantics(data.decode("utf-8", errors="replace"))
            for domain, fingerprint in semantics.items():
                manifest[f"@paper:{relative}:{domain}"] = fingerprint
        if path.name in {"assumption_ledger.md", "audit_issue_ledger.md"}:
            for line in data.decode("utf-8", errors="replace").splitlines():
                if "PROTECTED" not in line.upper() or "|" not in line:
                    continue
                cells = [cell.strip().replace("`", "").replace("*", "") for cell in line.strip().strip("|").split("|")]
                issue_id = cells[0] if cells else ""
                if issue_id and not set(issue_id) <= {"-", ":"}:
                    manifest[f"@protected:{relative}:{issue_id}"] = _sha256_bytes(
                        issue_id.encode("utf-8")
                    )
    return manifest


def manifest_fingerprint(manifest: dict[str, str]) -> str:
    return _canonical_hash(manifest)


def _change(
    flag: DirtyFlag,
    owner_stage: int,
    artifact: str,
    before: dict[str, str],
    after: dict[str, str],
) -> DirtyChange:
    return DirtyChange(
        flag=flag,
        owner_stage=owner_stage,
        cause_artifact=artifact,
        baseline_fingerprint=before.get(artifact, "MISSING"),
        current_fingerprint=after.get(artifact, "MISSING"),
    )


def classify_manifest_changes(
    before: dict[str, str], after: dict[str, str]
) -> list[DirtyChange]:
    changed = sorted(
        path for path in set(before) | set(after) if before.get(path) != after.get(path)
    )
    changes: dict[tuple[DirtyFlag, str], DirtyChange] = {}
    paper_raw_changes: set[str] = set()

    def remember(change: DirtyChange) -> None:
        changes.setdefault((change.flag, change.cause_artifact), change)

    for artifact in changed:
        if artifact.startswith("@protected:"):
            remember(_change(DirtyFlag.MATH, 8, artifact, before, after))
            continue
        if artifact.startswith("@paper:"):
            _, relative, domain = artifact.split(":", 2)
            if domain == "math":
                remember(_change(DirtyFlag.MATH, 8, relative, before, after))
            elif domain == "citation":
                remember(_change(DirtyFlag.CITATION, 9, relative, before, after))
            elif domain == "prose":
                remember(_change(DirtyFlag.PROSE, 9, relative, before, after))
            elif domain == "format":
                remember(_change(DirtyFlag.FORMAT, 9, relative, before, after))
            continue
        if artifact.endswith(".tex") and any(
            f"@paper:{artifact}:{domain}" in before
            or f"@paper:{artifact}:{domain}" in after
            for domain in ("math", "citation", "prose", "format")
        ):
            paper_raw_changes.add(artifact)
            continue
        ownership = artifact_ownership(artifact)
        if ownership is not None:
            remember(
                _change(
                    DirtyFlag(ownership.dirty_flag),
                    ownership.owner_stage,
                    artifact,
                    before,
                    after,
                )
            )
        else:
            # Unknown authored changes fail closed. Both flags are intentional:
            # the upstream result owner must re-attest, and the math preflight
            # cannot be skipped merely because classification was uncertain.
            remember(_change(DirtyFlag.MATH, 8, artifact, before, after))
            remember(_change(DirtyFlag.RESULT, 4, artifact, before, after))

    for relative in paper_raw_changes:
        semantic_prefix = f"@paper:{relative}:"
        if not any(path.startswith(semantic_prefix) for path in changed):
            remember(_change(DirtyFlag.FORMAT, 9, relative, before, after))

    return list(changes.values())


def semantic_flags(flags: list[dict[str, Any]] | tuple[dict[str, Any], ...]) -> set[str]:
    return {
        str(item.get("flag"))
        for item in flags
        if str(item.get("flag"))
        in {DirtyFlag.MODEL.value, DirtyFlag.MATH.value, DirtyFlag.RESULT.value}
    }
