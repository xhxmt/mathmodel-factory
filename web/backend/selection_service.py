from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any


STEP3_HEADING = "## Step 3 decision:"
VALID_VERDICT_RE = re.compile(r"^VERDICT:\s*(\S+)", re.M)
STREAM_RE = re.compile(r"m(\d+)")


class SelectionError(ValueError):
    pass


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else default
    except Exception:
        return default


def selection_config(project_path: Path) -> dict[str, Any]:
    data = _load_json(project_path / "selection" / "config.json", {})
    return data if isinstance(data, dict) else {}


def selection_enabled(project_path: Path, gate: str) -> bool:
    cfg = selection_config(project_path)
    if cfg.get("enabled") is not True:
        return False
    gates = cfg.get("gates", [])
    return gate in gates if isinstance(gates, list) else False


def timeout_minutes(project_path: Path) -> int:
    raw = selection_config(project_path).get("timeout_minutes", 30)
    try:
        value = int(raw)
    except Exception:
        value = 30
    return max(1, min(value, 24 * 60))


def _first_verdict(path: Path) -> str:
    match = VALID_VERDICT_RE.search(_read_text(path).replace("\r\n", "\n"))
    return match.group(1).strip() if match else ""


def _demo_status(path: Path) -> str:
    data = _load_json(path, {})
    if isinstance(data, dict):
        return str(data.get("status") or data.get("solver_status") or "").upper()
    return ""


def _method_family(spec_text: str) -> str:
    match = re.search(r"method_library/[^/\s]+/([^/\s]+)\.md", spec_text)
    if match:
        return match.group(1).replace("_", " ").upper()
    if "MILP" in spec_text.upper():
        return "MILP"
    if "PSO" in spec_text.upper():
        return "PSO"
    return "UNKNOWN"


def _stream_ids(project_path: Path) -> list[str]:
    ids = {
        match.group(0)
        for path in project_path.glob("m*_critique.md")
        for match in STREAM_RE.finditer(path.stem)
    }
    return sorted(ids, key=lambda item: int(item[1:]))


def _score_stream(project_path: Path, stream_id: str) -> dict[str, int]:
    spec_text = _read_text(project_path / f"{stream_id}_spec.md")
    critique_text = _read_text(project_path / f"{stream_id}_critique.md")
    demo_status = _demo_status(project_path / f"{stream_id}_demo_result.json")
    correctness = 90 if demo_status == "OPTIMAL" else 78 if demo_status in {"FEASIBLE", "SUCCESS"} else 65
    feasibility = 88 if demo_status in {"OPTIMAL", "FEASIBLE", "SUCCESS"} else 55
    coverage = min(100, 60 + 10 * len(set(re.findall(r"\bP[1-9]\b|问题[一二三四五六七八九十]", spec_text))))
    innovation = 70 + (8 if "robust" in spec_text.lower() or "鲁棒" in spec_text else 0)
    risk = 20 + 15 * len(re.findall(r"\bMAJOR\b|风险|warning", critique_text, re.I))
    return {
        "correctness": min(correctness, 100),
        "feasibility": min(feasibility, 100),
        "coverage": min(coverage, 100),
        "innovation": min(innovation, 100),
        "risk": min(risk, 100),
        "differentiation": 50,
    }


def _tradeoffs(project_path: Path, stream_id: str) -> list[str]:
    text = _read_text(project_path / f"{stream_id}_critique.md")
    rows = [line.strip("- ").strip() for line in text.splitlines() if re.search(r"风险|warning|MAJOR", line, re.I)]
    return rows[:3] or ["No blocking critique issue recorded."]


def _build_option(project_path: Path, stream_id: str) -> dict[str, Any] | None:
    if _first_verdict(project_path / f"{stream_id}_critique.md") != "VALIDATED":
        return None
    spec_text = _read_text(project_path / f"{stream_id}_spec.md")
    family = _method_family(spec_text)
    scores = _score_stream(project_path, stream_id)
    composite = (
        scores["correctness"] * 10_000
        + scores["feasibility"] * 100
        + scores["coverage"]
        - scores["risk"]
    )
    return {
        "id": stream_id,
        "rank": 0,
        "title": f"{stream_id} - {family}",
        "family": family,
        "validated": True,
        "scores": scores,
        "composite_score": composite,
        "summary": f"{stream_id} uses {family} as a validated modeling stream.",
        "why_high_ranked": [
            f"Demo status: {_demo_status(project_path / f'{stream_id}_demo_result.json') or 'unknown'}.",
            "Critique verdict: VALIDATED.",
        ],
        "main_tradeoffs": _tradeoffs(project_path, stream_id),
        "subproblem_mapping": {},
        "evidence_files": [
            f"{stream_id}_spec.md",
            f"{stream_id}_critique.md",
            f"{stream_id}_demo_result.json",
        ],
        "aux_compatibility": [],
        "recommended_aux": "",
        "selection_payload": {"primary": stream_id, "auxiliary": "NONE"},
    }


