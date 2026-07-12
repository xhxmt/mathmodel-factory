import json
import sys


def test_problem_specific_eval_promises_reconcile_without_generic_warning(
    tmp_path, monkeypatch, capsys
):
    from scripts import verify_spec_impl

    (tmp_path / "model.md").write_text(
        "# Model\n\n"
        "### 8.1 结构分析与降维\n\n"
        "逐问题实际评估预算：问题 2 n_eval=544；问题 3 n_eval=14496；"
        "问题 4 n_eval=21744；问题 5 n_eval=64636。\n\n"
        "## 9. 边界情形\n",
        encoding="utf-8",
    )
    for problem, n_eval in ((2, 544), (3, 14496), (4, 21744), (5, 64636)):
        path = tmp_path / "results" / f"problem{problem}" / "values.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "status": "FEASIBLE",
                    "provenance": {
                        "solver": "differential_evolution" if problem < 5 else "custom PSO",
                        "budget": {"n_eval": n_eval},
                    },
                }
            ),
            encoding="utf-8",
        )

    monkeypatch.setattr(sys, "argv", ["verify_spec_impl.py", str(tmp_path)])

    assert verify_spec_impl.main() == 0
    output = capsys.readouterr().out
    assert "SPEC_IMPL_BLOCKING=0" in output
    assert "SPEC_IMPL_WARN=0" in output
