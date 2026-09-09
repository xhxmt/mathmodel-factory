# tests/conftest.py
import os
import sys
import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO_ROOT, "scripts")
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "mini_proj")


@pytest.fixture(autouse=True)
def isolate_model_launcher_environment(monkeypatch):
    """Tests opt into routing/argv environment explicitly; host settings are not fixtures."""
    monkeypatch.delenv("CODEX_ONLY", raising=False)
    monkeypatch.delenv("CODEX_CLI_PATH", raising=False)
