#!/usr/bin/env python3
"""Rank registered Modeling Factory methods for a problem description.

This is a lightweight HMML-lite retriever: it reads method_library/index.json,
scores registered method nodes against a query, and prints a shortlist that
Step 0/1 agents can cite. It never invents method paths.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


WORD_RE = re.compile(r"[a-z0-9_+\-.]+", re.I)
CJK_RE = re.compile(r"[\u4e00-\u9fff]")
# A citation is any reference to a method document under method_library/.
# Used by --check-citations to enforce that agents cite only registered methods.
CITATION_RE = re.compile(r"method_library/[A-Za-z0-9_./-]+\.md")


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def load_query(args: argparse.Namespace) -> str:
    parts: list[str] = []
    for path in args.query_file or []:
        parts.append(Path(path).read_text(encoding="utf-8"))
    if args.query:
        parts.append(args.query)
    if not parts:
        data = sys.stdin.read()
        if data.strip():
            parts.append(data)
    if not parts:
        raise SystemExit("No query provided. Use --query, --query-file, or stdin.")
    return "\n".join(parts)


def word_tokens(text: str) -> set[str]:
    return {m.group(0).lower() for m in WORD_RE.finditer(text)}


def cjk_chars(text: str) -> set[str]:
    return set(CJK_RE.findall(text))


def text_match_score(text: str, q_lower: str, query_words: set[str], query_cjk: set[str], weight: float) -> float:
    text_lower = text.lower()
    if not text:
        return 0.0
    if text_lower in q_lower:
        return weight

    item_words = word_tokens(text)
    if item_words and item_words & query_words:
        return weight * min(len(item_words & query_words), 3) / 3

    item_cjk = cjk_chars(text)
    if item_cjk and query_cjk:
        overlap = len(item_cjk & query_cjk) / max(len(item_cjk), 1)
        if overlap >= 0.5:
            return weight * min(overlap, 1.0) * 0.75
    return 0.0


def score_entry(entry: dict[str, Any], query: str, query_words: set[str]) -> tuple[float, list[str]]:
    q_lower = query.lower()
    query_cjk = cjk_chars(query)
    score = 0.0
    hits: list[str] = []

    weighted_fields = [
        ("method", 4.0),
        ("name_zh", 4.0),
        ("domain", 1.5),
        ("subdomain", 1.5),
    ]
    for field, weight in weighted_fields:
        value = str(entry.get(field, ""))
        if value and value.lower() in q_lower:
            score += weight
            hits.append(value)

    for field, weight in [
        ("keywords", 3.0),
        ("applicable_problem_types", 2.0),
        ("required_data", 1.0),
        ("solver_stack", 0.8),
    ]:
        for item in entry.get(field, []):
            text = str(item)
            delta = text_match_score(text, q_lower, query_words, query_cjk, weight)
            if delta:
                score += delta
            if delta and len(hits) < 8:
                hits.append(text)

    return score, hits


def validate_paths(entries: list[dict[str, Any]], root: Path) -> None:
    missing = []
    for entry in entries:
        path = root / str(entry.get("path", ""))
        if not path.is_file():
            missing.append(str(entry.get("path", "")))
    if missing:
        raise SystemExit("index contains missing method paths:\n" + "\n".join(missing))


def rank_methods(entries: list[dict[str, Any]], query: str, top_k: int) -> list[dict[str, Any]]:
    query_words = word_tokens(query)
    ranked: list[dict[str, Any]] = []
    for entry in entries:
        score, hits = score_entry(entry, query, query_words)
        item = dict(entry)
        item["score"] = round(score, 3)
        item["matched_terms"] = hits
        ranked.append(item)
    ranked.sort(key=lambda item: (-item["score"], item["domain"], item["method"]))
    if top_k > 0:
        return ranked[:top_k]
    return ranked


def print_markdown(ranked: list[dict[str, Any]]) -> None:
    print("# Method Retrieval Results")
    print()
    print("| Rank | Score | Method | Domain | Path | Matched terms |")
    print("|---:|---:|---|---|---|---|")
    for idx, item in enumerate(ranked, 1):
        terms = ", ".join(item.get("matched_terms", [])[:6]) or "-"
        method = item.get("name_zh") or item.get("method")
        print(
            f"| {idx} | {item['score']:.3f} | {method} ({item['method']}) | "
            f"{item['domain']} / {item['subdomain']} | {item['path']} | {terms} |"
        )


def check_citations(entries: list[dict[str, Any]], files: list[str]) -> int:
    """Verify every method_library/<...>.md reference in `files` is registered.

    Enforces the HMML-lite rule that agents may cite only methods present in
    index.json. References to README.md are ignored (doc links, not methods).
    Returns 0 when every citation is registered, 1 otherwise (with a report).
    """
    registered = {str(entry.get("path", "")) for entry in entries}
    offenders: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    total = 0
    checked = 0
    for fp in files:
        path = Path(fp)
        if not path.is_file():
            print(f"WARNING: citation-check target not found, skipped: {fp}", file=sys.stderr)
            continue
        checked += 1
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in CITATION_RE.finditer(text):
            cited = match.group(0)
            if cited.endswith("/README.md"):
                continue
            total += 1
            key = (fp, cited)
            if cited not in registered and key not in seen:
                seen.add(key)
                offenders.append(key)

    if offenders:
        print("UNREGISTERED METHOD CITATIONS (not in method_library/index.json):")
        for fp, cited in offenders:
            print(f"  {cited}   <- {fp}")
        print(
            f"\n{len(offenders)} unregistered citation(s) across {checked} file(s). "
            "Agents may cite only methods registered in index.json — register the "
            "method (index entry + .md doc) or fix the path."
        )
        return 1
    print(f"OK: {total} method citation(s) across {checked} file(s), all registered.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", default=str(repo_root() / "method_library" / "index.json"))
    parser.add_argument("--query", help="Inline problem description or keywords.")
    parser.add_argument("--query-file", action="append", help="Problem markdown file to read. May be repeated.")
    parser.add_argument("--top-k", type=int, default=8, help="Number of methods to return. Use 0 for all.")
    parser.add_argument("--format", choices=["markdown", "json"], default="markdown")
    parser.add_argument("--validate-only", action="store_true", help="Only validate index schema and paths.")
    parser.add_argument(
        "--check-citations",
        action="append",
        metavar="FILE",
        help="Verify every method_library/<...>.md reference in FILE is registered. "
        "May be repeated. Exits 1 if any citation is unregistered.",
    )
    args = parser.parse_args()

    root = repo_root()
    index_path = Path(args.index)
    entries = json.loads(index_path.read_text(encoding="utf-8"))
    if not isinstance(entries, list):
        raise SystemExit("index must be a JSON list")
    validate_paths(entries, root)

    if args.check_citations:
        return check_citations(entries, args.check_citations)

    if args.validate_only:
        print(f"OK: {len(entries)} registered methods")
        return 0

    query = load_query(args)
    ranked = rank_methods(entries, query, args.top_k)
    if args.format == "json":
        print(json.dumps(ranked, ensure_ascii=False, indent=2))
    else:
        print_markdown(ranked)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
