from pathlib import Path
import hashlib
import json
import os
import subprocess


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_external_evaluation_uses_independent_scoring_eligibility():
    text = (REPO_ROOT / "evaluation/run_evaluation.sh").read_text(encoding="utf-8")

    assert "internal Gate 2/delivery state is structural evidence" in text
    assert "packet integrity FAIL" in text
    assert "packet completeness INCOMPLETE" in text
    assert "affected roles will be forced to INDETERMINATE" in text
    assert "refusing to spend judge tokens on an incomplete project" not in text


def test_role_prompt_receives_manifest_and_numeric_traceability_signal():
    text = (REPO_ROOT / "evaluation/run_evaluation.sh").read_text(encoding="utf-8")

    assert 'cat "$PROJECT/judge_packets/$role/manifest.json"' in text
    assert 'printf \'UNMATCHED_NUMBERS=%s\\n\' "$UNMATCHED"' in text
    assert "UNTRUSTED ISOLATED PACKET START" in text


def test_evaluation_results_use_unique_immutable_run_directory():
    text = (REPO_ROOT / "evaluation/run_evaluation.sh").read_text(encoding="utf-8")

    assert 'RUN_PARENT="$RESULTS_DIR/runs/$BASE"' in text
    assert '"configuration_fingerprint"' in text
    assert 'AGG_JSON="$RUN_DIR/eval.json"' in text
    assert 'chmod -R a-w "$RUN_DIR"' in text
    assert "symlink_to" in text
    assert "preserving diagnostic JSON for enrichment/publication" in text
    assert 'data.get("comparison_ready") is True' in text


def test_evaluation_configuration_binds_all_result_provenance_inputs():
    text = (REPO_ROOT / "evaluation/run_evaluation.sh").read_text(encoding="utf-8")

    for name in (
        "parse_judge_score_sha256",
        "enrich_evaluation_result_sha256",
        "evaluate_modeling_project_sha256",
    ):
        assert name in text
    assert '"calibration_report": optional_file_record' in text
    assert '"exists": exists' in text
    assert '"sha256": sha(path) if exists else None' in text
    assert '--calibration-report "$CALIBRATION_REPORT"' in text


def test_hard_fail_still_publishes_immutable_diagnostic_result(tmp_path):
    project = tmp_path / "demo"
    (project / "problem").mkdir(parents=True)
    (project / "paper").mkdir()
    (project / "results").mkdir()
    (project / "models/m1").mkdir(parents=True)
    (project / "problem/source.md").write_text("Problem statement", encoding="utf-8")
    (project / "paper/paper.tex").write_text("Final paper claim: objective 42", encoding="utf-8")
    (project / "model.md").write_text("Mathematical model", encoding="utf-8")
    (project / "models/m1/02_model.py").write_text("print(42)\n", encoding="utf-8")
    (project / "solve_log.md").write_text("solver completed", encoding="utf-8")
    (project / "results/canonical_results.json").write_text(
        '{"objective": 42}', encoding="utf-8"
    )

    fake_claude = tmp_path / "fake-claude"
    fake_claude.write_text(
        """#!/usr/bin/env python3
import json, sys
p = sys.stdin.read()
if "独立数学审计员" in p:
    d = {"schema_version":"judge-role-v1","role":"math","verdict":"FAIL","fatal_flaws":1,"evidence":[{"claim":"c","location":"paper/paper.tex","finding":"contradiction","severity":"fatal"}],"limitations":[],"conclusion":"fail"}
    print("VERDICT: FAIL")
elif "独立执行审计员" in p:
    d = {"schema_version":"judge-role-v1","role":"execution","verdict":"PASS","fatal_flaws":0,"evidence":[{"claim":"c","location":"results/canonical_results.json","finding":"traceable","severity":"support"}],"limitations":[],"conclusion":"pass"}
    print("VERDICT: PASS")
else:
    dims = {k:{"score":s,"evidence":[{"location":"paper/paper.tex","finding":"e"}]} for k,s in [("model_presentation",16),("solution_narrative",16),("innovation",16),("writing_clarity",12),("result_persuasiveness",12),("sensitivity_limitations",8)]}
    d = {"schema_version":"judge-role-v1","role":"paper","verdict":"PASS","dimensions":dims,"overall_score":80,"limitations":[],"recommendations":["a","b","c"],"conclusion":"pass"}
    print("VERDICT: PASS")
print(json.dumps(d, ensure_ascii=False))
""",
        encoding="utf-8",
    )
    fake_claude.chmod(0o755)
    results = tmp_path / "evaluation-results"
    home = tmp_path / "home"
    home.mkdir()
    calibration_report = tmp_path / "calibration.json"
    calibration_report.write_text('{"runtime_score_reliability":{"ready":false}}\n', encoding="utf-8")
    env = os.environ.copy()
    env.update({
        "CLAUDE_BIN": str(fake_claude),
        "CLAUDE_MODEL": "fake-judge",
        "EVALUATION_RESULTS_DIR": str(results),
        "EVALUATION_CALIBRATION_REPORT": str(calibration_report),
        "HOME": str(home),
    })

    completed = subprocess.run(
        [
            str(REPO_ROOT / "evaluation/run_evaluation.sh"),
            str(project),
            "demo",
            "--samples",
            "1",
        ],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )

    assert completed.returncode == 1, completed.stderr
    run_dirs = [path for path in (results / "runs/demo").iterdir() if path.is_dir()]
    assert len(run_dirs) == 1
    aggregate = json.loads((run_dirs[0] / "eval.json").read_text(encoding="utf-8"))
    configuration = json.loads(
        (run_dirs[0] / "configuration.json").read_text(encoding="utf-8")
    )
    assert aggregate["evaluation_run"]["configuration_fingerprint"]
    assert aggregate["scoring_eligible"] is True
    assert aggregate["model"] == "fake-judge"
    assert aggregate["judge_config"]["configuration_fingerprint"]
    assert aggregate["calibration_schema_version"] == 3
    assert aggregate["overall_score"] is None
    assert configuration["version"] == 2
    assert configuration["calibration_report"] == {
        "path": str(calibration_report),
        "exists": True,
        "sha256": hashlib.sha256(calibration_report.read_bytes()).hexdigest(),
    }
    for key, relative in (
        ("parse_judge_score_sha256", "scripts/parse_judge_score.py"),
        ("enrich_evaluation_result_sha256", "scripts/enrich_evaluation_result.py"),
        ("evaluate_modeling_project_sha256", "scripts/evaluate_modeling_project.py"),
    ):
        assert configuration["implementation"][key] == hashlib.sha256(
            (REPO_ROOT / relative).read_bytes()
        ).hexdigest()
    latest = results / "demo_eval.json"
    assert latest.is_symlink()
    assert latest.resolve() == run_dirs[0] / "eval.json"
    for path in run_dirs[0].rglob("*"):
        if path.is_dir():
            path.chmod(0o755)
        else:
            path.chmod(0o644)
    run_dirs[0].chmod(0o755)
