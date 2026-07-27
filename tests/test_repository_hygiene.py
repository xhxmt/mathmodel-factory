from __future__ import annotations

import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CURRENT_WEB_DOCS = (
    ROOT / "web" / "README.md",
    ROOT / "web" / "QUICKSTART.md",
    ROOT / "web" / "USAGE_GUIDE.md",
    ROOT / "web" / "docs" / "deployment" / "DEPLOYMENT.md",
)
CURRENT_ENTRY_DOCS = (
    ROOT / "README.md",
    ROOT / "DOCUMENTATION_INDEX.md",
    ROOT / "AGENTS.md",
    ROOT / "CLAUDE.md",
    *CURRENT_WEB_DOCS,
)


def tracked_web_markdown() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "web/*.md", "web/**/*.md"],
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
    )
    return [ROOT / line for line in result.stdout.splitlines() if line]


def test_current_web_docs_do_not_reintroduce_retired_contracts():
    retired_phrases = (
        "USERS_DB",
        "默认登录凭据",
        "目前使用内存数据库",
        "系统会自动生成随机的 JWT Secret",
        "python app.py",
        "python3 backend/app.py",
    )
    weak_login_example = "admin" + "123"

    for path in CURRENT_WEB_DOCS:
        text = path.read_text(encoding="utf-8")
        assert weak_login_example not in text, path
        for phrase in retired_phrases:
            assert phrase not in text, f"{path}: retired phrase {phrase!r}"


def test_tracked_historical_web_docs_are_labeled_and_point_to_current_owners():
    current = {path.resolve() for path in CURRENT_WEB_DOCS}
    link_pattern = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
    for path in tracked_web_markdown():
        if path.resolve() in current:
            continue
        opening = "\n".join(path.read_text(encoding="utf-8").splitlines()[:16])
        assert "历史快照（非现役合同）" in opening, path
        assert "Web README" in opening, path
        assert "现役 runbook" in opening, path
        targets = {(path.parent / target).resolve() for target in link_pattern.findall(opening)}
        assert (ROOT / "web" / "README.md").resolve() in targets, path
        assert (ROOT / "web" / "docs" / "deployment" / "DEPLOYMENT.md").resolve() in targets, path


def test_tracked_web_docs_do_not_contain_plaintext_password_examples():
    password_value = re.compile(
        r"(?i)(?:password|密码)\s*[:：=]\s*`?([A-Za-z0-9][A-Za-z0-9!@#$%^&*._-]{7,})"
    )
    allowed = {
        "secret",
        "secretmanager",
        "redacted",
        "removed",
    }

    for path in tracked_web_markdown():
        text = path.read_text(encoding="utf-8")
        for match in password_value.finditer(text):
            value = match.group(1).lower().replace("-", "").replace("_", "")
            assert value in allowed, f"{path}: plaintext password-like example near line {text[:match.start()].count(chr(10)) + 1}"


def test_secret_examples_and_diagnostics_never_print_value_prefixes():
    paths = (
        ROOT / "docs" / "SECRET_MANAGER_GUIDE.md",
        ROOT / "scripts" / "setup_secret_manager.sh",
        ROOT / "scripts" / "test_secret_manager.sh",
        ROOT / "web" / "check_status.sh",
    )
    prefix_display = re.compile(
        r"\$\{(?:MINERU_TOKEN|GEMINI_API_KEY|DEEPSEEK_API_KEY|JWT_SECRET|ADMIN_PASSWORD):0:"
    )
    direct_echo = re.compile(
        r"(?m)^\s*echo\s+.*\$(?:MINERU_TOKEN|GEMINI_API_KEY|DEEPSEEK_API_KEY|JWT_SECRET|ADMIN_PASSWORD)"
    )
    for path in paths:
        text = path.read_text(encoding="utf-8")
        assert not prefix_display.search(text), path
        assert not direct_echo.search(text), path


def test_example_env_files_contain_no_sensitive_assignments():
    sensitive_assignment = re.compile(
        r"(?m)^(?:JWT_SECRET|JWT_SECRET_KEY|ADMIN_PASSWORD|MINERU_TOKEN|"
        r"GEMINI_API_KEY|DEEPSEEK_API_KEY|DASHSCOPE_API_KEY|TELEGRAM_BOT_TOKEN)="
    )
    for path in (ROOT / ".env.example", ROOT / "web" / ".env.example"):
        text = path.read_text(encoding="utf-8")
        assert not sensitive_assignment.search(text), path


def test_deploy_builds_frontend_as_service_user():
    text = (ROOT / "web" / "deploy.sh").read_text(encoding="utf-8")
    assert "set -euo pipefail" in text
    assert 'sudo -u "$SERVICE_USER" -H bash -lc' in text
    assert "dist/index.html" in text


def test_current_entry_doc_links_resolve():
    link_pattern = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
    for path in CURRENT_ENTRY_DOCS:
        text = path.read_text(encoding="utf-8")
        for raw_target in link_pattern.findall(text):
            target = raw_target.split("#", 1)[0].strip()
            if not target or target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            resolved = (path.parent / target).resolve()
            assert resolved.exists(), f"{path}: missing link target {raw_target}"
