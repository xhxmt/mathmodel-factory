"""Gate 1: the two-track harness for entry equivalence.

Gate 1 asks one question and nothing else: with the *same* code, the *same*
state and the *same* environment, does swapping the entry point

    legacy   FactoryEngine.run(max_steps=1)
    bounded  FactoryEngine.run_bounded(contract)

change the workflow's semantics?

Everything else is held fixed, including things that normally vary:

* **the absolute path.**  A single canonical project path is used for both
  tracks, and each track starts from a byte-identical restoration of one seed.
  An absolute path can leak into artifacts, so a shared path beats "equal-length
  paths" - and it is why the harness never uses ``tmp_path`` for the project.
* **the clock.**  ``SQLiteStateStore`` takes an injectable ``clock``.  Pinning it
  to a constant makes every timestamp, and therefore every ``event_id`` (which
  is hashed from ``project_id``/``revision``/``event_type``/``created_at``),
  identical across the tracks.  That is strictly stronger than normalising
  timestamps away: it lets them be compared.
* **the seed.**  Built once, hashed, snapshotted, then restored for each track.

What is left that genuinely cannot be pinned, and is therefore normalised
explicitly rather than ignored wholesale:

* ``runner_lease_id`` - ``engine.py`` generates it with ``uuid.uuid4().hex``.
* ``heartbeat_at`` - two call sites use ``time.time()`` directly rather than the
  store clock.
* ``runner_pid`` - the owning process.

One difference is *expected* rather than normalised: the bounded track's
``RUN_STARTED`` carries an extra ``bounded_run`` authorisation block.  The
harness records it separately so its presence is asserted, not tolerated
silently.

Any other difference is a finding.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import tempfile
import time as _time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable

from factory_core.storage import SQLiteStateStore
from factory_core.workflow_events import ENVELOPE_KEY, replay_events, replay_state

#: A fixed instant.  A constant clock (rather than a counter) keeps timestamps
#: equal even if the two tracks happen to read the clock a different number of
#: times, which would otherwise desynchronise everything downstream.
CONSTANT_EPOCH = 1_700_000_000


def constant_clock() -> float:
    return CONSTANT_EPOCH


@contextmanager
def frozen_time():
    """Pin the wall clock for the duration of one track.

    The injected store clock is not the only time source.  ``prompt_step.py``
    builds its own ``SQLiteStateStore(context.project_dir)`` (lines 130, 175,
    288 and 384) with the *default* clock, so the ``PROMPT_INPUT_BOUND`` event's
    ``created_at`` - and therefore its ``event_id``, which is hashed from it -
    comes from the real wall clock.

    That matters more than it sounds: the two tracks would then differ only when
    they straddle a second boundary, so on a fast machine the difference hides
    and on a loaded one it appears.  Freezing the clock makes the tracks
    genuinely time-independent instead of accidentally equal, and it is what
    lets ``heartbeat_at`` be compared exactly rather than normalised away.
    """

    import factory_core.storage as storage_module

    original_time = _time.time
    original_store = storage_module.SQLiteStateStore

    class _FrozenStore(original_store):
        """A store whose default clock is the constant, not ``time.time``.

        ``SQLiteStateStore.__init__`` declares ``clock: ... = time.time``, so the
        default is bound once at class-definition time.  Patching ``time.time``
        alone therefore does **not** reach an internally constructed store - and
        ``prompt_step.py`` re-imports ``SQLiteStateStore`` inside its methods
        (lines 130, 175, 288, 384), so replacing the module attribute does.
        """

        def __init__(self, project_dir, *, clock=constant_clock):
            super().__init__(project_dir, clock=clock)

    _time.time = constant_clock
    storage_module.SQLiteStateStore = _FrozenStore
    try:
        yield
    finally:
        _time.time = original_time
        storage_module.SQLiteStateStore = original_store


def store_at(root: Path) -> SQLiteStateStore:
    """A store pinned to the canonical clock."""

    return SQLiteStateStore(root, clock=constant_clock)


#: The canonical path.  Identical for both tracks, for every scenario, so an
#: absolute path embedded in an artifact or an event cannot differ between them.
CANARY_BASE = Path(tempfile.gettempdir()) / "pf-g1-canary"
CANARY_PROJECT = CANARY_BASE / "project"
CANARY_SEED = CANARY_BASE / "seed"

#: The values that genuinely cannot be pinned, so they are replaced before
#: comparison - and nothing else.  ``lease_id``/``runner_lease_id`` are
#: ``uuid.uuid4().hex`` (engine.py:309) and ``runner_pid`` is the owning process.
#:
#: ``heartbeat_at`` is *not* here: it comes from two direct ``time.time()`` calls
#: (engine.py:317, 504), and ``frozen_time()`` pins those, so it can be compared
#: exactly.  ``worker_pid``/``worker_identity`` are absent for the same reason -
#: both tracks run in this one process and must match.
NORMALISED_KEYS = frozenset({"runner_lease_id", "lease_id", "runner_pid"})

#: The one expected structural difference: the bounded track binds its
#: authorisation into RUN_STARTED.
BOUNDED_DIFFERENCE_KEYS = frozenset({"bounded_run", "bounded_run_id", "bounded_run_contract_sha256"})

#: Tables compared row-for-row, in addition to the event stream.
GOVERNED_TABLES = (
    "project_config",
    "contest_policy",
    "stage_checkpoints",
    "stage_checkpoint_history",
    "solver_jobs",
    "dirty_flags",
    "dirty_causes",
    "dirty_flag_clear_receipts",
    "dirty_cause_classification",
    "workflow_decision_requests",
    "workflow_decision_instances",
    "prompt_attempt_inputs",
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str)


# --------------------------------------------------------------------- the seed
def build_seed(build: Callable[[Path], None]) -> dict[str, Any]:
    """Build the seed once at the canonical path and snapshot it.

    ``build`` receives the canonical project directory and must leave it in the
    ready state both tracks will start from.
    """

    if CANARY_BASE.exists():
        shutil.rmtree(CANARY_BASE)
    CANARY_PROJECT.mkdir(parents=True)
    build(CANARY_PROJECT)

    evidence = describe_seed(CANARY_PROJECT)
    shutil.copytree(CANARY_PROJECT, CANARY_SEED, symlinks=True)
    return evidence


def describe_seed(root: Path) -> dict[str, Any]:
    """Everything that pins the seed: bytes, triple, counts and root hash."""

    database = root / ".factory" / "state.db"
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        connection.row_factory = sqlite3.Row
        project_state = dict(
            connection.execute("SELECT * FROM project_state").fetchone()
        )
        physical = connection.execute("SELECT schema_version FROM schema_info").fetchone()[0]
        counts = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in GOVERNED_TABLES
        }
        counts["events"] = connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    finally:
        connection.close()

    return {
        "state_db_sha256": _sha256(database),
        "files": file_manifest(root),
        "triple": {
            "user_version": _user_version(database),
            "physical_schema": physical,
            "project_state_schema": project_state["schema_version"],
        },
        "project_state": _normalise(project_state),
        "counts": counts,
        "aggregate_domain_root": store_at(root).aggregate_domain_root(),
    }


def _user_version(database: Path) -> int:
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        return connection.execute("PRAGMA user_version").fetchone()[0]
    finally:
        connection.close()


def file_manifest(root: Path) -> dict[str, str]:
    """sha256 of every file except the database and its journal.

    The database is compared logically (every table), which is stronger than
    comparing its bytes, and its WAL/SHM siblings are an implementation detail.
    """

    manifest: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if relative in {".factory/state.db", ".factory/state.db-wal", ".factory/state.db-shm"}:
            continue
        manifest[relative] = _sha256(path)
    return manifest


def restore_seed() -> Path:
    """Put a byte-identical copy of the seed back at the canonical path."""

    if CANARY_PROJECT.exists():
        shutil.rmtree(CANARY_PROJECT)
    shutil.copytree(CANARY_SEED, CANARY_PROJECT, symlinks=True)
    return CANARY_PROJECT


# ------------------------------------------------------------------ collection
def _normalise(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: (
                "<normalised>"
                if key in NORMALISED_KEYS
                else _normalise(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_normalise(item) for item in value]
    return value


def collect(root: Path) -> dict[str, Any]:
    """The full semantic closure of a finished track."""

    store = store_at(root)
    database = root / ".factory" / "state.db"
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        connection.row_factory = sqlite3.Row
        project_state = dict(connection.execute("SELECT * FROM project_state").fetchone())
        physical = connection.execute("SELECT schema_version FROM schema_info").fetchone()[0]
        tables = {
            table: [
                dict(row)
                for row in connection.execute(
                    f"SELECT * FROM {table} ORDER BY 1, 2"
                    if _column_count(connection, table) >= 2
                    else f"SELECT * FROM {table} ORDER BY 1"
                ).fetchall()
            ]
            for table in GOVERNED_TABLES
        }
    finally:
        connection.close()

    snapshot = store.status_snapshot()
    events = store.events()
    envelopes = [
        event.payload[ENVELOPE_KEY]
        for event in events
        if isinstance(event.payload.get(ENVELOPE_KEY), dict)
    ]

    return {
        "triple": {
            "user_version": _user_version(database),
            "physical_schema": physical,
            "project_state_schema": project_state["schema_version"],
        },
        "project_state": _normalise(project_state),
        # the replay contract itself, field by field
        "replay_state": replay_state(snapshot["state"]),
        "replayed_matches_state": (
            replay_events(snapshot["events"]) == replay_state(snapshot["state"])
        ),
        "event_replay_valid": snapshot["event_replay_valid"],
        "aggregate_valid": snapshot["aggregate_valid"],
        # every recorded state hash, in order: the strongest per-event assertion
        "state_hashes": [envelope.get("state_hash_after") for envelope in envelopes],
        "events": [
            {
                "revision": event.revision,
                "type": event.type,
                "step": event.step,
                "attempt": event.attempt,
                "payload": _normalise(event.payload),
            }
            for event in events
        ],
        # exposed deliberately: a non-injected time source shows up here rather
        # than only as a mystifying event_id mismatch
        "event_created_at": [event.created_at for event in events],
        "tables": _normalise(tables),
        "aggregate_domain_root": store.aggregate_domain_root(),
        "files": file_manifest(root),
    }


def _column_count(connection: sqlite3.Connection, table: str) -> int:
    return len(connection.execute(f"PRAGMA table_info({table})").fetchall())


# ------------------------------------------------------------------ comparison
def _walk_diff(left: Any, right: Any, path: str = "") -> list[str]:
    """Every leaf that differs, addressed by path."""

    if isinstance(left, dict) and isinstance(right, dict):
        differences: list[str] = []
        for key in sorted(set(left) | set(right)):
            child = f"{path}.{key}" if path else str(key)
            if key not in left:
                differences.append(f"{child}: only in bounded")
            elif key not in right:
                differences.append(f"{child}: only in legacy")
            else:
                differences.extend(_walk_diff(left[key], right[key], child))
        return differences
    if isinstance(left, list) and isinstance(right, list):
        if len(left) != len(right):
            return [f"{path}: length {len(left)} != {len(right)}"]
        differences = []
        for index, (a, b) in enumerate(zip(left, right)):
            differences.extend(_walk_diff(a, b, f"{path}[{index}]"))
        return differences
    if left != right:
        return [f"{path}: {_short(left)} != {_short(right)}"]
    return []


def _short(value: Any, limit: int = 60) -> str:
    text = _canonical(value)
    return text if len(text) <= limit else text[:limit] + "..."


def compare(legacy: dict[str, Any], bounded: dict[str, Any]) -> dict[str, list[str]]:
    """Differences by area, with the one expected difference separated out."""

    areas = {
        "triple": (legacy["triple"], bounded["triple"]),
        "project_state": (legacy["project_state"], bounded["project_state"]),
        "replay_state": (legacy["replay_state"], bounded["replay_state"]),
        "state_hashes": (legacy["state_hashes"], bounded["state_hashes"]),
        "event_created_at": (legacy["event_created_at"], bounded["event_created_at"]),
        "tables": (legacy["tables"], bounded["tables"]),
        "aggregate_domain_root": (
            legacy["aggregate_domain_root"],
            bounded["aggregate_domain_root"],
        ),
        "files": (legacy["files"], bounded["files"]),
    }
    findings = {
        area: _walk_diff(left, right, area) for area, (left, right) in areas.items()
    }

    # events are compared structurally, with the bounded authorisation lifted out
    findings["events"] = []
    legacy_events = legacy["events"]
    bounded_events = bounded["events"]
    if len(legacy_events) != len(bounded_events):
        findings["events"].append(
            f"events: {len(legacy_events)} != {len(bounded_events)}"
        )
    else:
        for index, (a, b) in enumerate(zip(legacy_events, bounded_events)):
            # stripped from *both* sides so the comparator is symmetric: it answers
            # "are these two runs equivalent apart from the authorisation block",
            # which is also the right question when both sides are bounded.  That
            # the block is present where it should be is asserted separately by
            # bounded_authorisation(), not tolerated here.
            findings["events"].extend(
                _walk_diff(
                    {**a, "payload": _strip_bounded_block(a["payload"])},
                    {**b, "payload": _strip_bounded_block(b["payload"])},
                    f"events[{index}]",
                )
            )
    return findings


def _strip_bounded_block(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if key not in BOUNDED_DIFFERENCE_KEYS}


def bounded_authorisation(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The ``bounded_run`` bindings found in the bounded track's events."""

    found = []
    for event in events:
        block = event["payload"].get("bounded_run")
        if isinstance(block, dict):
            found.append({"revision": event["revision"], "type": event["type"], "block": block})
    return found


def fixture_digest(evidence: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(evidence).encode("utf-8")).hexdigest()
