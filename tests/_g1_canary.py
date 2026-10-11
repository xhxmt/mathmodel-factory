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

from factory_core.domain import ExecutionResult, ValidationResult, WorkflowStatus
from factory_core.registry import StepRegistry
from factory_core.stages import STAGE_SCHEDULER_GENERATION
from factory_core.steps import build_native_registry
from factory_core.steps.catalog import STEP_CONTRACTS
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


#: The canary's own internal-consistency answers.
INTEGRITY_KEYS = ("event_replay_valid", "aggregate_valid", "replayed_matches_state")


def integrity_of(collected: dict) -> dict:
    """The three consistency answers, as one comparable group.

    They were recorded and never compared, so both tracks could be internally
    broken - a stream that does not replay, an aggregate root that does not
    verify - while every equivalence assertion passed.  "These two runs agree" is
    worth little if neither is coherent.
    """

    return {key: collected.get(key) for key in INTEGRITY_KEYS}


def assert_integrity(collected: dict) -> None:
    """Require a track to be internally valid, not merely equal to the other."""

    problems = [
        f"{key}={value!r}"
        for key, value in integrity_of(collected).items()
        if value is not True
    ]
    assert not problems, "the canary's own integrity checks failed: " + ", ".join(problems)


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
        # compared as well as asserted, so a track that is valid in one run and
        # invalid in the other is a finding rather than a silence
        "integrity": (integrity_of(legacy), integrity_of(bounded)),
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

# =============================================== shared hermetic Stage fixtures
#: The hermetic *factory root* (prompt templates), deliberately outside
#: CANARY_BASE because build_seed() clears that directory.
FACTORY_ROOT = Path(tempfile.gettempdir()) / "pf-g1-factory"

#: The durable local solver job the solver scenario seeds.
SOLVER_JOB_ID = "local_python_g1_canary_0001"


#: Prompt templates the registry asks for that are not Step contract prompts.
_EXTRA_PROMPTS = (
    "step2_modeling_proposal.txt",
    "step2_modeling_critic.txt",
    "step8_5_reviewer_entry.txt",
    "execution_auditor.txt",
    "math_auditor.txt",
    "paper_reviewer.txt",
)


def ensure_factory() -> Path:
    """A factory root carrying the real prompt template names.

    ``PromptRenderer`` resolves ``contract.prompt`` verbatim under
    ``<root>/prompts``, so the fixture must use the real file names rather than
    a synthesised scheme - otherwise it would exercise a template layout the
    product does not have.
    """

    prompts = FACTORY_ROOT / "prompts"
    prompts.mkdir(parents=True, exist_ok=True)
    for contract in STEP_CONTRACTS:
        if contract.prompt is not None:
            (prompts / contract.prompt).write_text(
                "hermetic prompt for __BASE_NAME__ at __PROJECT_PATH__\n",
                encoding="utf-8",
            )
    # build_native_registry also registers Stage subtasks whose prompts are not
    # Step contracts (step 8.5's reviewer entry gate, and step 2's two proposal
    # prompts), and the judge prompts live outside the catalogue too.  Names
    # taken from the templates factory_core/steps and stages.py actually ask for.
    for extra in _EXTRA_PROMPTS:
        (prompts / extra).write_text(
            "hermetic prompt for __BASE_NAME__ at __PROJECT_PATH__\n",
            encoding="utf-8",
        )
    return FACTORY_ROOT


class HermeticDispatcher:
    """Stands in for the model backend: deterministic, no external call.

    ``on_execute`` lets a scenario act *during* the Step - which is how the
    protected-manifest pre-commit check is reached without a real model.
    """

    def __init__(self, on_execute=None):
        self._on_execute = on_execute
        self.calls = 0

    def execute(self, request, **kwargs):
        self.calls += 1
        if self._on_execute is not None:
            self._on_execute(request)
        return ExecutionResult.succeeded(model_id="hermetic")


class HermeticValidator:
    """Accepts whatever the hermetic dispatcher produced."""

    def validate(self, context):
        return ValidationResult.valid("artifact")


def stage_registry(*, dispatcher=None) -> StepRegistry:
    """The production registry shape, with the two side-effect seams replaced.

    ``build_native_registry`` is the real builder: every Step contract, its real
    prompt, the real Stage subtask routing and the real checkpoint machinery.
    Only the dispatcher (no model calls) and the validator (no real artifact
    parsing) are hermetic.  That is the point of the injection points - the
    Stage paths the simplification touches stay on the execution path.
    """

    return build_native_registry(
        ensure_factory(),
        dispatcher=dispatcher or HermeticDispatcher(),
        validator_factory=lambda _root, _step: HermeticValidator(),
    )


def stage_seed(root: Path) -> None:
    """A fresh Stage v1 project, ready to advance its first subtask."""

    store_at(root).initialize(
        project_id="g1-canary",
        project_type="modeling",
        scheduler_generation=STAGE_SCHEDULER_GENERATION,
    )


