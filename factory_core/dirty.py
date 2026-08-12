from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Any


DIRTY_CLASSIFIER_SCHEMA = "factory-dirty-classifier-v1"


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
    "problem",
    "models",
    "results",
    "figures",
    "tables",
    "data/final",
    "run_state/solver_jobs",
    "selection/decisions",
    ".factory/solver_receipts",
}
_TRACKED_TOP_SUFFIXES = {".md", ".tex", ".json", ".bib", ".csv", ".xlsx"}
_IGNORED_NAMES = {
    "checkpoint.md",
    "delivery_manifest.json",
    "numbers_manifest.json",
    "status.json",
}
_MATH_RE = re.compile(
    r"\$\$.*?\$\$|(?<!\$)\$(?!\$).*?(?<!\$)\$(?!\$)|"
    r"\\\[(?:.|\n)*?\\\]|\\begin\{(?:equation\*?|align\*?|gather\*?|multline\*?)\}"
    r"(?:.|\n)*?\\end\{(?:equation\*?|align\*?|gather\*?|multline\*?)\}",
    re.DOTALL,
)
_CITATION_RE = re.compile(r"\\(?:cite|citep|citet|autocite)\*?(?:\[[^]]*\])?\{[^}]+\}")
_LATEX_COMMAND_RE = re.compile(r"\\[A-Za-z@]+\*?(?:\[[^]]*\])?")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_hash(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return _sha256_bytes(encoded)


def classifier_contract_sha256() -> str:
    source = Path(__file__).read_bytes()
    return _sha256_bytes(DIRTY_CLASSIFIER_SCHEMA.encode("ascii") + b"\0" + source)


def _tracked(relative: str, path: Path) -> bool:
    parts = Path(relative).parts
    if any(part in _EXCLUDED_PARTS for part in parts):
        return False
    if path.name in _IGNORED_NAMES or path.name.endswith((".latest.txt", ".latest.json")):
        return False
    if len(parts) == 1:
        return path.suffix.lower() in _TRACKED_TOP_SUFFIXES
    return any(relative == root or relative.startswith(root + "/") for root in _TRACKED_ROOTS)


def _paper_semantics(text: str) -> dict[str, str]:
    math_chunks = _MATH_RE.findall(text)
    citations = _CITATION_RE.findall(text)
    without_math = _MATH_RE.sub(" ", text)
    without_citations = _CITATION_RE.sub(" ", without_math)
    prose = _LATEX_COMMAND_RE.sub(" ", without_citations)
    prose = re.sub(r"[{}%&_#~^\\]", " ", prose)
    prose = re.sub(r"\s+", " ", prose).strip()
    format_only = re.sub(r"\s+", " ", without_math).strip()
    return {
        "math": _canonical_hash(math_chunks),
        "citation": _canonical_hash(citations),
        "prose": _sha256_bytes(prose.encode("utf-8", errors="replace")),
        "format": _sha256_bytes(format_only.encode("utf-8", errors="replace")),
    }


def capture_artifact_manifest(project_dir: str | Path) -> dict[str, str]:
    project = Path(project_dir).resolve()
    manifest: dict[str, str] = {}
    for path in sorted(project.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(project).as_posix()
        if not _tracked(relative, path):
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        manifest[relative] = _sha256_bytes(data)
        if path.suffix.lower() == ".tex" and (
            path.name.endswith("_paper.tex") or relative == "paper/paper.tex"
        ):
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
    changes: dict[DirtyFlag, DirtyChange] = {}
    paper_raw_changes: set[str] = set()

    def remember(change: DirtyChange) -> None:
        changes.setdefault(change.flag, change)

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
        if artifact.endswith("_paper.tex") or artifact == "paper/paper.tex":
            paper_raw_changes.add(artifact)
            continue
        lowered = artifact.lower()
        if lowered.startswith("models/") or lowered in {
            "model.md",
            "quality_contract.json",
            "symbol_table.md",
            "assumption_ledger.md",
            "modeling_scope_gate.md",
            "claim_registry.json",
        }:
            remember(_change(DirtyFlag.MODEL, 3, artifact, before, after))
        elif (
            (
                lowered.startswith("results/")
                and (
                    Path(lowered).name
                    in {"canonical_results.json", "values.json", "invariants.json"}
                    or any(
                        token in lowered
                        for token in (
                            "provenance",
                            "source_mapping",
                            "adopted_objective",
                            "decision_variable",
                            "solver_evidence",
                        )
                    )
                )
            )
            or lowered.startswith("run_state/solver_jobs/")
            or lowered.startswith(".factory/solver_receipts/")
            or lowered == "solve_log.md"
        ):
            remember(_change(DirtyFlag.RESULT, 4, artifact, before, after))
        elif lowered.startswith("results/"):
            remember(_change(DirtyFlag.FORMAT, 9, artifact, before, after))
        elif lowered.startswith("figures/") or lowered == "visualization_log.md":
            remember(_change(DirtyFlag.VISUAL, 6, artifact, before, after))
        elif lowered.endswith(".bib") or "citation" in lowered:
            remember(_change(DirtyFlag.CITATION, 9, artifact, before, after))
        elif lowered.startswith("tables/") or lowered.startswith("style/"):
            remember(_change(DirtyFlag.FORMAT, 9, artifact, before, after))
        elif lowered.endswith(".md"):
            remember(_change(DirtyFlag.PROSE, 9, artifact, before, after))
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
