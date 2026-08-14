#!/usr/bin/env python3
"""Coarse-to-fine retrieval across curated and authorized HMML methods.

The first pass ranks hierarchy branches; the second ranks method leaves inside
the selected branches while retaining strong direct leaf matches. Every result
still resolves to a registered, on-disk method document.
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

QUERY_EXPANSIONS = {
    "优化": ("optimization", "programming"),
    "规划": ("programming",),
    "线性": ("linear",),
    "整数": ("integer", "discrete"),
    "混合整数": ("mixed integer programming", "MIP"),
    "非线性": ("nonlinear",),
    "凸优化": ("convex programming",),
    "二次规划": ("quadratic programming",),
    "多目标": ("multi-objective programming",),
    "动态规划": ("dynamic programming",),
    "资源分配": ("resource allocation",),
    "生产调度": ("production scheduling",),
    "调度": ("scheduling",),
    "物流": ("logistics",),
    "运输": ("transportation",),
    "图论": ("graph theory",),
    "最短路": ("shortest path",),
    "路径": ("path", "routing"),
    "旅行商": ("traveling salesman problem", "TSP"),
    "网络流": ("network flow",),
    "生成树": ("minimum spanning tree",),
    "预测": ("prediction", "forecasting"),
    "时间序列": ("time series",),
    "回归": ("regression",),
    "聚类": ("clustering",),
    "分类": ("classification",),
    "主成分": ("principal component analysis", "PCA"),
    "层次分析": ("analytic hierarchy process", "AHP"),
    "评价": ("evaluation",),
    "马尔可夫": ("Markov",),
    "排队": ("queuing theory",),
    "博弈": ("game theory",),
    "蒙特卡洛": ("Monte Carlo",),
    "微分方程": ("differential equation",),
    "偏微分": ("partial differential equation", "PDE"),
    "插值": ("interpolation",),
    "拟合": ("curve fitting",),
    "神经网络": ("neural network",),
}


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def default_index_paths(root: Path) -> list[Path]:
    paths = [root / "method_library" / "index.json"]
    hmml_index = root / "method_library" / "hmml" / "index.json"
    if hmml_index.is_file():
        paths.append(hmml_index)
    return paths


def load_entries(index_paths: list[Path]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    for index_path in index_paths:
        payload = json.loads(index_path.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            raise SystemExit(f"index must be a JSON list: {index_path}")
        for entry in payload:
            if not isinstance(entry, dict) or not entry.get("path"):
                raise SystemExit(f"index contains an invalid entry: {index_path}")
            path = str(entry["path"])
            if path in seen_paths:
                raise SystemExit(f"duplicate registered method path: {path}")
            seen_paths.add(path)
            entries.append(entry)
    return entries


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


def expand_query(query: str) -> str:
    additions: list[str] = []
    for term, translations in QUERY_EXPANSIONS.items():
        if term in query:
            additions.extend(translations)
    if not additions:
        return query
    return query + "\n" + " ".join(dict.fromkeys(additions))


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


def _hierarchy(entry: dict[str, Any]) -> list[str]:
    configured = entry.get("hierarchy")
    if isinstance(configured, list) and configured:
        return [str(item) for item in configured if str(item).strip()]
    return [
        str(item)
        for item in (entry.get("domain"), entry.get("subdomain"))
        if str(item or "").strip()
    ]


def _hit_label(value: str, maximum: int = 120) -> str:
    compact = " ".join(value.split())
    return compact if len(compact) <= maximum else compact[: maximum - 1] + "…"


def score_entry(
    entry: dict[str, Any], query: str, query_words: set[str]
) -> tuple[float, float, list[str]]:
    q_lower = query.lower()
    query_cjk = cjk_chars(query)
    leaf_score = 0.0
    hierarchy_score = 0.0
    hits: list[str] = []

    weighted_fields = [
        ("method", 4.0),
        ("name_zh", 4.0),
        ("description", 2.0),
    ]
    for field, weight in weighted_fields:
        value = str(entry.get(field, ""))
        delta = text_match_score(value, q_lower, query_words, query_cjk, weight)
        if delta:
            leaf_score += delta
            hits.append(_hit_label(value))

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
                leaf_score += delta
            if delta and len(hits) < 8:
                hits.append(_hit_label(text))

    for level, label in enumerate(_hierarchy(entry)):
        weight = max(0.8, 2.2 - level * 0.45)
        delta = text_match_score(label, q_lower, query_words, query_cjk, weight)
        hierarchy_score += delta
        if delta and len(hits) < 8:
            hits.append(_hit_label(label))
    for description in entry.get("hierarchy_descriptions", []) or []:
        hierarchy_score += text_match_score(
            str(description), q_lower, query_words, query_cjk, 0.6
        )

    return leaf_score, hierarchy_score, hits


def validate_paths(entries: list[dict[str, Any]], root: Path) -> None:
    missing = []
    for entry in entries:
        path = root / str(entry.get("path", ""))
        if not path.is_file():
            missing.append(str(entry.get("path", "")))
    if missing:
        raise SystemExit("index contains missing method paths:\n" + "\n".join(missing))


def rank_methods(entries: list[dict[str, Any]], query: str, top_k: int) -> list[dict[str, Any]]:
    query = expand_query(query)
    query_words = word_tokens(query)
    scored: list[dict[str, Any]] = []
    for entry in entries:
        leaf_score, hierarchy_score, hits = score_entry(entry, query, query_words)
        item = dict(entry)
        hierarchy = _hierarchy(entry)
        item["hierarchy"] = hierarchy
        item["hierarchy_path"] = " → ".join(hierarchy)
        item["leaf_score"] = round(leaf_score, 3)
        item["hierarchy_score"] = round(hierarchy_score, 3)
        item["matched_terms"] = hits
        scored.append(item)

    branch_scores: dict[str, float] = {}
    for item in scored:
        branch = item["hierarchy"][0] if item["hierarchy"] else "未分类"
        candidate = float(item["hierarchy_score"]) + 0.2 * float(item["leaf_score"])
        branch_scores[branch] = max(branch_scores.get(branch, 0.0), candidate)
    selected_branches = {
        branch
        for branch, score in sorted(
            branch_scores.items(), key=lambda pair: (-pair[1], pair[0])
        )[:4]
        if score > 0
    }
    direct_limit = max(12, top_k * 2 if top_k > 0 else 12)
    direct_paths = {
        item["path"]
        for item in sorted(
            scored,
            key=lambda item: (-float(item["leaf_score"]), str(item["method"])),
        )[:direct_limit]
        if float(item["leaf_score"]) > 0
    }

    ranked: list[dict[str, Any]] = []
    for item in scored:
        branch = item["hierarchy"][0] if item["hierarchy"] else "未分类"
        branch_selected = not selected_branches or branch in selected_branches
        if top_k != 0 and not branch_selected and item["path"] not in direct_paths:
            continue
        branch_score = branch_scores.get(branch, 0.0)
        item["branch"] = branch
        item["branch_score"] = round(branch_score, 3)
        item["branch_selected"] = branch_selected
        item["retrieval_stage"] = "hierarchy+leaf" if branch_selected else "direct-leaf-fallback"
        item["score"] = round(
            float(item["leaf_score"])
            + 0.35 * float(item["hierarchy_score"])
            + 0.15 * branch_score,
            3,
        )
        ranked.append(item)
    ranked.sort(
        key=lambda item: (
            -item["score"],
            -item["leaf_score"],
            str(item.get("domain") or ""),
            str(item["method"]),
        )
    )
    if top_k > 0:
        return ranked[:top_k]
    return ranked


def _markdown_cell(value: Any) -> str:
    return str(value).replace("|", "/").replace("\n", " ")


def print_markdown(ranked: list[dict[str, Any]]) -> None:
    print("# Method Retrieval Results")
    print()
    branches = []
    for item in ranked:
        branch = str(item.get("branch") or "未分类")
        if item.get("branch_selected") and branch not in branches:
            branches.append(branch)
    print(f"Selected hierarchy branches: {', '.join(branches) if branches else 'all'}")
    print()
    print("| Rank | Score | Method | Domain | Path | Matched terms | Hierarchy | Source |")
    print("|---:|---:|---|---|---|---|---|---|")
    for idx, item in enumerate(ranked, 1):
        terms = _markdown_cell(", ".join(item.get("matched_terms", [])[:6]) or "-")
        method_name = _markdown_cell(item.get("method") or "")
        display_name = _markdown_cell(item.get("name_zh") or method_name)
        method_cell = (
            f"{display_name} ({method_name})"
            if display_name != method_name
            else method_name
        )
        print(
            f"| {idx} | {item['score']:.3f} | {method_cell} | "
            f"{_markdown_cell(item['domain'])} / {_markdown_cell(item['subdomain'])} | "
            f"{_markdown_cell(item['path'])} | {terms} | "
            f"{_markdown_cell(item.get('hierarchy_path') or '-')} | "
            f"{_markdown_cell(item.get('source') or 'curated')} |"
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
        print("UNREGISTERED METHOD CITATIONS (not in the loaded method registries):")
        for fp, cited in offenders:
            print(f"  {cited}   <- {fp}")
        print(
            f"\n{len(offenders)} unregistered citation(s) across {checked} file(s). "
            "Agents may cite only methods registered in a loaded index — register the "
            "method (index entry + .md doc) or fix the path."
        )
        return 1
    print(f"OK: {total} method citation(s) across {checked} file(s), all registered.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--index",
        action="append",
        help="Registry JSON to load. May be repeated. Defaults to curated + HMML registries.",
    )
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
    index_paths = [Path(value) for value in args.index] if args.index else default_index_paths(root)
    entries = load_entries(index_paths)
    validate_paths(entries, root)

    if args.check_citations:
        return check_citations(entries, args.check_citations)

    if args.validate_only:
        print(
            f"OK: {len(entries)} registered methods across {len(index_paths)} registries"
        )
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