def _rank_options(options: list[dict[str, Any]]) -> list[dict[str, Any]]:
    options.sort(
        key=lambda item: (
            -item["scores"]["correctness"],
            -item["scores"]["feasibility"],
            -item["scores"]["coverage"],
            -item["scores"]["innovation"],
            item["scores"]["risk"],
            item["id"],
        )
    )
    for rank, item in enumerate(options, 1):
        item["rank"] = rank
    for item in options:
        item["aux_compatibility"] = [
            other["id"] for other in options if other["id"] != item["id"] and other["family"] != item["family"]
        ]
        item["recommended_aux"] = item["aux_compatibility"][0] if item["aux_compatibility"] else "NONE"
        item["selection_payload"]["auxiliary"] = item["recommended_aux"]
    return options


def build_step3_options(project_path: Path, *, now_epoch: int | None = None) -> dict[str, Any]:
    now = int(time.time()) if now_epoch is None else int(now_epoch)
    options = [
        option
        for stream_id in _stream_ids(project_path)
        if (option := _build_option(project_path, stream_id)) is not None
    ]
    options = _rank_options(options)
    payload = {
        "schema_version": "1.0",
        "gate": "step3",
        "available": bool(options),
        "message": "" if options else "No VALIDATED Step 2 streams are available.",
        "created_epoch": now,
        "deadline_epoch": now + timeout_minutes(project_path) * 60,
        "default_option_id": options[0]["id"] if options else "",
        "default_aux_id": options[0]["recommended_aux"] if options else "NONE",
        "ranking_policy": "correctness_feasibility_first",
        "options": options,
    }
    _write_json_atomic(project_path / "selection" / "step3_options.json", payload)
    _write_text_atomic(project_path / "selection" / "step3_request.md", render_step3_request(payload))
    return payload


def render_step3_request(payload: dict[str, Any]) -> str:
    lines = ["# Step 3 方法主线选择", "", f"Default: `{payload.get('default_option_id', '')}`", ""]
    for item in payload.get("options", []):
        lines.extend(
            [
                f"## #{item['rank']} {item['title']}",
                f"- 正确性: {item['scores']['correctness']}",
                f"- 可行性: {item['scores']['feasibility']}",
                f"- 推荐 AUX: {item.get('recommended_aux') or 'NONE'}",
                f"- 摘要: {item.get('summary', '')}",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def read_selection_request(project_path: Path, gate: str = "step3") -> dict[str, Any]:
    options = _load_json(project_path / "selection" / f"{gate}_options.json", {})
    decision = _load_json(project_path / "selection" / f"{gate}_decision.json", None)
    if not isinstance(options, dict):
        options = {}
    options["decision"] = decision if isinstance(decision, dict) else None
    options["selected_option_id"] = options["decision"].get("selected_option_id", "") if options["decision"] else ""
    return options


def write_selection_decision(
    project_path: Path,
    *,
    gate: str,
    selected_option_id: str,
    selected_aux_id: str = "",
    source: str,
    reason: str,
    now_epoch: int | None = None,
) -> dict[str, Any]:
    if gate != "step3":
        raise SelectionError(f"Unsupported selection gate: {gate}")
    payload = read_selection_request(project_path, gate)
    options = payload.get("options") or []
    option_ids = {str(item.get("id")) for item in options}
    if selected_option_id not in option_ids:
        raise SelectionError(f"Unknown option id: {selected_option_id}")
    if selected_aux_id and selected_aux_id != "NONE" and selected_aux_id not in option_ids:
        raise SelectionError(f"Unknown auxiliary id: {selected_aux_id}")
    selected = next(item for item in options if item.get("id") == selected_option_id)
    aux = selected_aux_id or selected.get("recommended_aux") or "NONE"
    now = int(time.time()) if now_epoch is None else int(now_epoch)
    decision = {
        "schema_version": "1.0",
        "gate": gate,
        "selected_option_id": selected_option_id,
        "selected_aux_id": aux,
        "source": source,
        "decided_epoch": now,
        "reason": reason,
        "mirrored_to_human_review": True,
    }
    _write_json_atomic(project_path / "selection" / "step3_decision.json", decision)
    mirror_step3_decision_to_human_review(project_path, selected, aux, decision)
    return decision


def mirror_step3_decision_to_human_review(
    project_path: Path,
    selected_option: dict[str, Any],
    selected_aux_id: str,
    decision: dict[str, Any],
) -> None:
    human_review = project_path / "human_review.md"
    existing = _read_text(human_review)
    section = (
        f"{STEP3_HEADING}\n\n"
        "STATUS: READY\n"
        f"SOURCE: {decision.get('source', '')}\n"
        f"PRIMARY: {selected_option.get('id', '')}\n"
        f"AUXILIARY: {selected_aux_id or 'NONE'}\n"
        f"Reason: {decision.get('reason', '')}\n"
        f"Selected title: {selected_option.get('title', '')}\n"
        f"Family: {selected_option.get('family', '')}\n"
    )
    pattern = re.compile(rf"^{re.escape(STEP3_HEADING)}\n.*?(?=^## |\Z)", re.M | re.S)
    if pattern.search(existing):
        updated = pattern.sub(section, existing).rstrip() + "\n"
    else:
        prefix = existing.rstrip()
        updated = f"{prefix}\n\n{section}" if prefix else f"# 人工审核与介入记录\n\n{section}"
    _write_text_atomic(human_review, updated)
