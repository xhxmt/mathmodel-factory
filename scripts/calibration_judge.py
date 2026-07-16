#!/usr/bin/env python3
"""Blind pairwise and split-axis calibration for awarded-paper PDFs.

This evaluator is deliberately separate from evaluation/run_evaluation.sh:
awarded papers are usually standalone PDFs and do not have a Modeling Factory
project directory, logs, or canonical result files.  It therefore judges only
claims visible in the paper, keeps model correctness separate from writing
quality, and performs anonymous pairwise comparisons before absolute scoring.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import re
import statistics
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.llm_judge_call import call as call_judge
from scripts.proxy_calibration import apply_perturbation


PROMPT_DIR = ROOT / "evaluation" / "prompts"
WRITING_DIMENSIONS = (
    "answer_completeness",
    "argument_chain",
    "result_traceability",
    "section_organization",
    "figure_narrative",
    "language_maturity",
)
WINNERS = {"A", "B", "TIE"}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _truncate(text: str, limit: int) -> str:
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text
    marker = "\n\n... [middle omitted for calibration context budget] ...\n\n"
    marker_bytes = marker.encode("utf-8")
    available = max(0, limit - len(marker_bytes))
    head = data[: available * 3 // 4].decode("utf-8", errors="ignore")
    tail = data[-available // 4 :].decode("utf-8", errors="ignore")
    return head + marker + tail


def extract_paper_text(item: dict[str, Any], root: Path, limit: int = 120_000) -> str:
    paper = root / str(item.get("paper_path") or "")
    if not paper.is_file():
        raise FileNotFoundError(f"paper not found: {paper}")
    if paper.suffix.lower() == ".pdf":
        proc = subprocess.run(
            ["pdftotext", "-layout", str(paper), "-"],
            capture_output=True,
            text=True,
            timeout=90,
        )
        text = proc.stdout if proc.returncode == 0 else ""
    elif paper.suffix.lower() in {".md", ".txt", ".tex"}:
        text = paper.read_text(encoding="utf-8", errors="replace")
    else:
        text = ""
    if len(text.strip()) < 1000:
        ocr_path = item.get("ocr_path")
        ocr = root / str(ocr_path or "")
        if ocr_path and ocr.is_file():
            text = ocr.read_text(encoding="utf-8", errors="replace")
    if len(text.strip()) < 1000:
        raise ValueError(f"paper text unavailable or too short: {paper}")
    text = _truncate(anonymize_text(text, item), limit)
    text = apply_perturbation(text, item.get("proxy_perturbation"))
    return _truncate(text, limit)


def anonymize_text(text: str, item: dict[str, Any]) -> str:
    """Remove common identity/award leakage before sending text to a judge."""
    redactions = list(item.get("redactions") or [])
    redactions.extend(
        [
            str(item.get("id") or ""),
            str(item.get("award_tier") or ""),
            str(item.get("category") or ""),
        ]
    )
    for value in sorted({value for value in redactions if len(value.strip()) >= 3}, key=len, reverse=True):
        text = re.sub(re.escape(value), "[已匿名]", text, flags=re.I)
    text = re.sub(r"(?:参赛)?队号\s*[：:]?\s*[A-Za-z0-9_-]{4,}", "队号：[已匿名]", text)
    text = re.sub(r"(?:一等奖|二等奖|三等奖|国一|国二|省一|省二|省三|优秀论文)", "[奖项已隐藏]", text)
    return text


def parse_json_output(text: str) -> dict[str, Any]:
    stripped = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", stripped, re.S)
    candidate = fenced.group(1) if fenced else stripped
    if not candidate.startswith("{"):
        start, end = candidate.find("{"), candidate.rfind("}")
        if start >= 0 and end > start:
            candidate = candidate[start : end + 1]
    data = json.loads(candidate)
    if not isinstance(data, dict):
        raise ValueError("judge output must be a JSON object")
    return data


def validate_pairwise(data: dict[str, Any]) -> dict[str, Any]:
    for key in ("overall_winner", "correctness_winner", "writing_winner"):
        if data.get(key) not in WINNERS:
            raise ValueError(f"invalid {key}: {data.get(key)!r}")
    confidence = data.get("confidence")
    if not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
        raise ValueError("confidence must be between 0 and 1")
    for key in ("fatal_flaw_a", "fatal_flaw_b"):
        if not isinstance(data.get(key), bool):
            raise ValueError(f"{key} must be boolean")
    for key in ("fatal_evidence_a", "fatal_evidence_b"):
        value = data.get(key)
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ValueError(f"{key} must be a string list")
    return data


def validate_absolute(data: dict[str, Any]) -> dict[str, Any]:
    correctness = data.get("correctness")
    writing = data.get("writing")
    if not isinstance(correctness, dict) or not isinstance(writing, dict):
        raise ValueError("absolute output needs correctness and writing objects")
    score = correctness.get("score")
    fatal = correctness.get("fatal_flaws")
    if not isinstance(score, (int, float)) or not 0 <= score <= 100:
        raise ValueError("correctness.score must be 0..100")
    if not isinstance(fatal, int) or fatal < 0:
        raise ValueError("correctness.fatal_flaws must be a non-negative integer")
    total = writing.get("score")
    dims = writing.get("dimensions")
    if not isinstance(total, (int, float)) or not 0 <= total <= 100:
        raise ValueError("writing.score must be 0..100")
    if not isinstance(dims, dict):
        raise ValueError("writing.dimensions must be an object")
    for name in WRITING_DIMENSIONS:
        value = dims.get(name)
        if not isinstance(value, (int, float)) or not 0 <= value <= 100:
            raise ValueError(f"writing dimension {name} must be 0..100")
    return data


def _render_prompt(name: str, replacements: dict[str, str]) -> str:
    text = (PROMPT_DIR / name).read_text(encoding="utf-8")
    for key, value in replacements.items():
        text = text.replace(f"__{key}__", value)
    return text


def _anonymous_order(pair: dict[str, Any], sample: int) -> tuple[str, str]:
    higher, lower = str(pair["higher"]), str(pair["lower"])
    return (higher, lower) if sample % 2 == 1 else (lower, higher)


def comparison_dossier(text_a: str, text_b: str, limit: int = 8000) -> str:
    """Expose small but decisive changes that are easy to miss in long papers."""
    lines_a = text_a.splitlines()
    lines_b = text_b.splitlines()
    matcher = difflib.SequenceMatcher(a=lines_a, b=lines_b, autojunk=False)
    if matcher.quick_ratio() < 0.72:
        return "两篇论文整体差异较大；差异线索未展开，必须按题目逐问独立比较。"
    chunks: list[str] = []
    for tag, a1, a2, b1, b2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        before_a = "\n".join(lines_a[max(0, a1 - 2) : min(len(lines_a), a2 + 2)])
        before_b = "\n".join(lines_b[max(0, b1 - 2) : min(len(lines_b), b2 + 2)])
        chunks.append(f"差异类型={tag}\n[A片段]\n{before_a}\n[B片段]\n{before_b}")
        if sum(len(chunk.encode("utf-8")) for chunk in chunks) >= limit:
            break
    if not chunks:
        return "机器逐行比较未发现文本差异；仍需独立核验公式、图表和结论。"
    return _truncate("\n\n".join(chunks), limit)


def _unblind(winner: str, a_id: str, b_id: str) -> str:
    if winner == "A":
        return a_id
    if winner == "B":
        return b_id
    return "TIE"


def _median(values: list[float]) -> float | None:
    return round(statistics.median(values), 2) if values else None


def _derive_overall(correctness: str, writing: str, reported: str) -> str:
    """Do not let an unexplained overall tie erase a clear axis-level loss."""
    if correctness == writing:
        return correctness
    if correctness == "TIE" and writing != "TIE":
        return writing
    if writing == "TIE" and correctness != "TIE":
        return correctness
    if correctness != "TIE" and writing != "TIE" and correctness != writing:
        return correctness
    return reported


def _majority(runs: list[dict[str, Any]], key: str) -> str | None:
    counts: dict[str, int] = {}
    for run in runs:
        winner = str(run[key])
        counts[winner] = counts.get(winner, 0) + 1
    if not counts:
        return None
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    if len(ordered) > 1 and ordered[0][1] == ordered[1][1]:
        return "TIE"
    return ordered[0][0]


def _needs_adjudication(
    runs: list[dict[str, Any]], *, malformed: int, has_comparison_differences: bool
) -> bool:
    if malformed or has_comparison_differences or not runs:
        return True
    winners = {str(run["overall_winner"]) for run in runs}
    fatal_states = {(bool(run["fatal_flaw_a"]), bool(run["fatal_flaw_b"])) for run in runs}
    return len(winners) > 1 or len(fatal_states) > 1 or _majority(runs, "overall_winner") == "TIE"


def _pair_judgment(
    *,
    prompt: str,
    model: str,
    timeout: int,
    a_id: str,
    b_id: str,
    sample: int | str,
    role: str,
) -> dict[str, Any]:
    parsed = validate_pairwise(parse_json_output(call_judge(prompt, model, timeout, 5000)))
    fatal_a = bool(parsed["fatal_flaw_a"])
    fatal_b = bool(parsed["fatal_flaw_b"])
    overall = _unblind(parsed["overall_winner"], a_id, b_id)
    correctness = _unblind(parsed["correctness_winner"], a_id, b_id)
    writing = _unblind(parsed["writing_winner"], a_id, b_id)
    fatal_override = False
    if fatal_a != fatal_b:
        nonfatal = b_id if fatal_a else a_id
        overall = nonfatal
        correctness = nonfatal
        fatal_override = True
    else:
        overall = _derive_overall(correctness, writing, overall)
    return {
        "sample": sample,
        "role": role,
        "model": model,
        "a_id": a_id,
        "b_id": b_id,
        "overall_winner": overall,
        "correctness_winner": correctness,
        "writing_winner": writing,
        "confidence": parsed["confidence"],
        "fatal_flaw_a": fatal_a,
        "fatal_flaw_b": fatal_b,
        "fatal_evidence_a": parsed["fatal_evidence_a"],
        "fatal_evidence_b": parsed["fatal_evidence_b"],
        "fatal_override": fatal_override,
        "reasons": parsed.get("reasons", {}),
    }


def run_pair(
    pair: dict[str, Any],
    papers: dict[str, dict[str, Any]],
    root: Path,
    *,
    model: str,
    samples: int,
    timeout: int,
    text_limit: int = 80_000,
    adjudicator_model: str | None = None,
) -> dict[str, Any]:
    text_cache: dict[str, str] = {}
    runs: list[dict[str, Any]] = []
    malformed = 0
    for sample in range(1, samples + 1):
        a_id, b_id = _anonymous_order(pair, sample)
        for paper_id in (a_id, b_id):
            if paper_id not in text_cache:
                text_cache[paper_id] = extract_paper_text(papers[paper_id], root, text_limit)
        dossier = comparison_dossier(text_cache[a_id], text_cache[b_id])
        prompt = _render_prompt(
            "calibration_pairwise.txt",
            {
                "PROBLEM_ID": str(papers[a_id].get("problem_id") or "UNKNOWN"),
                "COMPARISON_DOSSIER": dossier,
                "PAPER_A": text_cache[a_id],
                "PAPER_B": text_cache[b_id],
            },
        )
        try:
            runs.append(_pair_judgment(
                prompt=prompt, model=model, timeout=timeout, a_id=a_id, b_id=b_id,
                sample=sample, role="primary",
            ))
        except (OSError, ValueError, json.JSONDecodeError, RuntimeError) as exc:
            malformed += 1
            runs.append({"sample": sample, "status": "MALFORMED", "error": str(exc)})

    valid = [run for run in runs if run.get("status") != "MALFORMED"]
    reference_dossier = comparison_dossier(text_cache[str(pair["higher"])], text_cache[str(pair["lower"])])
    adjudication_required = _needs_adjudication(
        valid,
        malformed=malformed,
        has_comparison_differences="差异类型=" in reference_dossier,
    )
    adjudication: dict[str, Any] | None = None
    if adjudicator_model and adjudication_required:
        a_id, b_id = str(pair["higher"]), str(pair["lower"])
        prompt = _render_prompt(
            "calibration_pairwise.txt",
            {
                "PROBLEM_ID": str(papers[a_id].get("problem_id") or "UNKNOWN"),
                "COMPARISON_DOSSIER": reference_dossier,
                "PAPER_A": text_cache[a_id],
                "PAPER_B": text_cache[b_id],
            },
        )
        try:
            adjudication = _pair_judgment(
                prompt=prompt, model=adjudicator_model, timeout=timeout,
                a_id=a_id, b_id=b_id, sample="adjudication", role="adjudicator",
            )
            runs.append(adjudication)
        except (OSError, ValueError, json.JSONDecodeError, RuntimeError) as exc:
            malformed += 1
            runs.append({
                "sample": "adjudication", "role": "adjudicator", "model": adjudicator_model,
                "status": "MALFORMED", "error": str(exc),
            })

    primary_valid = [run for run in valid if run.get("role") == "primary"]

    def final_winner(key: str) -> str | None:
        if adjudication:
            return str(adjudication[key])
        return _majority(primary_valid, key)

    return {
        "schema_version": 2,
        "kind": "blind_pairwise",
        "model": model,
        "higher": pair["higher"],
        "lower": pair["lower"],
        "basis": pair.get("basis"),
        "samples_requested": samples + (1 if adjudicator_model and adjudication_required else 0),
        "samples_scored": sum(run.get("status") != "MALFORMED" for run in runs),
        "malformed": malformed,
        "overall_winner": final_winner("overall_winner"),
        "correctness_winner": final_winner("correctness_winner"),
        "writing_winner": final_winner("writing_winner"),
        "median_confidence": _median([
            float(run["confidence"]) for run in runs if run.get("status") != "MALFORMED"
        ]),
        "adjudication_required": adjudication_required,
        "adjudicated": adjudication is not None,
        "adjudicator_model": adjudicator_model,
        "runs": runs,
    }


def run_absolute(
    item: dict[str, Any],
    root: Path,
    *,
    model: str,
    samples: int,
    timeout: int,
) -> dict[str, Any]:
    paper_text = extract_paper_text(item, root)
    runs: list[dict[str, Any]] = []
    malformed = 0
    for sample in range(1, samples + 1):
        prompt = _render_prompt(
            "calibration_absolute.txt",
            {"PROBLEM_ID": str(item.get("problem_id") or "UNKNOWN"), "PAPER": paper_text},
        )
        try:
            parsed = validate_absolute(parse_json_output(call_judge(prompt, model, timeout, 6000)))
            parsed["sample"] = sample
            runs.append(parsed)
        except (OSError, ValueError, json.JSONDecodeError, RuntimeError) as exc:
            malformed += 1
            runs.append({"sample": sample, "status": "MALFORMED", "error": str(exc)})
    valid = [run for run in runs if run.get("status") != "MALFORMED"]
    correctness_scores = [float(run["correctness"]["score"]) for run in valid]
    writing_scores = [float(run["writing"]["score"]) for run in valid]
    fatal_counts = [int(run["correctness"]["fatal_flaws"]) for run in valid]
    dims = {
        name: _median([float(run["writing"]["dimensions"][name]) for run in valid])
        for name in WRITING_DIMENSIONS
    }
    return {
        "schema_version": 2,
        "kind": "split_absolute",
        "model": model,
        "paper_id": item["id"],
        "problem_id": item.get("problem_id"),
        "paper_sha256": _sha256(root / str(item["paper_path"])),
        "samples_requested": samples,
        "samples_scored": len(valid),
        "malformed": malformed,
        "correctness": {
            "median_score": _median(correctness_scores),
            "fatal_flaw_rate": (
                sum(value > 0 for value in fatal_counts) / len(fatal_counts) if fatal_counts else None
            ),
        },
        "writing": {"median_score": _median(writing_scores), "dimensions": dims},
        # Backward-compatible secondary absolute axis. Calibration readiness
        # never uses this score in place of explicit pairwise judgments.
        "llm_score": {"median_recomputed": _median(writing_scores), "n": samples, "n_scored": len(valid)},
        "runs": runs,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest")
    parser.add_argument("--model", default="deepseek-chat")
    parser.add_argument("--samples", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=360)
    parser.add_argument("--pairwise-only", action="store_true")
    parser.add_argument("--absolute-only", action="store_true")
    parser.add_argument("--results-dir", help="Override calibration_results_dir from the manifest.")
    parser.add_argument("--pair-text-limit", type=int, default=80000, help="UTF-8 byte budget per paper in pairwise prompts.")
    parser.add_argument("--adjudicator-model", help="Independent model used only for ties, instability, or close-document disputes.")
    args = parser.parse_args()
    if args.samples < 1 or (args.pairwise_only and args.absolute_only):
        parser.error("invalid mode or sample count")
    manifest_path = Path(args.manifest).resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    root = (manifest_path.parent / str(manifest.get("path_root") or ".")).resolve()
    results = root / str(args.results_dir or manifest.get("calibration_results_dir") or "evaluation/results/calibration")
    results.mkdir(parents=True, exist_ok=True)
    papers = {str(item["id"]): item for item in manifest.get("papers", [])}

    if not args.absolute_only:
        for pair in manifest.get("pairs", []):
            pair_id = str(pair.get("id") or f"{pair['higher']}__vs__{pair['lower']}")
            output = run_pair(
                pair, papers, root, model=args.model, samples=args.samples,
                timeout=args.timeout, text_limit=args.pair_text_limit,
                adjudicator_model=args.adjudicator_model,
            )
            path = results / f"pair_{pair_id}.json"
            path.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(f"pair {pair_id}: {output['overall_winner']} -> {path}")

    if not args.pairwise_only:
        for item in manifest.get("papers", []):
            output = run_absolute(item, root, model=args.model, samples=args.samples, timeout=args.timeout)
            path = results / f"paper_{item['id']}.json"
            path.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(f"paper {item['id']}: writing={output['writing']['median_score']} -> {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
