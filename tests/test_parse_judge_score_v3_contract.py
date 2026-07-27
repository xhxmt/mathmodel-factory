from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

from scripts.parse_judge_score import parse_file


DIMENSIONS = (
    ("model_presentation", "模型呈现", 20, 18),
    ("solution_narrative", "求解叙事", 20, 18),
    ("innovation", "创新性", 20, 17),
    ("writing_clarity", "写作清晰度", 15, 14),
    ("result_persuasiveness", "结果说服力", 15, 14),
    ("sensitivity_limitations", "敏感性与局限", 10, 9),
)


def _grounding(role: str, *, enforced: bool = False, valid: bool | None = None) -> dict:
    return {
        "schema_version": "evidence-grounding-v1",
        "role": role,
        "enforced": enforced,
        "valid": valid,
        "refs": [],
        "errors": [],
    }


def _payload() -> dict:
    quote = "grounded quote"
    quote_sha256 = hashlib.sha256(quote.encode("utf-8")).hexdigest()
    dimensions = {
        key: {
            "label": label,
            "max": maximum,
            "score": score,
            "evidence": [{
                "ref_id": f"{key}-ref",
                "chunk_id": "a" * 64,
                "quote": quote,
                "quote_sha256": quote_sha256,
                "finding": "grounded",
            }],
        }
        for key, label, maximum, score in DIMENSIONS
    }
    return {
        "schema_version": "judge-aggregate-v3",
        "verdict": "PASS",
        "status": "PASS",
        "score_available": True,
        "score_semantics": "UNCALIBRATED_DIAGNOSTIC",
        "comparison_ready": False,
        "overall_score": 90,
        "paper_score": 90,
        "vetoes": [],
        "indeterminate_roles": [],
        "dimensions": dimensions,
        "role_statuses": {"math": "PASS", "execution": "PASS", "paper": "PASS"},
        "evidence_grounding": {
            role: _grounding(role) for role in ("math", "execution", "paper")
        },
    }


def _write(path: Path, payload: dict) -> Path:
    path.write_text(
        "VERDICT: PASS\n"
        "<!-- JUDGE_AGGREGATE_JSON_BEGIN -->\n"
        f"{json.dumps(payload, ensure_ascii=False)}\n"
        "<!-- JUDGE_AGGREGATE_JSON_END -->\n",
        encoding="utf-8",
    )
    return path


def test_v3_requires_and_preserves_grounding_summary(tmp_path: Path) -> None:
    parsed = parse_file(_write(tmp_path / "judge.md", _payload()))

    assert parsed["schema_valid"] is True
    assert parsed["schema_version"] == "judge-aggregate-v3"
    assert parsed["score_available"] is True
    assert set(parsed["evidence_grounding"]) == {"math", "execution", "paper"}


def test_v3_missing_grounding_is_not_current_schema(tmp_path: Path) -> None:
    payload = _payload()
    payload.pop("evidence_grounding")
    parsed = parse_file(_write(tmp_path / "judge.md", payload))

    assert parsed["schema_valid"] is False
    assert parsed["score_available"] is False
    assert "evidence_grounding" in parsed["parse_error"]


def test_v3_cannot_claim_pass_with_invalid_enforced_grounding(tmp_path: Path) -> None:
    payload = _payload()
    payload["evidence_grounding"]["paper"] = _grounding(
        "paper", enforced=True, valid=False
    )
    parsed = parse_file(_write(tmp_path / "judge.md", payload))

    assert parsed["schema_valid"] is False
    assert "evidence_grounding.paper is invalid" in parsed["parse_error"]


def test_v2_aggregate_is_read_only_legacy_and_never_scored(tmp_path: Path) -> None:
    payload = copy.deepcopy(_payload())
    payload["schema_version"] = "judge-aggregate-v2"
    payload["overall_score"] = 90
    parsed = parse_file(_write(tmp_path / "judge.md", payload))

    assert parsed["schema_valid"] is False
    assert parsed["legacy"] is True
    assert parsed["status"] == "LEGACY_UNVERIFIED"
    assert parsed["score_available"] is False
    assert parsed["comparison_ready"] is False
    assert parsed["total"] is None
