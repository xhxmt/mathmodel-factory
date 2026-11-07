"""Read-only checks that a run can start: model routes and scratch space.

Two acceptance runs in October 2026 stopped on causes this module can see
before any model is called: a provider balance that returned 402 mid-step, and
a memory-backed ``/tmp`` that a checker filled.  The dispatcher then reported
only the last fallback candidate, so the real cause never reached the event
stream.  ``factory preflight`` resolves each Step's candidate chain exactly as
:class:`~factory_core.adapters.models.dispatcher.ModelDispatcher` does and
reports, per candidate, whether its executable, credential and provider are
usable - without printing any credential.

The checks never write workflow state.  A check that cannot be decided (no
network, a provider without a known probe) is ``UNKNOWN``, never ``PASS``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Mapping

from .runtime_env import disk_tmpdir_overrides, effective_tempdir, filesystem_type

PASS, WARN, FAIL, UNKNOWN = "PASS", "WARN", "FAIL", "UNKNOWN"
_SEVERITY = {PASS: 0, UNKNOWN: 1, WARN: 1, FAIL: 2}
DEEPSEEK_BALANCE_URL = "https://api.deepseek.com/user/balance"
DSH_MIN_NODE_MAJOR = 24

# (status, body) for a GET; raising is reserved for "the host is unreachable".
HttpGet = Callable[[str, Mapping[str, str], float], "tuple[int, bytes]"]


@dataclass
class Check:
    name: str
    status: str
    detail: str
    data: dict = field(default_factory=dict)


def _urllib_get(url: str, headers: Mapping[str, str], timeout: float) -> tuple[int, bytes]:
    request = urllib.request.Request(url, headers=dict(headers), method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status), response.read(65536)
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read(4096) if exc.fp is not None else b""


def _http_verdict(status: int) -> tuple[str, str]:
    if 200 <= status < 300:
        return PASS, f"HTTP {status}"
    if status in (401, 403):
        return FAIL, f"HTTP {status}: the credential is rejected"
    if status == 402:
        return FAIL, "HTTP 402: the account has no balance (quota exhausted)"
    if status == 429:
        return WARN, "HTTP 429: rate limited right now"
    return UNKNOWN, f"HTTP {status}: no verdict from this endpoint"


# --------------------------------------------------------------------------
# Scratch space
# --------------------------------------------------------------------------


def check_scratch_space(
    factory_root: Path,
    project_dir: Path | None,
    *,
    min_free_bytes: int,
    environ: Mapping[str, str] | None = None,
    mounts_text: str | None = None,
) -> list[Check]:
    environ = os.environ if environ is None else environ
    overrides = disk_tmpdir_overrides(factory_root, environ, mounts_text=mounts_text, create=False)
    tmpdir = Path(overrides.get("TMPDIR") or effective_tempdir(environ))
    targets = [("scratch:TMPDIR", tmpdir)]
    if project_dir is not None:
        targets.append(("scratch:project", project_dir))
    checks = []
    for name, path in targets:
        # A run creates its scratch directory on start; measure where it will live.
        probe = path
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        if not probe.is_dir():
            # Either the target itself or a parent the run would create it
            # under is a file: creating the scratch directory would fail.
            checks.append(Check(name, FAIL, f"{path}: {probe} exists and is not a directory",
                                {"path": str(path), "blocking_path": str(probe)}))
            continue
        try:
            free = shutil.disk_usage(probe).free
        except OSError as exc:
            checks.append(Check(name, FAIL, f"{path}: {exc.strerror or exc}"))
            continue
        fs_type = filesystem_type(probe, mounts_text)
        status = PASS if free >= min_free_bytes else FAIL
        detail = f"{path} ({fs_type or 'unknown fs'}): {free / 2**30:.1f} GiB free"
        if status == FAIL:
            detail += f", below the {min_free_bytes / 2**30:.1f} GiB minimum"
        checks.append(Check(name, status, detail, {"path": str(path), "free_bytes": free,
                                                     "filesystem": fs_type, "overrides": overrides}))
    return checks


# --------------------------------------------------------------------------
# Model routes
# --------------------------------------------------------------------------


def _node_major(node: str) -> int | None:
    try:
        output = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.match(r"v?(\d+)", output.strip())
    return int(match.group(1)) if match else None


def _executable(path_or_name: str) -> str | None:
    if not path_or_name:
        return None
    if os.sep in path_or_name:
        return path_or_name if os.path.isfile(path_or_name) and os.access(path_or_name, os.X_OK) else None
    return shutil.which(path_or_name)


def _probe_deepseek_balance(key: str, http_get: HttpGet, timeout: float) -> Check:
    try:
        status, body = http_get(DEEPSEEK_BALANCE_URL, {"Authorization": f"Bearer {key}"}, timeout)
    except OSError as exc:
        return Check("provider", UNKNOWN, f"deepseek balance endpoint unreachable: {type(exc).__name__}")
    verdict, detail = _http_verdict(status)
    if verdict == PASS:
        try:
            payload = json.loads(body.decode("utf-8", errors="replace"))
        except ValueError:
            return Check("provider", UNKNOWN, "deepseek balance endpoint returned no JSON")
        if not isinstance(payload, dict):
            return Check("provider", UNKNOWN, "deepseek balance endpoint returned an unexpected JSON shape")
        if payload.get("is_available") is False:
            return Check("provider", FAIL, "deepseek reports is_available=false (balance exhausted)")
        infos = payload.get("balance_infos")
        # Only the documented shape proves a balance: is_available true and a list of balances.
        if payload.get("is_available") is not True or not isinstance(infos, list):
            return Check("provider", UNKNOWN, "deepseek balance response lacks is_available=true and balance_infos")
        balances = [
            f"{item.get('total_balance')} {item.get('currency')}" for item in infos if isinstance(item, dict)
        ]
        return Check("provider", PASS, "deepseek balance available: " + (", ".join(balances) or "n/a"))
    return Check("provider", verdict, f"deepseek balance: {detail}")


def _probe_openai_compatible(base_url: str, key: str, http_get: HttpGet, timeout: float) -> Check:
    url = base_url.rstrip("/") + "/models"
    try:
        status, body = http_get(url, {"Authorization": f"Bearer {key}"}, timeout)
    except OSError as exc:
        return Check("provider", UNKNOWN, f"{base_url} unreachable: {type(exc).__name__}")
    verdict, detail = _http_verdict(status)
    if verdict == PASS:
        # An OpenAI-compatible model list is an object with a "data" list; a
        # proxy's login page or an empty body is not proof the route works.
        try:
            payload = json.loads(body.decode("utf-8", errors="replace"))
        except ValueError:
            payload = None
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            return Check("provider", UNKNOWN, f"{url}: HTTP {status} but not an OpenAI-compatible model list")
    return Check("provider", verdict, f"{url}: {detail}")


def check_model(
    model_id: str,
    entry: Mapping[str, str] | None,
    factory_root: Path,
    *,
    environ: Mapping[str, str] | None = None,
    http_get: HttpGet | None = _urllib_get,
    timeout: float = 15.0,
) -> Check:
    """Decide whether one model id can be dispatched on this host now."""

    environ = os.environ if environ is None else environ
    if entry is None:
        return Check(f"model:{model_id}", FAIL, "not in the registry, or disabled")
    backend = entry.get("backend", "")
    key_env = entry.get("key_env", "")
    findings: list[Check] = []

    if backend == "codex":
        cli = environ.get("CODEX_CLI_PATH", "") or "codex"
        findings.append(Check("executable", PASS if _executable(cli) else FAIL, f"codex CLI {'found' if _executable(cli) else 'not found'}: {cli}"))
    elif backend == "claude":
        findings.append(Check("executable", PASS if _executable("claude") else FAIL, "claude CLI " + ("found" if _executable("claude") else "not found on PATH")))
    elif backend == "agy":
        script = factory_root / "scripts" / "agy_run.py"
        findings.append(Check("executable", PASS if script.is_file() else FAIL, f"{script.name} {'present' if script.is_file() else 'missing'}"))
    elif backend == "dsh":
        node = environ.get("DSH_NODE", "").strip() or (shutil.which("node") or "")
        cli = environ.get("DSH_CLI", "").strip() or (shutil.which("dsh") or "")
        major = _node_major(node) if node else None
        if not node or major is None:
            findings.append(Check("executable", FAIL, "no usable node for the DeepSeek Harness (set DSH_NODE)"))
        elif major < DSH_MIN_NODE_MAJOR:
            findings.append(Check("executable", FAIL, f"node v{major} < {DSH_MIN_NODE_MAJOR}: the launcher would exit 0 having done nothing"))
        elif not cli or not os.path.isfile(cli):
            findings.append(Check("executable", FAIL, "DeepSeek Harness launcher not found (set DSH_CLI)"))
        else:
            findings.append(Check("executable", PASS, f"node v{major}, launcher present"))
        key_env = key_env or environ.get("DSH_API_KEY_ENV", "") or "DEEPSEEK_API_KEY"
    elif backend in {"openai", "deepseek", "gemini"}:
        findings.append(Check("executable", PASS, "HTTP agent backend (no local executable)"))
    else:
        findings.append(Check("executable", FAIL, f"unknown backend {backend!r}"))

    key = environ.get(key_env, "") if key_env else ""
    if key_env:
        findings.append(Check("credential", PASS if key else FAIL,
                              f"{key_env} {'is set' if key else 'is not in the environment'}"))

    if key and http_get is not None:
        provider = (environ.get("DSH_PROVIDER", "").strip() or "deepseek-official") if backend == "dsh" else ""
        base_url = entry.get("base_url", "")
        if provider == "deepseek-official" or "api.deepseek.com" in base_url:
            findings.append(_probe_deepseek_balance(key, http_get, timeout))
        elif base_url and backend in {"openai", "deepseek"}:
            findings.append(_probe_openai_compatible(base_url, key, http_get, timeout))
        else:
            findings.append(Check("provider", UNKNOWN, f"no probe for this route ({provider or backend})"))
    elif key and http_get is None:
        findings.append(Check("provider", UNKNOWN, "network probes disabled"))

    # The worst part decides; UNKNOWN (an unverified provider) ranks below FAIL.
    status = max(findings, key=lambda check: _SEVERITY[check.status]).status
    detail = "; ".join(f"{check.name}: {check.detail}" for check in findings)
    return Check(f"model:{model_id}", status, detail, {"backend": backend, "parts": [asdict(c) for c in findings]})


def step_candidates(factory_root: Path, project_id: str, step_key: str | int, defaults: Iterable[str]) -> list[str]:
    """The candidate chain the dispatcher would try for one Step, in order."""

    from .adapters.models.dispatcher import ModelDispatcher
    from .registry import ModelBackendRegistry

    defaults = tuple(defaults)
    policy = ModelDispatcher(factory_root, ModelBackendRegistry()).policy_for(project_id, step_key, defaults or ("",))
    candidates: list[str] = []
    for model_id in (policy.primary, policy.fallback, *defaults):
        if model_id and model_id not in candidates:
            candidates.append(model_id)
    if os.getenv("CODEX_ONLY", "0").lower() in {"1", "true", "yes", "on"}:
        candidates = [model_id for model_id in candidates if _entry(factory_root, model_id) and _entry(factory_root, model_id).get("backend") == "codex"]
    from .model_allowlist import model_allowed

    # The dispatcher skips candidates outside FACTORY_MODEL_ALLOWLIST, so the
    # preflight judges only the models that would actually run.
    return [model_id for model_id in candidates if model_allowed(model_id)]


def _entry(factory_root: Path, model_id: str) -> dict[str, str] | None:
    from .adapters.models.dispatcher import ModelDispatcher
    from .registry import ModelBackendRegistry

    # The dispatcher's own resolution: builtins first, then web/model_registry.json.
    return ModelDispatcher(factory_root, ModelBackendRegistry())._entry(model_id)


def joint_modeling_enabled(project_dir: Path) -> bool | None:
    """Whether Step 2 runs on the pinned joint-modeling executor.

    Reads the latest ``JOINT_MODELING_CONFIGURED`` event from a private
    main/WAL snapshot (the engine's own reader), so a live database is seen
    as committed and nothing beside it is created.  ``None`` = undecidable.
    """

    from .state_lease import isolated_state_snapshot_ro, state_commit_lease

    database = project_dir / ".factory" / "state.db"
    if not database.is_file():
        return False
    try:
        with state_commit_lease(project_dir):
            with isolated_state_snapshot_ro(database) as connection:
                row = connection.execute(
                    "SELECT payload_json FROM events WHERE type='JOINT_MODELING_CONFIGURED' "
                    "ORDER BY revision DESC LIMIT 1"
                ).fetchone()
    except Exception:  # noqa: BLE001 - a lease, snapshot or SQLite failure is reported as undecidable
        return None
    if row is None:
        return False
    try:
        value = json.loads(row[0] or "{}").get("joint_modeling")
    except (ValueError, AttributeError):
        return None
    if not isinstance(value, dict) or type(value.get("enabled")) is not bool:
        return None
    return value["enabled"]


def _joint_executor_check() -> Check:
    from .joint_modeling_executor import executor_blocker

    blocker = executor_blocker()
    if blocker:
        return Check("model:joint-claude", FAIL, f"joint-modeling executor unavailable: {blocker}")
    return Check("model:joint-claude", PASS, "joint-modeling executor (Claude CLI) available")


def check_routes(
    factory_root: Path,
    project_id: str,
    steps: Iterable[int],
    *,
    project_dir: Path | None = None,
    environ: Mapping[str, str] | None = None,
    http_get: HttpGet | None = _urllib_get,
    timeout: float = 15.0,
) -> tuple[list[Check], list[Check]]:
    """Per-Step route verdicts plus the per-model checks they were built from.

    A Step is ``PASS`` when its first candidate passes, ``WARN`` when only a
    later candidate passes or nothing could be confirmed, and ``FAIL`` when
    every candidate fails.
    """

    from .steps.catalog import contract_for

    model_checks: dict[str, Check] = {}
    step_checks: list[Check] = []
    joint = joint_modeling_enabled(project_dir) if project_dir is not None else False
    for step in steps:
        contract = contract_for(step)
        if step == 2 and joint is not False:
            # The dispatcher bypasses the candidate chain for Step 2 when joint
            # modeling is enabled: the pinned executor is the only route.
            if joint is None:
                step_checks.append(Check(f"step:{step}:{contract.name}", WARN,
                                         "joint-modeling configuration unreadable; Step 2 route undecided"))
                continue
            executor = _joint_executor_check()
            model_checks.setdefault(executor.name, executor)
            step_checks.append(Check(f"step:{step}:{contract.name}", executor.status,
                                     f"joint modeling enabled: {executor.detail}",
                                     {"candidates": [executor.name]}))
            continue
        candidates = step_candidates(factory_root, project_id, step, contract.default_models)
        verdicts = []
        for model_id in candidates:
            if model_id not in model_checks:
                model_checks[model_id] = check_model(
                    model_id, _entry(factory_root, model_id), factory_root,
                    environ=environ, http_get=http_get, timeout=timeout,
                )
            verdicts.append((model_id, model_checks[model_id].status))
        if not verdicts:
            status, detail = (PASS, "no model call") if contract.implementation in {"delivery"} else (FAIL, "no candidate")
        elif verdicts[0][1] == PASS:
            status, detail = PASS, f"primary {verdicts[0][0]} usable"
        elif any(verdict == PASS for _, verdict in verdicts):
            usable = next(model_id for model_id, verdict in verdicts if verdict == PASS)
            status, detail = WARN, f"primary {verdicts[0][0]} is {verdicts[0][1]}; would fall back to {usable}"
        elif all(verdict == FAIL for _, verdict in verdicts):
            status, detail = FAIL, "every candidate fails: " + ", ".join(model_id for model_id, _ in verdicts)
        else:
            status, detail = WARN, "no candidate could be confirmed: " + ", ".join(f"{m}={v}" for m, v in verdicts)
        step_checks.append(Check(f"step:{step}:{contract.name}", status, detail,
                                 {"candidates": [model_id for model_id, _ in verdicts]}))
    return step_checks, list(model_checks.values())


def run_preflight(
    factory_root: Path,
    project_dir: Path | None = None,
    *,
    steps: Iterable[int] = range(17),
    min_free_bytes: int = 2 * 2**30,
    environ: Mapping[str, str] | None = None,
    http_get: HttpGet | None = _urllib_get,
    mounts_text: str | None = None,
    timeout: float = 15.0,
) -> dict:
    project_id = project_dir.name if project_dir is not None else "_default"
    scratch = check_scratch_space(factory_root, project_dir, min_free_bytes=min_free_bytes,
                                  environ=environ, mounts_text=mounts_text)
    step_checks, model_checks = check_routes(factory_root, project_id, steps, project_dir=project_dir,
                                             environ=environ, http_get=http_get, timeout=timeout)
    checks = scratch + step_checks
    overall = max((check.status for check in checks), key=lambda status: _SEVERITY[status], default=PASS)
    if overall == UNKNOWN:
        overall = WARN
    return {
        "schema": "factory-preflight-v1",
        "status": overall,
        "project": project_id,
        "checks": [asdict(check) for check in checks],
        "models": [asdict(check) for check in model_checks],
    }
