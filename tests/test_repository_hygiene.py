from __future__ import annotations

import hashlib
import json
import os
import re
import stat
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


def _strict_manifest(root: Path) -> tuple[str, list[dict[str, object]], bytes]:
    manifest_path = root / "MANIFEST.json"
    manifest_bytes = manifest_path.read_bytes()

    def reject_duplicate_keys(pairs):
        value = {}
        for key, item in pairs:
            assert key not in value, f"duplicate manifest key: {key!r}"
            value[key] = item
        return value

    manifest = json.loads(
        manifest_bytes.decode("utf-8"),
        object_pairs_hook=reject_duplicate_keys,
    )
    assert isinstance(manifest, dict)
    archive_root = manifest["archive_root"]
    files = manifest["files"]
    assert isinstance(archive_root, str) and archive_root
    assert "/" not in archive_root and "\\" not in archive_root
    assert isinstance(files, list) and files
    assert all(isinstance(entry, dict) for entry in files)
    return archive_root, files, manifest_bytes


def _candidate_source_paths(root: Path) -> list[str]:
    archive_root, files, manifest_bytes = _strict_manifest(root)
    checksum_entries: dict[str, str] = {}
    checksums_path = root / "checksums" / "SHA256SUMS"
    for line in checksums_path.read_text(encoding="utf-8").splitlines():
        digest, separator, archive_path = line.partition("  ")
        assert separator == "  "
        assert re.fullmatch(r"[0-9a-f]{64}", digest)
        assert archive_path not in checksum_entries
        checksum_entries[archive_path] = digest

    source_paths: list[str] = []
    archive_paths: set[str] = set()
    for entry in files:
        source_path = entry.get("source_path")
        archive_path = entry.get("archive_path")
        digest = entry.get("sha256")
        size = entry.get("size")
        mode = entry.get("mode")
        assert isinstance(source_path, str) and source_path
        assert isinstance(archive_path, str) and archive_path
        assert isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest)
        assert isinstance(size, int) and size >= 0
        assert mode in {0o644, 0o755}
        parts = source_path.split("/")
        assert all(parts) and all(part not in {".", ".."} for part in parts)
        assert "\\" not in source_path and not source_path.startswith("/")
        assert archive_path == f"{archive_root}/{source_path}"
        assert source_path not in source_paths
        assert archive_path not in archive_paths
        source_paths.append(source_path)
        archive_paths.add(archive_path)

        path = root / source_path
        info = path.lstat()
        assert stat.S_ISREG(info.st_mode) and not path.is_symlink()
        normalized_mode = 0o755 if info.st_mode & 0o111 else 0o644
        assert info.st_size == size and normalized_mode == mode
        value = path.read_bytes()
        assert hashlib.sha256(value).hexdigest() == digest
        assert checksum_entries[archive_path] == digest

    manifest_archive = f"{archive_root}/MANIFEST.json"
    assert checksum_entries[manifest_archive] == hashlib.sha256(
        manifest_bytes
    ).hexdigest()
    assert set(checksum_entries) == archive_paths | {manifest_archive}
    return source_paths


def tracked_web_markdown(root: Path = ROOT) -> list[Path]:
    try:
        git_root = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=root,
            check=False,
            text=True,
            capture_output=True,
        )
    except OSError:
        git_root = None

    if git_root is not None and git_root.returncode == 0:
        assert Path(git_root.stdout.strip()).resolve() == root.resolve()
        result = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=root,
            check=True,
            stdout=subprocess.PIPE,
        )
        source_paths = [
            value.decode("utf-8") for value in result.stdout.split(b"\0") if value
        ]
    elif (
        root.resolve() == ROOT.resolve()
        and os.environ.get("PHASE9_TEST_SOURCE_REPOSITORY") is not None
    ):
        configured = Path(os.environ["PHASE9_TEST_SOURCE_REPOSITORY"])
        assert configured.is_absolute()
        configured = configured.resolve(strict=True)
        candidate_top = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=configured,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
        )
        assert Path(candidate_top.stdout.strip()).resolve() == configured
        result = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=configured,
            check=True,
            stdout=subprocess.PIPE,
        )
        source_paths = [
            value.decode("utf-8") for value in result.stdout.split(b"\0") if value
        ]
        for relative in source_paths:
            if relative.startswith("web/") and relative.endswith(".md"):
                expected = subprocess.check_output(
                    ["git", "show", f"HEAD:{relative}"], cwd=configured
                )
                assert (root / relative).read_bytes() == expected
    else:
        source_paths = _candidate_source_paths(root)

    return sorted(
        root / relative
        for relative in source_paths
        if relative.startswith("web/") and relative.endswith(".md")
    )


def test_tracked_web_markdown_uses_exact_candidate_manifest_without_git(
    tmp_path,
    monkeypatch,
):
    source_values = {
        "web/README.md": b"# Current Web documentation\n",
        "web/docs/history/OLD.md": b"# Historical Web documentation\n",
    }
    archive_root = "paper_factory_phase4_6_candidate"
    manifest_files = []
    for relative, value in source_values.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value)
        path.chmod(0o644)
        manifest_files.append(
            {
                "archive_path": f"{archive_root}/{relative}",
                "mode": 0o644,
                "sha256": hashlib.sha256(value).hexdigest(),
                "size": len(value),
                "source_path": relative,
            }
        )
    unlisted = tmp_path / "web" / "runtime" / "UNLISTED.md"
    unlisted.parent.mkdir(parents=True)
    unlisted.write_text("not part of the candidate closure\n", encoding="utf-8")

    manifest = {
        "archive_root": archive_root,
        "files": manifest_files,
    }
    manifest_bytes = (
        json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    (tmp_path / "MANIFEST.json").write_bytes(manifest_bytes)
    checksums = {
        entry["archive_path"]: entry["sha256"] for entry in manifest_files
    }
    checksums[f"{archive_root}/MANIFEST.json"] = hashlib.sha256(
        manifest_bytes
    ).hexdigest()
    checksum_path = tmp_path / "checksums" / "SHA256SUMS"
    checksum_path.parent.mkdir()
    checksum_path.write_text(
        "".join(
            f"{checksums[path]}  {path}\n" for path in sorted(checksums)
        ),
        encoding="utf-8",
    )

    real_run = subprocess.run

    def no_git_worktree(command, *args, **kwargs):
        if command[:3] == ["git", "rev-parse", "--show-toplevel"]:
            return subprocess.CompletedProcess(command, 128, stdout="", stderr="")
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", no_git_worktree)

    assert tracked_web_markdown(tmp_path) == [
        tmp_path / "web" / "README.md",
        tmp_path / "web" / "docs" / "history" / "OLD.md",
    ]


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
