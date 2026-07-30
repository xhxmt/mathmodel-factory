import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_deploy_script_runs_preflight_for_secret_manager_contract():
    deploy = (REPO_ROOT / "web" / "deploy.sh").read_text(encoding="utf-8")

    assert "preflight()" in deploy
    for sensitive_key in (
        "MINERU_TOKEN",
        "GEMINI_API_KEY",
        "DEEPSEEK_API_KEY",
        "DASHSCOPE_API_KEY",
        "JWT_SECRET",
        "JWT_SECRET_KEY",
        "ADMIN_PASSWORD",
        "TELEGRAM_BOT_TOKEN",
    ):
        assert sensitive_key in deploy
    assert 'source "$PROJECT_ROOT/scripts/load_secrets.sh"' in deploy
    assert '"$PROJECT_ROOT/factory_core/adapters/legacy_runner.sh"' in deploy
    for locked_input in (
        "pyproject.toml",
        "uv.lock",
        "web/backend/requirements.lock",
        "cloud/requirements.lock",
        "web/frontend/package-lock.json",
    ):
        assert locked_input in deploy
    assert "build_native_registry" in deploy
    assert "list(range(17))" in deploy
    assert 'grep -Fq "WorkingDirectory=$PROJECT_ROOT"' in deploy
    assert ".venv/bin/uvicorn apps.web.backend.main:app" in deploy
    assert "bash -n" in deploy
    assert "preflight" in deploy.split("# 主流程", maxsplit=1)[1]

    runbook = (REPO_ROOT / "web/docs/deployment/DEPLOYMENT.md").read_text(encoding="utf-8")
    assert "uv sync --extra web --extra models --locked" in runbook


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
    assert "WorkingDirectory=/home/tfisher/paper_factory" in unit
    assert "/paper_factory/.venv/bin/uvicorn" in unit
    assert "apps.web.backend.main:app" in unit
    assert "web/backend/venv" not in unit
    assert "correct horse battery staple" not in unit
    assert "secret-value" not in unit


def test_runtime_start_scripts_do_not_install_dependencies():
    backend = (REPO_ROOT / "web/backend/start.sh").read_text(encoding="utf-8")
    frontend = (REPO_ROOT / "web/frontend/start.sh").read_text(encoding="utf-8")

    for script in (backend, frontend):
        assert re.search(
            r"^\s*(?:pip install|npm install|npm ci|python3 -m venv)\b",
            script,
            re.MULTILINE,
        ) is None
    assert "/.venv/bin/python" in backend
    assert 'exec npm run dev' in frontend


def test_reproducible_build_inputs_are_present_and_exported_from_uv_lock():
    expected = (
        REPO_ROOT / "pyproject.toml",
        REPO_ROOT / "uv.lock",
        REPO_ROOT / "web/backend/requirements.lock",
        REPO_ROOT / "cloud/requirements.lock",
        REPO_ROOT / "web/frontend/package-lock.json",
    )
    assert all(path.is_file() and path.stat().st_size > 0 for path in expected)
    web_export = expected[2].read_text(encoding="utf-8")
    assert "--extra web" in web_export.splitlines()[1]
    assert "--extra models" in web_export.splitlines()[1]
    assert "google-antigravity==0.1.0" in web_export
    assert "--extra cloud" in expected[3].read_text(encoding="utf-8").splitlines()[1]

    dockerfile = (REPO_ROOT / "cloud/Dockerfile").read_text(encoding="utf-8")
    assert "COPY requirements.lock" in dockerfile
    assert "--require-hashes -r requirements.lock" in dockerfile
    assert "COPY requirements.txt" not in dockerfile
