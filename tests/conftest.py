# tests/conftest.py
import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO_ROOT, "scripts")
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "mini_proj")

# --- live dashboard auth isolation -------------------------------------------------
#
# ``web/backend/main.py`` calls ``AuthStore.bootstrap_admin`` at *import* time, and
# ``AuthStore`` falls back to ``<factory_root>/web/auth.db`` when ``AUTH_DB_FILE`` is
# unset.  A test that imported the app while exporting its own ``ADMIN_PASSWORD``
# therefore overwrote the *running dashboard's* admin password with a literal
# committed in this repository: on 2026-09-20T12:13:33Z the stored admin hash became
# the test literal, making the live admin credential publicly known.
#
# This redirect runs at conftest import -- before any test module is collected -- so
# neither collection-time nor test-time app imports can reach the repository
# database.  ``setdefault`` yields to a value already present in the environment,
# and the tests that care assign ``AUTH_DB_FILE`` themselves, so their behaviour is
# unchanged.
_TEST_TMP_ROOT = Path(tempfile.mkdtemp(prefix="paper-factory-tests-"))
atexit.register(shutil.rmtree, _TEST_TMP_ROOT, ignore_errors=True)
os.environ.setdefault("AUTH_DB_FILE", str(_TEST_TMP_ROOT / "auth.db"))
# The dispatch preflight (phase 3d) probes model routes over the network and
# the host's credentials; tests that need it build the hook explicitly.  Assigned,
# not ``setdefault``: a host that exports FACTORY_DISPATCH_PREFLIGHT=1 must not
# turn the network probe back on for the whole suite.
os.environ["FACTORY_DISPATCH_PREFLIGHT"] = "0"

PRODUCTION_AUTH_DB = Path(REPO_ROOT) / "web" / "auth.db"


def _auth_db_bytes() -> bytes | None:
    try:
        return PRODUCTION_AUTH_DB.read_bytes()
    except OSError:
        return None


@pytest.fixture(scope="session", autouse=True)
def live_auth_db_is_never_written():
    """Fail the run if any test mutates the live dashboard auth database.

    The database uses ``journal_mode=delete``, so a write lands in this file
    directly and a byte comparison is a faithful signal.  The only expected way
    for it to change during a run is a restart of the API service itself, which
    is called out in the message so it cannot be mistaken for test corruption.
    """

    before = _auth_db_bytes()
    yield
    after = _auth_db_bytes()
    if before != after:
        pytest.fail(
            f"{PRODUCTION_AUTH_DB} changed during this test session. Tests must not "
            "write the live dashboard auth database -- point AUTH_DB_FILE at a "
            "temporary path instead. (Restarting the API service mid-run also "
            "rewrites this file.)",
            pytrace=False,
        )


@pytest.fixture(autouse=True)
def isolate_model_launcher_environment(monkeypatch):
    """Tests opt into routing/argv environment explicitly; host settings are not fixtures."""
    monkeypatch.delenv("CODEX_ONLY", raising=False)
    monkeypatch.delenv("CODEX_CLI_PATH", raising=False)
