"""Gate 3: version and history compatibility.

Gate 1 and Gate 2 ran on projects the *current* code created, so they never
touched the thing the simplification actually changes about old data: the
schema generation.  Gate 3 is where the cross-version contracts live.

The fixtures are generated, not borrowed.  ``bde49712`` is the last commit whose
``SCHEMA_VERSION`` is still 9, so extracting its ``factory_core`` gives a
genuine *old interpreter*: the same venv runs it with ``PYTHONPATH`` pointed at
the extracted tree, and from a neutral working directory so the repository's own
``factory_core`` cannot shadow it.  That makes a real v9 database and a real
downgrade attempt reproducible in CI rather than only on the machine that has
the production projects.

What is asserted:

* G3.1 a read migrates the *physical* schema and leaves the event-stream
  generation alone, which is the documented split in ``storage.py``
* G3.2 I8-a: a v9 database that is structurally incomplete is silently repaired
  and promoted rather than refused - the gap, characterised
* G3.3 I8-b: the production v9 databases are audited read-only, and migrated
  only in a copy
* G3.4 the real ``bde49712`` code refuses a schema-10 database
* G3.5 v9 -> 10 -> old interpreter fails closed, with nothing written
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

import pytest

from factory_core.domain import SchemaPreconditionError
from factory_core.storage import (
    V9_REQUIRED_COLUMNS,
    V9_REQUIRED_TABLES,
    SQLiteStateStore,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The last commit whose SCHEMA_VERSION is 9.  Running its code is the only
#: honest way to test the downgrade barrier: a simulation would be testing the
#: simulation.
OLD_CODE_COMMIT = "bde49712"
OLD_CODE_ROOT = Path(tempfile.gettempdir()) / "pf-g3-old-code"

#: The non-terminal v9 specimen preserved outside the ongoing/ storage roots.
SPECIMEN = Path(
    "/home/tfisher/pf-canary-staging/cumcm_2025_b_luna_usability_20260906t063231z"
)
SPECIMEN_SHA256 = "e7c3e25582acf0f247021ceb824a2ec26b87510c1cdf1928d3c0b51d076eac8f"

#: Every production state database, with the physical generation measured on
#: 2026-10-07.  Three are still v9; the other three were already migrated.
PRODUCTION_DATABASES = {
    "cumcm_2020_a_codex_luna": (
        "/home/tfisher/paper_factory/ongoing/cumcm_2020_a_codex_luna/.factory/state.db",
        9,
    ),
    "stability_run2": (
        "/home/tfisher/paper_factory/.worktrees/cumcm-2025b-stability-run2-4d2eb32/"
        "ongoing/cumcm_2025_b_codex_luna_stability_20260817/.factory/state.db",
        9,
    ),
    "stability_run3": (
        "/home/tfisher/paper_factory/.worktrees/cumcm-2025b-stability-run2-4d2eb32/"
        "ongoing/cumcm_2025_b_codex_luna_stability_20260817_run3/.factory/state.db",
        9,
    ),
    "stability_run4": (
        "/home/tfisher/paper_factory/.worktrees/cumcm-2025b-stability-run2-4d2eb32/"
        "ongoing/cumcm_2025_b_codex_luna_stability_20260817_run4/.factory/state.db",
        10,
    ),
    "formal_2025b": (
        "/home/tfisher/paper_factory/ongoing/"
        "cumcm_2025_b_gpt_formal_20260908t153023z/.factory/state.db",
        10,
    ),
    "cumcm_2026_a": (
        "/home/tfisher/paper_factory/ongoing/"
        "cumcm_2026_a_fable_pro_20260910/.factory/state.db",
        10,
    ),
}

_V10_SIDE_TABLE = "dirty_cause_classification"


# --------------------------------------------------------------- old interpreter
def old_code_root() -> Path:
    """bde49712's ``factory_core`` and ``scripts``, extracted once.

    ``scripts`` is archived with it because the old ``factory_core`` imports from
    that package in two dozen places - ``scripts.model_dispatch_config`` from both
    the model dispatcher and the effective-prompt layer, among others - and
    ``run_with_old_code`` puts only this directory on ``PYTHONPATH``.  Extracting
    ``factory_core`` alone therefore works for the snippets that exist today, none
    of which reaches those modules, and fails with a bare
    ``ModuleNotFoundError: No module named 'scripts'`` the moment one does.  The
    repository root cannot simply be added to ``PYTHONPATH`` instead: that would
    import *this* branch's ``factory_core`` and the fixture would be built by the
    new code, which is the one thing the fixture exists to avoid.
    """

    if (OLD_CODE_ROOT / "factory_core" / "domain.py").is_file() and (
        OLD_CODE_ROOT / "scripts"
    ).is_dir():
        return OLD_CODE_ROOT

    OLD_CODE_ROOT.mkdir(parents=True, exist_ok=True)
    archive = OLD_CODE_ROOT.with_suffix(".tar")
    try:
        with archive.open("wb") as handle:
            subprocess.run(
                ["git", "archive", OLD_CODE_COMMIT, "factory_core", "scripts"],
                cwd=REPO_ROOT,
                stdout=handle,
                stderr=subprocess.PIPE,
                check=True,
                timeout=120,
            )
        with tarfile.open(archive) as bundle:
            # explicit filter: the default becomes "data" in 3.14, and this
            # archive is our own git output either way
            bundle.extractall(OLD_CODE_ROOT, filter="data")
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        pytest.skip(
            f"cannot extract {OLD_CODE_COMMIT} (needs full history): {exc}"
        )
    finally:
        archive.unlink(missing_ok=True)

    if not (OLD_CODE_ROOT / "factory_core" / "domain.py").is_file():
        pytest.skip(f"{OLD_CODE_COMMIT} extraction produced no factory_core")
    if not (OLD_CODE_ROOT / "scripts").is_dir():
        # `git archive` omits a path the commit does not have, so this only fires
        # if the extraction is incomplete rather than if scripts/ never existed.
        pytest.skip(f"{OLD_CODE_COMMIT} extraction produced no scripts package")
    return OLD_CODE_ROOT


def run_with_old_code(source: str, *, expect_success: bool = True) -> str:
    """Run a snippet against bde49712's code.

    ``cwd`` is deliberately the temp directory, not the repository: for ``-c``
    the interpreter puts the working directory first on ``sys.path``, so running
    from the repository would import *this* branch's ``factory_core`` and the
    test would silently be asserting against the new code.
    """

    environment = {**os.environ, "PYTHONPATH": str(old_code_root())}
    completed = subprocess.run(
        [sys.executable, "-c", source],
        cwd=tempfile.gettempdir(),
        env=environment,
        capture_output=True,
        text=True,
        timeout=180,
    )
    if expect_success and completed.returncode != 0:
        raise AssertionError(
            f"old-code run failed ({completed.returncode}):\n"
            f"{completed.stdout}\n{completed.stderr}"
        )
    return completed.stdout + completed.stderr


def make_v9_project(root: Path, *, project_id: str = "v9-fixture") -> Path:
    """A genuine v9 database plus project, built by bde49712's code."""

    root.mkdir(parents=True, exist_ok=True)
    output = run_with_old_code(
        "from factory_core.storage import SQLiteStateStore\n"
        f"st = SQLiteStateStore({str(root)!r})\n"
        f"st.initialize(project_id={project_id!r}, project_type='modeling')\n"
        "print('created')\n"
    )
    assert "created" in output
    return root


