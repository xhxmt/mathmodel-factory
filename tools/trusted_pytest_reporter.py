"""Candidate-external pytest event protocol used by Phase 9 evidence runners.

The runner copies these exact tracked bytes outside the candidate import root
before starting pytest.  Pytest loads that copy explicitly while candidate
``conftest.py`` files and candidate pytest configuration are disabled.  The
terminal report remains useful human evidence, but this protocol is the
machine-authoritative account of collection and every runtest phase.
"""

from __future__ import annotations

import hashlib
import argparse
import json
import os
from pathlib import Path
import re
from typing import Sequence
import sys
import stat


TRUSTED_PYTEST_EVENT_SCHEMA = "paper-factory-trusted-pytest-events-v2"
_NONCE = re.compile(r"[0-9a-f]{32}\Z")
_sink = None
_sequence = 0
_nonce_value: str | None = None
_pytest_skip_exception: type[BaseException] | None = None
_hook_monitor_undo = None
_plugin_manager = None
_plugin_mutators: dict[str, object] = {}
_plugin_inventory_sha256: str | None = None
_event_transport: str | None = None


def _plugin_inventory(plugin_manager: object) -> str:
    """Return a process-local identity for the complete registered plugin graph."""

    rows = sorted(
        (
            str(name),
            id(plugin),
            type(plugin).__module__,
            type(plugin).__qualname__,
        )
        for name, plugin in plugin_manager.list_name_plugin()
    )
    return hashlib.sha256(
        json.dumps(rows, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _freeze_plugin_graph(plugin_manager: object) -> None:
    """Reject candidate-time plugin registration or removal.

    All core plugins and this reporter are registered before ``sessionstart``.
    Candidate test modules are imported only afterwards.  Freezing the two
    mutation primitives here prevents a collected test from installing a late
    hook that rewrites terminal outcomes or the session exit status.
    """

    global _plugin_manager, _plugin_mutators
    _plugin_manager = plugin_manager
    _plugin_mutators = {
        name: getattr(plugin_manager, name) for name in ("register", "unregister")
    }

    def reject_mutation(*args: object, **kwargs: object) -> object:
        raise RuntimeError("trusted pytest plugin graph is frozen")

    for name in _plugin_mutators:
        setattr(plugin_manager, name, reject_mutation)


def _restore_plugin_graph() -> None:
    global _plugin_manager, _plugin_mutators
    if _plugin_manager is not None:
        for name, method in _plugin_mutators.items():
            setattr(_plugin_manager, name, method)
    _plugin_manager = None
    _plugin_mutators = {}


def _emit(event: str, **fields: object) -> None:
    global _sequence
    if _sink is None:
        raise RuntimeError("trusted pytest event sink is not open")
    body = {
        "schema": TRUSTED_PYTEST_EVENT_SCHEMA,
        "nonce": _nonce_value,
        "sequence": _sequence,
        "event": event,
        **fields,
    }
    _sink.write(
        json.dumps(
            body, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        + b"\n"
    )
    _sink.flush()
    _sequence += 1


def pytest_sessionstart(session: object) -> None:
    global _sink, _sequence, _nonce_value, _hook_monitor_undo
    global _plugin_inventory_sha256, _event_transport
    nonce = os.environ.pop("PHASE9_TRUSTED_PYTEST_NONCE")
    if _NONCE.fullmatch(nonce) is None:
        raise RuntimeError("trusted pytest nonce is invalid")
    _sequence = 0
    _nonce_value = nonce
    event_fd_text = os.environ.pop("PHASE9_TRUSTED_PYTEST_EVENT_FD", None)
    event_path_text = os.environ.pop("PHASE9_TRUSTED_PYTEST_EVENT_PATH", None)
    if event_fd_text is not None:
        if (
            not event_fd_text.isdecimal()
            or int(event_fd_text) < 3
            or event_path_text != "PARENT_CAPTURED_ANONYMOUS_PIPE"
        ):
            raise RuntimeError("trusted pytest parent event channel is invalid")
        event_fd = int(event_fd_text)
        metadata = os.fstat(event_fd)
        if not stat.S_ISFIFO(metadata.st_mode):
            raise RuntimeError("trusted pytest parent event channel is not a pipe")
        # The tested process receives only the write end.  It cannot truncate or
        # replace facts already observed by the parent runner.
        _sink = os.fdopen(event_fd, "wb", buffering=0, closefd=True)
        _event_transport = "PARENT_CAPTURED_ANONYMOUS_PIPE"
    else:
        if event_path_text is None:
            raise RuntimeError("trusted pytest event channel is absent")
        event_path = Path(event_path_text)
        # Compatibility path for callers that have not yet adopted the parent
        # pipe.  Formal audit records require the pipe transport explicitly.
        _sink = event_path.open("xb")
        _event_transport = "PATH_FILE"
    _emit(
        "session_start",
        rootdir=str(session.config.rootpath),
        event_transport=_event_transport,
    )
    # Observe the call-phase object before *any* candidate hook implementation
    # can rewrite the derived TestReport.  A candidate can register a
    # tryfirst/logreport hook later, but it cannot precede this hook-call
    # monitor installed at session start.
    def before_hook(
        hook_name: str, hook_impls: object, kwargs: dict[str, object]
    ) -> None:
        global _plugin_inventory_sha256
        if hook_name == "pytest_collection":
            # FixtureManager and other pytest-owned session plugins are
            # installed by sessionstart hooks. Freeze only once those hooks
            # have all completed, immediately before collection can import any
            # candidate test module.
            if _plugin_inventory_sha256 is not None:
                raise RuntimeError("trusted pytest plugin graph froze twice")
            plugin_manager = session.config.pluginmanager
            _plugin_inventory_sha256 = _plugin_inventory(plugin_manager)
            _emit(
                "plugin_freeze",
                plugin_inventory_sha256=_plugin_inventory_sha256,
            )
            _freeze_plugin_graph(plugin_manager)
            return
        if hook_name != "pytest_runtest_makereport":
            return
        item = kwargs.get("item")
        call = kwargs.get("call")
        nodeid = getattr(item, "nodeid", None)
        when = getattr(call, "when", None)
        excinfo = getattr(call, "excinfo", None)
        if type(nodeid) is not str or when not in {"setup", "call", "teardown"}:
            raise RuntimeError("trusted pytest phase coordinate is unavailable")
        xfail_declared = bool(list(item.iter_markers(name="xfail")))
        if excinfo is None:
            outcome = "passed"
        elif (
            _pytest_skip_exception is not None
            and excinfo.errisinstance(_pytest_skip_exception)
        ):
            outcome = "skipped"
        else:
            outcome = "failed"
        _emit(
            "phase_fact",
            nodeid=nodeid,
            when=when,
            outcome=outcome,
            xfail_declared=xfail_declared,
            source="runtest_call_excinfo",
        )

    def after_hook(
        outcome: object, hook_name: str, hook_impls: object,
        kwargs: dict[str, object],
    ) -> None:
        return None

    _hook_monitor_undo = session.config.pluginmanager.add_hookcall_monitoring(
        before_hook, after_hook
    )


def pytest_collection_finish(session: object) -> None:
    _emit("collection", nodeids=[item.nodeid for item in session.items])


def pytest_warning_recorded(
    warning_message: object,
    when: str,
    nodeid: str,
    location: object,
) -> None:
    # Warning text/location can contain machine-specific or sensitive values.
    # Only the semantic occurrence and coordinate are needed for reconciliation.
    _emit("warning", when=when, nodeid=nodeid or "")


def pytest_sessionfinish(session: object, exitstatus: int) -> None:
    global _sink, _hook_monitor_undo
    final_plugin_inventory = _plugin_inventory(session.config.pluginmanager)
    _emit(
        "session_finish",
        exitstatus=int(exitstatus),
        session_exitstatus=int(session.exitstatus),
        plugin_inventory_sha256=final_plugin_inventory,
    )
    assert _sink is not None
    if _event_transport == "PATH_FILE":
        os.fsync(_sink.fileno())
    _sink.close()
    _sink = None
    if _hook_monitor_undo is not None:
        _hook_monitor_undo()
        _hook_monitor_undo = None
    _restore_plugin_graph()


def reporter_source_descriptor(raw: bytes) -> dict[str, object]:
    return {
        "schema": TRUSTED_PYTEST_EVENT_SCHEMA,
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def validate_trusted_pytest_events(
    raw: bytes,
    *,
    nonce: str,
    expected_rootdir: str,
    expected_targets: Sequence[str] = (),
    expected_nodes: Sequence[str] | None = None,
    full_repository: bool = False,
    expected_transport: str | None = None,
) -> dict[str, object]:
    """Strictly decode one trusted event stream and derive pytest outcomes.

    A successful node has exactly one passed setup, call, and teardown report.
    Skips, xfail/xpass, collection/setup/teardown errors, duplicate/missing
    reports, out-of-scope collection, and a nonzero session exit all fail.
    """

    if _NONCE.fullmatch(nonce) is None or not raw or not raw.endswith(b"\n"):
        raise ValueError("trusted pytest event stream is missing or truncated")
    events: list[dict[str, object]] = []
    for index, line in enumerate(raw.splitlines()):
        try:
            value = json.loads(line.decode("utf-8", errors="strict"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("trusted pytest event is not strict JSON") from exc
        if type(value) is not dict:
            raise ValueError("trusted pytest event is not an object")
        canonical = json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        if canonical != line:
            raise ValueError("trusted pytest event is not canonical JSON")
        if (
            value.get("schema") != TRUSTED_PYTEST_EVENT_SCHEMA
            or value.get("nonce") != nonce
            or value.get("sequence") != index
        ):
            raise ValueError("trusted pytest event identity/sequence differs")
        events.append(value)
    if len(events) < 3:
        raise ValueError("trusted pytest event stream is incomplete")
    start, finish = events[0], events[-1]
    start_keys = {
        "schema", "nonce", "sequence", "event", "rootdir",
        "event_transport",
    }
    finish_keys = {
        "schema", "nonce", "sequence", "event", "exitstatus",
        "session_exitstatus", "plugin_inventory_sha256",
    }
    if (
        set(start) != start_keys
        or start.get("event") != "session_start"
        or start.get("rootdir") != expected_rootdir
        or start.get("event_transport")
        not in {"PARENT_CAPTURED_ANONYMOUS_PIPE", "PATH_FILE"}
        or (
            expected_transport is not None
            and start.get("event_transport") != expected_transport
        )
        or set(finish) != finish_keys
        or finish.get("event") != "session_finish"
        or finish.get("exitstatus") != 0
        or finish.get("session_exitstatus") != 0
    ):
        raise ValueError("trusted pytest session boundary differs")
    freezes = [item for item in events if item.get("event") == "plugin_freeze"]
    if (
        len(freezes) != 1
        or set(freezes[0])
        != {"schema", "nonce", "sequence", "event", "plugin_inventory_sha256"}
        or type(freezes[0].get("plugin_inventory_sha256")) is not str
        or re.fullmatch(
            r"[0-9a-f]{64}", str(freezes[0]["plugin_inventory_sha256"])
        )
        is None
        or finish.get("plugin_inventory_sha256")
        != freezes[0].get("plugin_inventory_sha256")
    ):
        raise ValueError("trusted pytest plugin inventory differs")
    collections = [item for item in events if item.get("event") == "collection"]
    if len(collections) != 1 or set(collections[0]) != {
        "schema", "nonce", "sequence", "event", "nodeids"
    }:
        raise ValueError("trusted pytest collection record differs")
    nodeids = collections[0].get("nodeids")
    if (
        type(nodeids) is not list
        or not nodeids
        or any(type(node) is not str or not node for node in nodeids)
        or len(nodeids) != len(set(nodeids))
    ):
        raise ValueError("trusted pytest collection inventory differs")
    if expected_nodes is not None and nodeids != list(expected_nodes):
        raise ValueError("trusted pytest exact node inventory differs")
    modules = [str(node).split("::", 1)[0] for node in nodeids]
    if expected_nodes is None:
        targets = list(expected_targets)
        if full_repository:
            if any(not module.startswith("tests/") or not module.endswith(".py") for module in modules):
                raise ValueError("full-repository collection escaped tracked tests")
        elif (
            not targets
            or any(module not in targets for module in modules)
            or any(target not in modules for target in targets)
        ):
            raise ValueError("trusted pytest collection target coverage differs")

    reports: dict[tuple[str, str], tuple[str, object]] = {}
    warning_count = 0
    for event in events[1:-1]:
        kind = event.get("event")
        if kind in {"collection", "plugin_freeze"}:
            continue
        if kind == "warning":
            if set(event) != {
                "schema", "nonce", "sequence", "event", "when", "nodeid"
            } or type(event.get("when")) is not str or type(event.get("nodeid")) is not str:
                raise ValueError("trusted pytest warning event differs")
            warning_count += 1
            continue
        if kind != "phase_fact" or set(event) != {
            "schema", "nonce", "sequence", "event", "nodeid", "when",
            "outcome", "xfail_declared", "source",
        }:
            raise ValueError("trusted pytest event type differs")
        node = event.get("nodeid")
        when = event.get("when")
        outcome = event.get("outcome")
        xfail_declared = event.get("xfail_declared")
        if (
            node not in nodeids
            or when not in {"setup", "call", "teardown"}
            or outcome not in {"passed", "failed", "skipped"}
            or type(xfail_declared) is not bool
            or event.get("source") != "runtest_call_excinfo"
        ):
            raise ValueError("trusted pytest report coordinate differs")
        key = (str(node), str(when))
        if key in reports:
            raise ValueError("trusted pytest report is duplicated")
        reports[key] = (str(outcome), xfail_declared)

    required_phases = {"setup", "call", "teardown"}
    for node in nodeids:
        phases = {when for candidate, when in reports if candidate == node}
        if phases != required_phases:
            raise ValueError("trusted pytest node phase inventory differs")
        if any(reports[(node, phase)] != ("passed", False) for phase in required_phases):
            raise ValueError("trusted pytest contains a non-pass/xfail report")
    expected_report_count = len(nodeids) * len(required_phases)
    if len(reports) != expected_report_count:
        raise ValueError("trusted pytest report inventory differs")
    return {
        "counts": {
            "collected": len(nodeids),
            "passed": len(nodeids),
            "failed": 0,
            "errors": 0,
            "skipped": 0,
            "xfailed": 0,
            "xpassed": 0,
            "warnings": warning_count,
        },
        "node_outcomes": [
            {"test_node": node, "outcome": "PASSED"} for node in nodeids
        ],
        "session_exitstatus": 0,
        "rootdir": expected_rootdir,
        "event_transport": start["event_transport"],
        "plugin_inventory_sha256": freezes[0]["plugin_inventory_sha256"],
    }


def main(argv: Sequence[str] | None = None) -> int:
    """Start real pytest only after isolated interpreter initialization."""

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--runtime-site-packages", required=True, type=Path)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("pytest_args", nargs=argparse.REMAINDER)
    arguments = parser.parse_args(argv)
    pytest_args = list(arguments.pytest_args)
    if pytest_args and pytest_args[0] == "--":
        pytest_args.pop(0)
    source_root = arguments.source_root.resolve(strict=True)
    runtime_site_packages = arguments.runtime_site_packages.resolve(strict=True)
    runtime_prefix = runtime_site_packages.parents[2]
    requested_executable = Path(os.path.abspath(sys.executable))
    expected_site_packages = (
        runtime_prefix / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    environment_config = runtime_prefix / "pyvenv.cfg"
    if (
        requested_executable.parent.parent != runtime_prefix
        or expected_site_packages != runtime_site_packages
        or not environment_config.is_file()
        or environment_config.is_symlink()
    ):
        raise RuntimeError("trusted pytest virtual environment coordinate differs")
    # Python <=3.13 applies pyvenv.cfg in site initialization.  -S is required
    # to exclude all .pth/sitecustomize hooks, so reconstruct only the verified
    # prefix that belongs to the exact launcher and site-packages directory.
    sys.prefix = str(runtime_prefix)
    sys.exec_prefix = str(runtime_prefix)
    source_metadata = source_root.lstat()
    scripts_root = source_root / "scripts"
    if not stat.S_ISDIR(source_metadata.st_mode) or stat.S_ISLNK(source_metadata.st_mode):
        raise RuntimeError("candidate source import root is unsafe")
    scripts_present = scripts_root.exists() or scripts_root.is_symlink()
    if scripts_present:
        scripts_metadata = scripts_root.lstat()
        if (
            not stat.S_ISDIR(scripts_metadata.st_mode)
            or stat.S_ISLNK(scripts_metadata.st_mode)
            or scripts_root.resolve(strict=True).parent != source_root
        ):
            raise RuntimeError("candidate scripts import root is unsafe")
    sys.path.insert(0, str(runtime_site_packages))
    import pytest
    global _pytest_skip_exception
    _pytest_skip_exception = pytest.skip.Exception

    pytest_origin = Path(str(pytest.__file__)).resolve(strict=True)
    try:
        pytest_origin.relative_to(runtime_site_packages)
    except ValueError as exc:
        raise RuntimeError("pytest did not load from the isolated Python runtime") from exc
    # The candidate becomes importable only now: after isolated startup, real
    # pytest import, and this external reporter's import/registration.
    # ``tests/conftest.py`` historically added both of these paths for ordinary
    # imports.  We reproduce only that path contract; --noconftest means its
    # pytest hooks/configuration are never loaded.
    pre_candidate_modules = frozenset(sys.modules)
    if scripts_present:
        sys.path.insert(0, str(scripts_root))
    sys.path.insert(0, str(source_root))
    result = int(pytest.main(pytest_args, plugins=[sys.modules[__name__]]))
    candidate_top_level = {
        path.stem
        for parent in ((source_root, scripts_root) if scripts_present else (source_root,))
        for path in parent.glob("*.py")
        if path.is_file() and not path.is_symlink()
    }
    escaped = [
        (name, getattr(module, "__file__", None))
        for name, module in sys.modules.items()
        if (
            name == "factory_core"
            or name.startswith("factory_core.")
            or name.startswith("tests.")
            or name == "conftest"
            or name.startswith("test_")
            or (
                name not in pre_candidate_modules
                and name.split(".", 1)[0] in candidate_top_level
            )
        )
        and getattr(module, "__file__", None)
        and not Path(str(module.__file__)).resolve().is_relative_to(source_root)
    ]
    if escaped:
        raise RuntimeError("candidate application/test import escaped source root")
    return result


if __name__ == "__main__":
    raise SystemExit(main())