def paused_seed(root: Path) -> None:
    """A project that has genuinely stopped: no entry can advance it.

    The honest construction for a repeated-boundary case, because a run that
    *did* advance shifts the revision, and ``boundary_fingerprint`` covers the
    revision - so two advancing runs can never report an unchanged boundary.
    """

    store = store_at(root)
    state = store.initialize(project_id="g1-canary", project_type="modeling")
    store.transition(
        expected_revision=state.revision,
        event_type="PAUSED",
        changes={"status": WorkflowStatus.PAUSED},
        payload={"reason": {"code": "PAUSED", "subcode": "OPERATOR", "actor": "operator"}},
    )


def stage_seed_ready_for_step(root: Path, completed_step: int) -> None:
    """A Stage v1 project whose next subtask is ``completed_step + 1``.

    Drivers hard-code the step they were written for; seeds express the same
    position without the rotted revision.
    """

    store_at(root).initialize(
        project_id="g1-canary",
        project_type="modeling",
        scheduler_generation=STAGE_SCHEDULER_GENERATION,
        last_completed_step=completed_step,
    )


def stage_seed_ready_for_step_with_protected_file(root: Path, completed_step: int) -> None:
    stage_seed_ready_for_step(root, completed_step)
    (root / PROTECTED_FILE).write_text(PROTECTED_CONTENT, encoding="utf-8")


def stage_seed_at_step_5(root: Path) -> None:
    """A Stage v1 project whose next subtask is source step 5 (Stage 4, solve).

    The state the first Gate 4 driver hard-coded as an assertion
    (``status == 'ready' and active_step == 5``), expressed as a seed instead so
    the migration can be proved against it after the original project completed
    and the assertion rotted.
    """

    store_at(root).initialize(
        project_id="g1-canary",
        project_type="modeling",
        scheduler_generation=STAGE_SCHEDULER_GENERATION,
        last_completed_step=4,
    )


def stage_seed_at_step_5_with_protected_file(root: Path) -> None:
    stage_seed_at_step_5(root)
    (root / PROTECTED_FILE).write_text(PROTECTED_CONTENT, encoding="utf-8")


def stage_seed_with_solver(root: Path) -> None:
    """A Stage project whose next Step has a durable local solver job.

    ``last_completed_step=4`` puts the cursor on Stage 4 / subtask ``solve`` /
    source step 5, and the job carries that same slot, so the run's dirty
    classification consults a receipt with an owner to resolve rather than an
    empty table.
    """

    store = store_at(root)
    state = store.initialize(
        project_id="g1-canary",
        project_type="modeling",
        scheduler_generation=STAGE_SCHEDULER_GENERATION,
        last_completed_step=4,
    )
    store.create_solver_job(
        expected_revision=state.revision,
        record={
            "job_id": SOLVER_JOB_ID,
            "owner_stage": 4,
            "owner_subtask": "solve",
            "backend": "local",
            "runtime": "python",
            "script": "models/m1/05_solve.py",
            "workdir": "models/m1",
            "argv": [],
            "max_time_seconds": 60,
            "status": "completed",
            "result_refs": {},
        },
    )

    jobs = root / ".factory" / "solver_jobs"
    jobs.mkdir(parents=True, exist_ok=True)
    (jobs / f"{SOLVER_JOB_ID}.json").write_text(
        '{"status": "completed", "returncode": 0, "finished_at": 1700000000}\n',
        encoding="utf-8",
    )
    receipts = root / ".factory" / "solver_receipts"
    receipts.mkdir(parents=True, exist_ok=True)
    (receipts / f"{SOLVER_JOB_ID}.completed.json").write_text(
        '{"job_id": "%s", "schema_version": "factory-solver-receipt-v1"}\n' % SOLVER_JOB_ID,
        encoding="utf-8",
    )


def stage_seed_with_dirty(root: Path) -> None:
    """A Stage project carrying a live dirty obligation.

    A clean one-subtask advance leaves the dirty tables empty, so comparing them
    would prove nothing - the "equal because both are empty" failure mode.
    """

    from factory_core.current_dirty import classifier_contract_sha256

    store = store_at(root)
    state = store.initialize(
        project_id="g1-canary",
        project_type="modeling",
        scheduler_generation=STAGE_SCHEDULER_GENERATION,
    )
    store.transition(
        expected_revision=state.revision,
        event_type="MATH_CHANGED_FOR_TEST",
        changes={},
        dirty_changes=[
            {
                "flag": "MATH_DIRTY",
                "owner_stage": 8,
                "cause_artifact": "canary_paper.tex",
                "baseline_fingerprint": "a" * 64,
                "current_fingerprint": "b" * 64,
                "classifier_contract_sha256": classifier_contract_sha256(),
            }
        ],
    )


#: A file the protected-manifest scenarios guard.  Seeded at a known content so
#: the contract can hold its digest and a run can then be made to break it.
PROTECTED_FILE = "canary_protected.txt"
PROTECTED_CONTENT = "sealed content\n"


def stage_seed_with_protected_file(root: Path) -> None:
    """A Stage v1 project plus a file a contract may declare protected."""

    stage_seed(root)
    (root / PROTECTED_FILE).write_text(PROTECTED_CONTENT, encoding="utf-8")


def protected_digest(content: str = PROTECTED_CONTENT) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()