# -------------------------------------------------------------------- raw reads
def raw_facts(database: Path) -> dict:
    """Everything readable without touching Factory code - no migration."""

    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        return {
            "user_version": connection.execute("PRAGMA user_version").fetchone()[0],
            "physical_schema": connection.execute(
                "SELECT schema_version FROM schema_info"
            ).fetchone()[0],
            "state_schema": connection.execute(
                "SELECT schema_version FROM project_state"
            ).fetchone()[0],
            "revision": connection.execute(
                "SELECT revision FROM project_state"
            ).fetchone()[0],
            "events": connection.execute("SELECT COUNT(*) FROM events").fetchone()[0],
            "has_v10_side_table": _V10_SIDE_TABLE in tables,
        }
    finally:
        connection.close()


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def enveloped_events(database: Path) -> int:
    """How many events carry a versioned replay envelope.

    Raw SQLite only: asking the store would migrate the database being measured.
    A stream with none predates the versioned generation, so the replay and
    aggregate-root contracts are not applicable to it - which is a different
    statement from "it failed validation", and the audit must not conflate them.
    """

    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        total = 0
        for (payload,) in connection.execute("SELECT payload_json FROM events"):
            try:
                decoded = json.loads(payload)
            except (TypeError, ValueError):
                continue
            if isinstance(decoded.get("_workflow"), dict):
                total += 1
        return total
    finally:
        connection.close()


