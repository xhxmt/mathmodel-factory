from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STEP2_PROMPTS = (
    ROOT / "prompts" / "step2_modeling_proposal.txt",
    ROOT / "prompts" / "step2_modeling_critic.txt",
)


def test_step2_prompts_query_jobs_through_solver_wrapper():
    for prompt in STEP2_PROMPTS:
        text = prompt.read_text(encoding="utf-8")

        assert "solver_submit.sh --status" in text
        assert "--json" in text
        assert "solver-job-evidence-v1" in text
        assert "run_state/solver_jobs/" not in text


def test_step2_prompts_keep_solver_storage_implementation_private():
    proposal = STEP2_PROMPTS[0].read_text(encoding="utf-8")
    critic = STEP2_PROMPTS[1].read_text(encoding="utf-8")

    assert "不要直接读取 `.factory/state.db` 或旧 `run_state` 文件" in proposal
    assert "不要绕过 wrapper 直接读取 SQLite 或兼容状态文件" in critic


def test_step2_prompts_bind_job_evidence_to_the_demo_script():
    for prompt in STEP2_PROMPTS:
        text = prompt.read_text(encoding="utf-8")

        for field in ("job_id", "runtime", "script", "status"):
            assert f"`{field}`" in text
