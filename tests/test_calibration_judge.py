import json
from pathlib import Path

import pytest

from scripts.calibration_judge import (
    _anonymous_order,
    _adjudication_order,
    anonymize_text,
    comparison_dossier,
    _derive_overall,
    _evaluator_identity,
    _select_pairs,
    load_problem_context,
    _needs_adjudication,
    parse_json_output,
    validate_absolute,
    validate_pairwise,
    run_pair,
)
from scripts.proxy_calibration import apply_perturbation


def test_anonymous_order_is_deterministic_and_uses_only_pair_ids():
    pair = {"higher": "national", "lower": "provincial"}
    assert _anonymous_order(pair, 1) == ("national", "provincial")
    assert _anonymous_order(pair, 2) == ("provincial", "national")


def test_pairwise_requires_even_primary_samples_and_adjudicator_has_stable_orientation(tmp_path):
    pair = {"id": "demo", "higher": "national", "lower": "provincial"}
    assert _adjudication_order(pair) in {
        ("national", "provincial"),
        ("provincial", "national"),
    }
    assert _adjudication_order(pair) == _adjudication_order(pair)
    with pytest.raises(ValueError, match="even number"):
        run_pair(pair, {}, tmp_path, model="deepseek-chat", samples=3, timeout=1)


def test_composite_evaluator_identity_does_not_attribute_adjudication_to_primary():
    identity = _evaluator_identity(
        "deepseek-chat",
        adjudicator_model="gemini-3.1-pro-preview",
        decision_source="adjudicator",
    )
    assert identity == {
        "schema": "calibration-evaluator-v1",
        "kind": "composite",
        "primary_model": "deepseek-chat",
        "adjudicator_model": "gemini-3.1-pro-preview",
        "models": ["deepseek-chat", "gemini-3.1-pro-preview"],
        "decision_source": "adjudicator",
    }


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


def test_select_pairs_supports_resumable_targeted_runs():
    pairs = [
        {"id": "one", "higher": "a", "lower": "b"},
        {"id": "two", "higher": "c", "lower": "d"},
    ]
    assert _select_pairs(pairs, {"two"}) == [pairs[1]]
    with pytest.raises(ValueError):
        _select_pairs(pairs, {"missing"})


def test_problem_context_loads_frozen_problem_and_review_focus(tmp_path):
    (tmp_path / "problem.md").write_text("正式赛题内容", encoding="utf-8")
    manifest = {
        "problem_contexts": {
            "X": {"path": "problem.md", "review_focus": ["检查约束", "检查答案"]}
        }
    }
    context = load_problem_context(manifest, "X", tmp_path)
    assert "正式赛题内容" in context
    assert "检查约束" in context


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