# ================================================== G3.1 read-triggered upgrade
def test_v9_is_created_by_the_old_code_and_reads_as_v9(tmp_path):
    """The fixture's premise: bde49712 really does make a v9 database."""

    root = make_v9_project(tmp_path / "v9")
    facts = raw_facts(root / ".factory" / "state.db")

    assert facts["physical_schema"] == 9
    assert facts["state_schema"] == 9
    assert facts["has_v10_side_table"] is False
    assert facts["events"] == 1, "initialize writes exactly one event"


def test_a_read_migrates_the_physical_schema_and_not_the_event_stream(tmp_path):
    """The documented split, asserted on a real v9 database.

    ``schema_info.schema_version`` is the physical DDL generation;
    ``project_state.schema_version`` is the generation recorded in the event
    stream.  A read may move the first and must not move the second, because the
    second is a replay field bound into every event's ``state_hash_after``.
    """

    root = make_v9_project(tmp_path / "v9")
    database = root / ".factory" / "state.db"
    before = raw_facts(database)

    SQLiteStateStore(root).load()  # a pure read

    after = raw_facts(database)
    assert after["physical_schema"] == 10, "the read migrated the physical DDL"
    assert after["state_schema"] == 9, "the event-stream generation must not move"
    assert after["revision"] == before["revision"]
    assert after["events"] == before["events"]
    assert after["user_version"] == before["user_version"] == 0
    assert before["has_v10_side_table"] is False
    assert after["has_v10_side_table"] is True, "the v10 side table was created"


@pytest.mark.parametrize("read", ["load", "status_snapshot", "events", "dirty_flags"])
def test_every_read_path_triggers_the_migration(tmp_path, read):
    """Not just load(): the store's other read entry points migrate too.

    This is why a v9 database must be copied before *any* inspection - there is
    no read-only way in through this API.
    """

    root = make_v9_project(tmp_path / "v9")
    database = root / ".factory" / "state.db"
    assert raw_facts(database)["physical_schema"] == 9

    store = SQLiteStateStore(root)
    getattr(store, read)()

    assert raw_facts(database)["physical_schema"] == 10


def test_the_first_genuine_write_converges_the_state_generation(tmp_path):
    """Only an event-carrying write may move the replay generation."""

    root = make_v9_project(tmp_path / "v9")
    database = root / ".factory" / "state.db"

    store = SQLiteStateStore(root)
    state = store.load()
    assert raw_facts(database)["state_schema"] == 9, "the read left it alone"

    store.transition(expected_revision=state.revision, event_type="CONVERGE_FOR_TEST", changes={})

    after = raw_facts(database)
    assert after["state_schema"] == 10, "a genuine write converges it"
    assert after["physical_schema"] == 10
    assert after["revision"] == state.revision + 1


# ===================================================== G3.2 I8-a: the contract
def _drop_table(database: Path, table: str) -> None:
    connection = sqlite3.connect(database)
    try:
        connection.execute(f"DROP TABLE {table}")
        connection.commit()
    finally:
        connection.close()


