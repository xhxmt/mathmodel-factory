#!/usr/bin/env python3
"""Generate the local HMML registry and method documents from upstream data.

The raw HMML.json/HMML.md files are vendored unchanged. This importer creates
deterministic, individually citable method documents plus a second registry;
it never rewrites the curated method_library/index.json registry.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import unicodedata
from pathlib import Path
from typing import Any, Iterator


SOURCE_REPOSITORY = "https://github.com/usail-hkust/LLM-MM-Agent"
SOURCE_COMMIT = "8abc1300e378eb40fe85b1ffcba6820c1358610a"
TAG_RE = re.compile(r"<(modeling_method|core_idea|application)>:\s*", re.I)


def _clean_label(value: str) -> str:
    return re.sub(r"\s*[:：]\s*$", "", value.strip())


def _slug(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", normalized.lower()).strip("-")
    if slug:
        return slug[:96].rstrip("-")
    return "method-" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _parse_description(description: str) -> dict[str, str]:
    matches = list(TAG_RE.finditer(description))
    if not matches:
        return {"description": description.strip()}
    fields: dict[str, str] = {}
    prefix = description[: matches[0].start()].strip()
    if prefix:
        fields["description"] = prefix
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(description)
        fields[match.group(1).lower()] = description[match.end() : end].strip()
    return fields


def _walk(
    nodes: list[dict[str, Any]],
    hierarchy: tuple[str, ...] = (),
    hierarchy_descriptions: tuple[str, ...] = (),
) -> Iterator[dict[str, Any]]:
    for node in nodes:
        if not isinstance(node, dict):
            continue
        method = node.get("method")
        if isinstance(method, str) and method.strip():
            yield {
                "method": method.strip(),
                "description": str(node.get("description") or "").strip(),
                "hierarchy": list(hierarchy),
                "hierarchy_descriptions": list(hierarchy_descriptions),
            }
            continue
        label = _clean_label(str(node.get("method_class") or ""))
        if not label:
            continue
        children = node.get("children")
        if not isinstance(children, list):
            continue
        yield from _walk(
            children,
            (*hierarchy, label),
            (*hierarchy_descriptions, str(node.get("description") or "").strip()),
        )


def _method_doc(entry: dict[str, Any], fields: dict[str, str]) -> str:
    hierarchy = " → ".join(entry["hierarchy"])
    return "\n".join(
        [
            f"# {entry['method']}",
            "",
            "> 来源：LLM-MM-Agent HMML；本文件由 `scripts/import_hmml.py` 确定性生成。",
            "",
            "## 分层位置",
            "",
            hierarchy,
            "",
            "## 建模方法",
            "",
            fields.get("modeling_method") or fields.get("description") or entry["description"],
            "",
            "## 核心思想",
            "",
            fields.get("core_idea") or "上游 HMML 未单独标注。",
            "",
            "## 典型应用",
            "",
            fields.get("application") or "上游 HMML 未单独标注。",
            "",
            "## 使用边界",
            "",
            "该条目来自广覆盖方法目录，仅用于候选召回；实际采用前必须结合题目数据、假设、求解器与失败模式做二次审查。",
            "",
        ]
    )


def import_hmml(source_json: Path, output_root: Path) -> list[dict[str, Any]]:
    tree = json.loads(source_json.read_text(encoding="utf-8"))
    if not isinstance(tree, list):
        raise ValueError("HMML source must be a JSON list")
    methods = list(_walk(tree))
    methods_dir = output_root / "methods"
    methods_dir.mkdir(parents=True, exist_ok=True)
    entries: list[dict[str, Any]] = []
    used_slugs: set[str] = set()
    for ordinal, method in enumerate(methods, 1):
        slug = _slug(method["method"])
        if slug in used_slugs:
            slug = f"{slug}-{ordinal}"
        used_slugs.add(slug)
        fields = _parse_description(method["description"])
        relative = f"method_library/hmml/methods/{slug}.md"
        (methods_dir / f"{slug}.md").write_text(_method_doc(method, fields), encoding="utf-8")
        hierarchy = method["hierarchy"]
        entries.append(
            {
                "domain": hierarchy[0] if hierarchy else "HMML",
                "subdomain": " / ".join(hierarchy[1:]),
                "method": method["method"],
                "name_zh": "",
                "path": relative,
                "keywords": hierarchy,
                "applicable_problem_types": [fields["application"]] if fields.get("application") else [],
                "required_data": [],
                "solver_stack": [],
                "failure_modes": ["广覆盖 HMML 条目未提供方法专属失败模式，采用前必须补充审查"],
                "hierarchy": hierarchy,
                "hierarchy_descriptions": method["hierarchy_descriptions"],
                "description": method["description"],
                "source": "LLM-MM-Agent HMML",
                "source_repository": SOURCE_REPOSITORY,
                "source_commit": SOURCE_COMMIT,
                "source_ordinal": ordinal,
            }
        )
    (output_root / "index.json").write_text(
        json.dumps(entries, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return entries


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-json", type=Path, required=True)
    parser.add_argument("--source-markdown", type=Path)
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "method_library" / "hmml",
    )
    args = parser.parse_args()
    entries = import_hmml(args.source_json, args.output_root)
    provenance = {
        "schema_version": "hmml-source-lock-v1",
        "source_repository": SOURCE_REPOSITORY,
        "source_commit": SOURCE_COMMIT,
        "source_files": {
            "HMML.json": _sha256(args.source_json),
        },
        "generated_method_count": len(entries),
        "authorization_note": (
            "Imported at the repository owner's direction after they confirmed "
            "authorization to copy the complete HMML data set."
        ),
    }
    if args.source_markdown:
        provenance["source_files"]["HMML.md"] = _sha256(args.source_markdown)
    (args.output_root / "SOURCE.json").write_text(
        json.dumps(provenance, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Generated {len(entries)} HMML method entries in {args.output_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
