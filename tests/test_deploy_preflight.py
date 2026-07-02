from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_deploy_script_runs_preflight_for_secret_manager_contract():
    deploy = (REPO_ROOT / "web" / "deploy.sh").read_text(encoding="utf-8")

    assert "preflight()" in deploy
    assert "MINERU_TOKEN|GEMINI_API_KEY|DEEPSEEK_API_KEY|JWT_SECRET|JWT_SECRET_KEY|ADMIN_PASSWORD" in deploy
    assert 'source "$PROJECT_ROOT/scripts/load_secrets.sh"' in deploy
    assert "bash -n" in deploy
    assert "preflight" in deploy.split("# 主流程", maxsplit=1)[1]


def test_deploy_health_check_waits_for_backend_and_fails_on_timeout():
    deploy = (REPO_ROOT / "web" / "deploy.sh").read_text(encoding="utf-8")

    assert "wait_for_http()" in deploy
    assert "http://127.0.0.1:8000/" in deploy
    assert "for attempt in" in deploy
    assert "return 1" in deploy.split("test_deployment()", maxsplit=1)[1]


def test_systemd_unit_template_sources_secret_manager_loader():
    unit_path = REPO_ROOT / "deploy" / "systemd" / "paper-factory-api.service"

    assert unit_path.exists()
    unit = unit_path.read_text(encoding="utf-8")
    assert "scripts/load_secrets.sh" in unit
    assert "ExecStart=" in unit
    assert "EnvironmentFile=/home/tfisher/paper_factory/web/.env" in unit
    assert "ReadWritePaths=/home/tfisher/paper_factory" in unit
    assert "correct horse battery staple" not in unit
    assert "secret-value" not in unit