def test_a_v9_database_missing_a_required_table_is_refused_before_any_ddl(tmp_path):
    """I8-a.  This test is the inversion of the gap it used to characterise.

    Before the pre-validator landed, a generation-9 database missing one of its
    own tables was silently repaired and promoted: the migration recreated the
    table, bumped ``schema_info``, and the caller never learned that the database
    it opened was not what it claimed to be.

    Now it is refused - and refused *before* ``BEGIN IMMEDIATE``, so the file is
    byte-identical afterwards.  That ordering is the whole point: a refusal that
    had already rewritten the schema would not be a refusal.
    """

    root = make_v9_project(tmp_path / "v9")
    database = root / ".factory" / "state.db"
    _drop_table(database, "stage_checkpoint_history")

    before = raw_facts(database)
    before_sha = sha256(database)
    assert before["physical_schema"] == 9

    with pytest.raises(SchemaPreconditionError, match="missing table stage_checkpoint_history"):
        SQLiteStateStore(root).load()

    after = raw_facts(database)
    assert after["physical_schema"] == 9, "the generation must not be promoted"
    assert after == before, "no raw fact may move"
    assert sha256(database) == before_sha, "the refusal must not have written anything"

    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
    finally:
        connection.close()
    assert "stage_checkpoint_history" not in tables, "it must not be recreated"


def test_a_v9_database_missing_a_required_column_is_refused_before_any_ddl(tmp_path):
    """The column half of the contract, asserted the same way."""

    root = make_v9_project(tmp_path / "v9")
    database = root / ".factory" / "state.db"

    # SQLite cannot drop a column before 3.35, so rebuild the table without it
    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("ALTER TABLE events RENAME TO events_old")
        connection.execute(
            "CREATE TABLE events (revision INTEGER PRIMARY KEY, type TEXT NOT NULL)"
        )
        connection.execute("DROP TABLE events_old")
        connection.commit()
    finally:
        connection.close()

    before = raw_facts(database)
    before_sha = sha256(database)

    with pytest.raises(SchemaPreconditionError, match="events is missing column"):
        SQLiteStateStore(root).load()

    assert raw_facts(database) == before
    assert raw_facts(database)["physical_schema"] == 9
    assert sha256(database) == before_sha


