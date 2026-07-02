from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


STEP1_HEADING = "## Step 1 modeling directions:"
WORD_RE = re.compile(r"[a-z0-9_+\-.]+", re.I)
CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def _read_text(path: Path) -> str:
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def _load_method_index(factory_root: Path) -> dict[str, dict[str, Any]]:
    index_path = factory_root / "method_library" / "index.json"
    try:
        entries = json.loads(index_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(entries, list):
        return {}
    return {
        str(entry.get("path", "")): entry
        for entry in entries
        if isinstance(entry, dict) and entry.get("path")
    }


def _parse_retrieval_table(text: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or "method_library/" not in stripped:
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if len(cells) < 5:
            continue
        try:
            rank = int(cells[0])
            score = float(cells[1])
        except ValueError:
            continue
        rows.append(
            {
                "rank": rank,
                "retrieval_score": score,
                "method_label": cells[2],
                "domain_label": cells[3],
                "path": cells[4],
                "matched_terms": [part.strip() for part in cells[5].split(",") if part.strip()] if len(cells) > 5 else [],
            }
        )
    rows.sort(key=lambda row: (row["rank"], -row["retrieval_score"]))
    return rows


def _slug(value: str, fallback: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9_.-]+", "-", value.strip().lower()).strip("-")
    return text or fallback


def _tokens(text: str) -> set[str]:
    return {match.group(0).lower() for match in WORD_RE.finditer(text)}


def _cjk_chars(text: str) -> set[str]:
    return set(CJK_RE.findall(text))


def _contains_concept(haystack: str, needle: str) -> bool:
    if not needle:
        return False
    haystack_lower = haystack.lower()
    needle_lower = needle.lower()
    if needle_lower in haystack_lower:
        return True
    needle_words = _tokens(needle)
    if needle_words and needle_words <= _tokens(haystack):
        return True
    needle_cjk = _cjk_chars(needle)
    if not needle_cjk:
        return False
    overlap = len(needle_cjk & _cjk_chars(haystack)) / max(len(needle_cjk), 1)
    return overlap >= 0.65


def _data_fit(required_data: list[Any], data_text: str) -> float:
    requirements = [str(item) for item in required_data if str(item).strip()]
    if not requirements:
        return 0.7
    hits = sum(1 for item in requirements if _contains_concept(data_text, item))
    return hits / len(requirements)


def _score_direction(entry: dict[str, Any], retrieval: dict[str, Any], data_text: str) -> tuple[int, int, int]:
    retrieval_score = float(retrieval.get("retrieval_score") or 0.0)
    applicable = len(entry.get("applicable_problem_types", []) or [])
    solver_count = len(entry.get("solver_stack", []) or [])
    failure_count = len(entry.get("failure_modes", []) or [])
    data_score = _data_fit(entry.get("required_data", []) or [], data_text)

    correctness = round(min(100, 55 + min(30, retrieval_score * 5) + min(8, applicable * 2)))
    feasibility = round(max(35, min(100, 58 + data_score * 32 + min(6, solver_count * 3) - min(10, failure_count * 3))))
    combined = round(correctness * 0.55 + feasibility * 0.45)
    return correctness, feasibility, combined


def _selected_direction_id(project_path: Path) -> str:
    text = _read_text(project_path / "human_review.md")
    match = re.search(r"^Selected direction id:\s*([A-Za-z0-9_.-]+)\s*$", text, re.M)
    return match.group(1) if match else ""


def build_modeling_directions(project_path: Path, factory_root: Path, limit: int = 3) -> dict[str, Any]:
    retrieval_path = project_path / "problem" / "method_retrieval.md"
    if not retrieval_path.is_file():
        return {
            "available": False,
            "message": "等待 Step 0 生成 problem/method_retrieval.md",
            "selected_direction_id": _selected_direction_id(project_path),
            "directions": [],
        }

    method_index = _load_method_index(factory_root)
    retrieval_rows = _parse_retrieval_table(_read_text(retrieval_path))
    if not method_index or not retrieval_rows:
        return {
            "available": False,
            "message": "暂无可排序的建模方向",
            "selected_direction_id": _selected_direction_id(project_path),
            "directions": [],
        }

    data_text = "\n".join(
        [
            _read_text(project_path / "problem" / "data_inventory.md"),
            _read_text(project_path / "problem" / "problem_brief.md"),
            _read_text(project_path / "problem" / "candidate_methods.md"),
        ]
    )

    seen_ids: set[str] = set()
    candidates: list[dict[str, Any]] = []
    for idx, retrieval in enumerate(retrieval_rows, 1):
        entry = method_index.get(str(retrieval.get("path", "")))
        if not entry:
            continue
        method = str(entry.get("method") or retrieval.get("method_label") or "")
        direction_id = _slug(method, f"direction-{idx}")
        if direction_id in seen_ids:
            continue
        seen_ids.add(direction_id)
        correctness, feasibility, combined = _score_direction(entry, retrieval, data_text)
        risks = [str(item) for item in (entry.get("failure_modes", []) or [])[:2]]
        required = [str(item) for item in (entry.get("required_data", []) or [])[:4]]
        solver_stack = [str(item) for item in (entry.get("solver_stack", []) or [])[:3]]
        title = str(entry.get("name_zh") or method or retrieval.get("method_label") or "建模方向")
        candidates.append(
            {
                "id": direction_id,
                "rank": 0,
                "title": title,
                "method": method,
                "domain": str(entry.get("domain") or ""),
                "subdomain": str(entry.get("subdomain") or ""),
                "method_path": str(entry.get("path") or retrieval.get("path") or ""),
                "correctness_score": correctness,
                "feasibility_score": feasibility,
                "combined_score": combined,
                "retrieval_score": retrieval.get("retrieval_score", 0),
                "matched_terms": retrieval.get("matched_terms", []),
                "required_data": required,
                "solver_stack": solver_stack,
                "risks": risks,
                "rationale": _direction_rationale(title, method, correctness, feasibility, required, solver_stack),
            }
        )

    candidates.sort(
        key=lambda item: (
            -int(item["combined_score"]),
            -int(item["correctness_score"]),
            -int(item["feasibility_score"]),
            str(item["id"]),
        )
    )
    directions = candidates[: max(1, min(limit, 3))]
    for rank, item in enumerate(directions, 1):
        item["rank"] = rank

    return {
        "available": bool(directions),
        "message": "" if directions else "暂无可排序的建模方向",
        "selected_direction_id": _selected_direction_id(project_path),
        "directions": directions,
    }


def _direction_rationale(
    title: str,
    method: str,
    correctness: int,
    feasibility: int,
    required_data: list[str],
    solver_stack: list[str],
) -> str:
    data_note = "、".join(required_data[:2]) if required_data else "题目结构化数据"
    solver_note = " / ".join(solver_stack[:2]) if solver_stack else "常规数值工具"
    return (
        f"{title}（{method}）在方法匹配和数据可得性上综合得分较高；"
        f"当前正确性潜力 {correctness}，可行性 {feasibility}。"
        f"主要依赖 {data_note}，可用 {solver_note} 落地。"
    )


def write_modeling_direction_selection(
    project_path: Path,
    direction: dict[str, Any],
    timestamp: str,
) -> None:
    human_review = project_path / "human_review.md"
    human_review.parent.mkdir(parents=True, exist_ok=True)
    existing = _read_text(human_review)
    section = _render_selection_section(direction, timestamp)

    pattern = re.compile(
        rf"^{re.escape(STEP1_HEADING)}\n.*?(?=^## |\Z)",
        re.M | re.S,
    )
    if pattern.search(existing):
        updated = pattern.sub(section, existing).rstrip() + "\n"
    else:
        prefix = existing.rstrip()
        if prefix:
            updated = f"{prefix}\n\n{section}"
        else:
            updated = f"# 人工审核与介入记录\n\n{section}"
    human_review.write_text(updated, encoding="utf-8")


def _render_selection_section(direction: dict[str, Any], timestamp: str) -> str:
    risks = direction.get("risks") or []
    risks_text = "；".join(str(item) for item in risks) if risks else "未识别硬阻塞"
    return (
        f"{STEP1_HEADING}\n\n"
        "STATUS: READY\n"
        f"Updated: {timestamp}\n"
        f"Selected direction id: {direction.get('id', '')}\n"
        f"Primary direction: {direction.get('title', '')} ({direction.get('method', '')})\n"
        f"Method path: {direction.get('method_path', '')}\n"
        f"Correctness score: {direction.get('correctness_score', '')}\n"
        f"Feasibility score: {direction.get('feasibility_score', '')}\n"
        f"Rationale: {direction.get('rationale', '')}\n"
        f"Known risks: {risks_text}\n\n"
        "请 Step 1 将该方向作为高优先级候选流写入 viable_streams.md；"
        "若数据、时间或算力约束使其不可行，必须在 research_brief.md 和 viability_gate.md 中说明原因，"
        "并保留至少一条不同方法族的备选流。\n"
    )
