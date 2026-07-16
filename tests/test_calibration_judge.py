import json
from pathlib import Path

import pytest

from scripts.calibration_judge import (
    _anonymous_order,
    anonymize_text,
    comparison_dossier,
    _derive_overall,
    _needs_adjudication,
    parse_json_output,
    validate_absolute,
    validate_pairwise,
)
from scripts.proxy_calibration import apply_perturbation


def test_anonymous_order_is_deterministic_and_uses_only_pair_ids():
    pair = {"higher": "national", "lower": "provincial"}
    assert _anonymous_order(pair, 1) == ("national", "provincial")
    assert _anonymous_order(pair, 2) == ("provincial", "national")


def test_parse_json_output_accepts_fenced_json():
    data = parse_json_output('```json\n{"overall_winner":"A"}\n```')
    assert data["overall_winner"] == "A"


def test_pairwise_contract_rejects_invalid_winner():
    with pytest.raises(ValueError):
        validate_pairwise(
            {
                "overall_winner": "national_first",
                "correctness_winner": "A",
                "writing_winner": "B",
                "confidence": 0.8,
                "fatal_flaw_a": False,
                "fatal_flaw_b": False,
                "fatal_evidence_a": [],
                "fatal_evidence_b": [],
            }
        )


def test_pairwise_contract_requires_fatal_audit_fields():
    with pytest.raises(ValueError):
        validate_pairwise(
            {
                "overall_winner": "A",
                "correctness_winner": "A",
                "writing_winner": "A",
                "confidence": 0.8,
            }
        )


def test_comparison_dossier_exposes_small_numeric_change():
    original = "\n".join(["共同内容"] * 30 + ["最终结果为 4.20 秒"] + ["共同结尾"] * 30)
    changed = original.replace("4.20", "7.27")
    dossier = comparison_dossier(original, changed)
    assert "4.20" in dossier
    assert "7.27" in dossier


def test_close_document_difference_always_routes_to_independent_adjudication():
    runs = [
        {
            "overall_winner": "A",
            "fatal_flaw_a": False,
            "fatal_flaw_b": False,
        }
    ]
    assert _needs_adjudication(runs, malformed=0, has_comparison_differences=True)


def test_stable_distinct_document_result_does_not_require_adjudication():
    runs = [
        {
            "overall_winner": "A",
            "fatal_flaw_a": False,
            "fatal_flaw_b": False,
        },
        {
            "overall_winner": "A",
            "fatal_flaw_a": False,
            "fatal_flaw_b": False,
        },
    ]
    assert not _needs_adjudication(runs, malformed=0, has_comparison_differences=False)


def test_overall_uses_clear_writing_loss_when_correctness_ties():
    assert _derive_overall("TIE", "clean", "TIE") == "clean"


def test_overall_uses_correctness_when_axes_conflict():
    assert _derive_overall("mathematically_correct", "better_written", "TIE") == "mathematically_correct"


def test_absolute_contract_requires_all_writing_dimensions():
    payload = {
        "correctness": {"score": 80, "fatal_flaws": 0},
        "writing": {"score": 82, "dimensions": {"answer_completeness": 80}},
    }
    with pytest.raises(ValueError):
        validate_absolute(payload)


def test_anonymize_text_removes_award_and_identity_tokens():
    text = anonymize_text(
        "省一等奖论文，队号 ABCD1234，项目 generated_current_pass。",
        {"id": "generated_current_pass", "award_tier": "provincial_first"},
    )
    assert "一等奖" not in text
    assert "ABCD1234" not in text
    assert "generated_current_pass" not in text


def test_proxy_perturbations_are_deterministic_and_change_text():
    source = "# 结果\n主要时间为 4.20 s。\n# 灵敏度分析\n扰动后稳定。\n# 结论\n完成。\n"
    assert apply_perturbation(source, "numeric_contradiction") == apply_perturbation(
        source, "numeric_contradiction"
    )
    assert apply_perturbation(source, "numeric_contradiction") != source
    assert "灵敏度分析" not in apply_perturbation(source, "no_sensitivity")