# ============================== the baseline is pinned to the real v9 generator
def test_the_v9_contract_matches_what_the_old_code_actually_creates(tmp_path):
    """An oracle, so the contract cannot drift away from generation 9.

    ``bde49712`` is the last generation-9 implementation and is immutable, so its
    fixture is the authority on what generation 9 *is*.  The table set is pinned
    exactly; the required columns are pinned as a subset, because the contract
    deliberately does not freeze every declared type and index.

    This is what removes the need to maintain per-generation schema documents:
    there is one upgrade boundary that matters, and this test holds it against
    the only implementation that ever produced it.
    """

    root = make_v9_project(tmp_path / "v9")
    database = root / ".factory" / "state.db"
    assert raw_facts(database)["physical_schema"] == 9

    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        actual_tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        actual_columns = {
            table: {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
            for table in V9_REQUIRED_COLUMNS
        }
    finally:
        connection.close()

    assert set(V9_REQUIRED_TABLES) == actual_tables, (
        "the v9 contract no longer matches what generation 9 actually created"
    )
    for table, required in V9_REQUIRED_COLUMNS.items():
        assert required <= actual_columns[table], (
            f"{table}: the contract requires columns generation 9 never had"
        )

    # and the validator accepts it, which is the property that matters
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        SQLiteStateStore._validate_v9_pre_upgrade_schema(connection)
    finally:
        connection.close()


# ============================================== G3.4 the real downgrade barrier
def test_the_old_code_refuses_a_schema_10_database(tmp_path):
    """Run bde49712's code against a database this branch created."""

    root = tmp_path / "new"
    root.mkdir()
    SQLiteStateStore(root).initialize(project_id="new-fixture", project_type="modeling")
    database = root / ".factory" / "state.db"
    assert raw_facts(database)["physical_schema"] == 10

    before = sha256(database)
    output = run_with_old_code(
        "from factory_core.storage import SQLiteStateStore\n"
        f"SQLiteStateStore({str(root)!r}).load()\n"
        "print('NO REFUSAL')\n",
        expect_success=False,
    )

    assert "NO REFUSAL" not in output
    assert "unsupported workflow schema 10" in output
    assert sha256(database) == before, "the refusal wrote nothing"


def test_the_old_interpreter_is_really_the_old_schema_version():
    """Guard: if this ever reads 10 the barrier test above is vacuous."""

    output = run_with_old_code(
        "from factory_core import domain\nprint('SCHEMA_VERSION', domain.SCHEMA_VERSION)\n"
    )
    assert "SCHEMA_VERSION 9" in output, output


# ============================================== G3.5 v9 -> 10 -> old interpreter
def test_v9_migrated_then_opened_by_the_old_code_fails_closed(tmp_path):
    """The full sequence, with the promise that nothing was written."""

    root = make_v9_project(tmp_path / "v9")
    database = root / ".factory" / "state.db"

    SQLiteStateStore(root).load()  # new code migrates the physical schema
    migrated = raw_facts(database)
    assert migrated["physical_schema"] == 10
    assert migrated["state_schema"] == 9

    before = sha256(database)
    output = run_with_old_code(
        "from factory_core.storage import SQLiteStateStore\n"
        f"SQLiteStateStore({str(root)!r}).load()\n"
        "print('NO REFUSAL')\n",
        expect_success=False,
    )

    assert "unsupported workflow schema 10" in output
    assert sha256(database) == before
    assert raw_facts(database) == migrated, "no business state moved either"


# ======================================================= the preserved specimen
def test_the_preserved_v9_specimen_still_matches_its_recorded_digest():
    """If the specimen were touched, every Gate 3 conclusion drawn from it dies."""

    if not SPECIMEN.is_dir():
        pytest.skip(f"specimen unavailable at {SPECIMEN}")
    assert sha256(SPECIMEN / ".factory" / "state.db") == SPECIMEN_SHA256


def test_the_specimen_migrates_the_same_way_on_a_copy(tmp_path):
    """A real, non-terminal v9 project - migrated only in a copy."""

    if not SPECIMEN.is_dir():
        pytest.skip(f"specimen unavailable at {SPECIMEN}")

    copy = tmp_path / "specimen"
    shutil.copytree(SPECIMEN, copy, symlinks=True)
    database = copy / ".factory" / "state.db"
    before = raw_facts(database)

    assert before["physical_schema"] == 9
    assert before["state_schema"] == 9
    assert before["events"] == 404

    SQLiteStateStore(copy).load()

    after = raw_facts(database)
    assert after["physical_schema"] == 10
    assert after["state_schema"] == 9
    assert after["events"] == before["events"]
    assert after["revision"] == before["revision"]

    # and the staging copy itself was not touched
    assert sha256(SPECIMEN / ".factory" / "state.db") == SPECIMEN_SHA256


# ============================================ G3.3 I8-b: the production audit
@pytest.mark.parametrize("name", sorted(PRODUCTION_DATABASES))
def test_a_production_database_is_audited_read_only_and_migrated_in_a_copy(tmp_path, name):
    """I8-b.  The original is read with raw SQLite and never with Factory code.

    ``_upgrade_schema`` is reached by ``load()``, ``status_snapshot()``,
    ``events()`` and ``dirty_flags()``, so *any* Factory read of a v9 database
    migrates it in place.  An audit that used Factory code to inspect the
    original would therefore destroy the thing it was measuring - which is why
    the order below is: raw read, copy, migrate the copy, then re-verify the
    original is untouched down to its mtime.
    """

    path, recorded_physical = PRODUCTION_DATABASES[name]
    database = Path(path)
    if not database.is_file():
        pytest.skip(f"production database unavailable: {database}")

    before_sha = sha256(database)
    before_mtime = database.stat().st_mtime
    before = raw_facts(database)

    assert before["physical_schema"] == recorded_physical, (
        f"{name}: recorded physical generation drifted"
    )

    project = tmp_path / "project"
    (project / ".factory").mkdir(parents=True)
    shutil.copy2(database, project / ".factory" / "state.db")
    for suffix in ("-wal", "-shm"):
        sibling = database.with_name(database.name + suffix)
        if sibling.exists():
            shutil.copy2(sibling, project / ".factory" / f"state.db{suffix}")

    store = SQLiteStateStore(project)
    snapshot = store.status_snapshot()

    after = raw_facts(project / ".factory" / "state.db")
    assert after["physical_schema"] == 10, "the copy reaches the current generation"
    assert after["state_schema"] == before["state_schema"], "the event stream is untouched"
    assert after["events"] == before["events"]
    assert after["revision"] == before["revision"]
    assert after["user_version"] == before["user_version"] == 0
    assert after["has_v10_side_table"] is True

    # The replay and aggregate contracts apply only to a versioned stream.  One
    # production database predates that generation (296 events, no envelope), so
    # the audit branches on the data rather than assuming every database is
    # replayable - asserting "valid" there would be asserting the wrong thing,
    # and skipping it silently would hide the distinction.
    enveloped = enveloped_events(project / ".factory" / "state.db")
    if enveloped == 0:
        assert before["events"] > 0, f"{name}: empty stream, nothing to audit"
        assert snapshot["event_replay_valid"] is False, (
            f"{name}: a pre-versioned stream cannot be replay-valid"
        )
        assert snapshot["aggregate_valid"] is False
    else:
        assert enveloped == before["events"], f"{name}: the stream is only part-versioned"
        assert snapshot["event_replay_valid"] is True, f"{name}: replay broke on migration"
        assert snapshot["aggregate_valid"] is True, f"{name}: the aggregate root drifted"
        root = store.aggregate_domain_root()
        assert _V10_SIDE_TABLE in root["effect_hashes"]

    # and the production database is byte- and mtime-identical
    assert sha256(database) == before_sha, f"{name}: the audit modified the database"
    assert database.stat().st_mtime == before_mtime


#: The production databases that are still generation 9 - the population the
#: pre-validator can actually reject, and therefore the one worth checking.
V9_PRODUCTION_DATABASES = {
    name: path
    for name, (path, physical) in PRODUCTION_DATABASES.items()
    if physical == 9
}


@pytest.mark.parametrize("name", sorted(V9_PRODUCTION_DATABASES))
def test_the_real_v9_production_databases_pass_the_precheck(tmp_path, name):
    """A contract that rejected real data would be worse than no contract.

    The validator is called directly on a *copy*, read-only and without
    migrating, so a pass means the contract accepted these databases rather than
    that some later step happened to succeed.  The originals are read with raw
    SQLite only, and are re-verified byte- and mtime-identical afterwards -
    because the pre-validator runs inside ``_upgrade_schema``, which means
    pointing Factory code at an original would migrate it.
    """

    database = Path(V9_PRODUCTION_DATABASES[name])
    if not database.is_file():
        pytest.skip(f"production database unavailable: {database}")

    before_sha = sha256(database)
    before_mtime = database.stat().st_mtime
    assert raw_facts(database)["physical_schema"] == 9

    copy = tmp_path / "state.db"
    shutil.copy2(database, copy)

    connection = sqlite3.connect(f"file:{copy}?mode=ro", uri=True)
    try:
        SQLiteStateStore._validate_v9_pre_upgrade_schema(connection)
    finally:
        connection.close()

    assert sha256(database) == before_sha, f"{name}: the precheck touched the original"
    assert database.stat().st_mtime == before_mtime
