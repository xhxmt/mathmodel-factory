"""0.7 schema 9 -> 10 gate: append-only dirty-cause provenance side table.

Exit conditions covered here (hermetic):

  1 SCHEMA_VERSION == 10
  2 _upgrade_schema() accepts 9 -> 10
  3 9 -> 10 adds ONLY the side table + its triggers; no existing table changes
    column set, and no other trigger appears or disappears
  4 the side table is registered under its own _domain_effect_hashes() key
  5 a v9-shaped project upgrades and still reports aggregate_valid (the real
    A/B/R copies are verified separately; see the 0.7 report)
  6 dirty_causes' column set is byte-identical before and after the upgrade
  7 a cause with no provenance row reads back as legacy_unrecorded and is never
    re-derived from the current classifier
  8 a new cause and its provenance commit in the SAME transaction (proved by
    forcing the provenance insert to fail and observing the cause roll back)
  9 the side table rejects UPDATE and DELETE via triggers
 10 an old v9 writer cannot continue business writes against a schema-10 DB
 11 running the migration twice is idempotent
 12 a migration failure rolls back completely (no half-migrated state)
  +  the compatibility branch is not abused once the first v10 event exists
  +  the provenance derivation covers every change the classifier emits
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from factory_core import storage as storage_module
from factory_core.current_dirty import classify_manifest_changes
from factory_core.dirty_classification import (
    CLASSIFICATION_SOURCES,
    _PAPER_DOMAIN_FLAGS,
    _paper_key_present,
    DIRTY_CAUSE_CLASSIFICATION_SCHEMA,
    LEGACY_UNRECORDED,
    classification_contract_sha256,
    classification_sources,
    recorded_source,
    source_for,
)
from factory_core.domain import SCHEMA_VERSION
from factory_core.storage import SQLiteStateStore
from factory_core.workflow_events import canonical_hash

_SIDE_TABLE = "dirty_cause_classification"
_SIDE_TRIGGERS = {
    "dirty_cause_classification_append_only_update",
    "dirty_cause_classification_append_only_delete",
}

_EVENTS_TRIGGERS_SQL = """
CREATE TRIGGER IF NOT EXISTS events_append_only_update
BEFORE UPDATE ON events
BEGIN
    SELECT RAISE(ABORT, 'workflow events are append-only');
END;
CREATE TRIGGER IF NOT EXISTS events_append_only_delete
BEFORE DELETE ON events
BEGIN
    SELECT RAISE(ABORT, 'workflow events are append-only');
END;
"""


def _db(project: Path) -> Path:
    return project / ".factory" / "state.db"


def _connect(project: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(_db(project))
    connection.row_factory = sqlite3.Row
    return connection


def _initialize(tmp_path: Path) -> tuple[Path, SQLiteStateStore]:
    project = tmp_path / "proj"
    project.mkdir()
    store = SQLiteStateStore(project)
    store.initialize(project_id="proj", project_type="modeling")
    return project, store


def _table_columns(connection: sqlite3.Connection) -> dict[str, list[str]]:
    tables = [
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
    ]
    return {
        table: [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]
        for table in tables
    }


def _trigger_names(connection: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'"
        )
    }


def _schema_version(project: Path) -> int:
    connection = _connect(project)
    try:
        return int(
            connection.execute(
                "SELECT schema_version FROM schema_info WHERE singleton=1"
            ).fetchone()[0]
        )
    finally:
        connection.close()


def _rewind_to_v9(project: Path) -> None:
    """Make a faithful v9-shaped fixture out of a v10 project.

    Drops the v10-only table and triggers, removes the v10 domain key from every
    historical event payload (recomputing that event's aggregate root), and
    rewinds the recorded schema version.  Events are append-only, so the events
    triggers are temporarily dropped on this throwaway copy and restored
    afterwards with their original SQL.

    Limitation: it reproduces the v9 *shape* in the dimensions that matter for
    the migration (absent table, absent domain key, version 9), not a v9 binary's
    full state.  Real historical projects are checked separately.
    """

    connection = _connect(project)
    try:
        connection.execute("DROP TRIGGER IF EXISTS events_append_only_update")
        connection.execute("DROP TRIGGER IF EXISTS events_append_only_delete")
        # Replay-aware rewrite: a faithful v9 fixture must also say 9 inside the
        # events' state_patch, otherwise project_state and the event stream
        # disagree and the fixture is not self-consistent (0.7.2).
        accumulated: dict = {}
        saw_snapshot = False
        for row in connection.execute(
            "SELECT revision, payload_json FROM events ORDER BY revision"
        ).fetchall():
            payload = json.loads(row["payload_json"])
            envelope = payload.get("_workflow")
            if not isinstance(envelope, dict):
                continue
            patch = envelope.get("state_patch")
            if isinstance(patch, dict):
                # mutate first: the accumulated state (and therefore the hash)
                # must reflect the rewritten value
                patch["schema_version"] = 9
                if envelope.get("state_patch_mode") == "snapshot":
                    accumulated = dict(patch)
                    saw_snapshot = True
                elif saw_snapshot:
                    accumulated.update(patch)
                envelope["state_hash_after"] = canonical_hash(accumulated)
            effects = envelope.get("effect_hashes_after")
            if isinstance(effects, dict) and _SIDE_TABLE in effects:
                effects.pop(_SIDE_TABLE)
                envelope["aggregate_root_hash_after"] = canonical_hash(effects)
            connection.execute(
                "UPDATE events SET payload_json=? WHERE revision=?",
                (
                    json.dumps(payload, ensure_ascii=True, sort_keys=True),
                    row["revision"],
                ),
            )
        connection.executescript(_EVENTS_TRIGGERS_SQL)
        for trigger in _SIDE_TRIGGERS:
            connection.execute(f"DROP TRIGGER IF EXISTS {trigger}")
        connection.execute(f"DROP TABLE IF EXISTS {_SIDE_TABLE}")
        connection.execute("UPDATE schema_info SET schema_version=9 WHERE singleton=1")
        connection.execute("UPDATE project_state SET schema_version=9 WHERE singleton=1")
        connection.commit()
    finally:
        connection.close()


# --------------------------------------------------------------------------- 1
def test_schema_version_is_10():
    assert SCHEMA_VERSION == 10


# --------------------------------------------------------------------------- 2
def test_upgrade_accepts_current_9(tmp_path, monkeypatch):
    project, store = _initialize(tmp_path)
    _rewind_to_v9(project)
    assert _schema_version(project) == 9

    store.status_snapshot()  # runs _upgrade_schema

    assert _schema_version(project) == 10


def test_upgrade_rejects_a_version_the_v9_writer_never_knew(tmp_path):
    project, store = _initialize(tmp_path)
    connection = _connect(project)
    try:
        connection.execute("UPDATE schema_info SET schema_version=11 WHERE singleton=1")
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(RuntimeError, match="unsupported workflow schema"):
        store.status_snapshot()


# ------------------------------------------------------------------ 3, 6, 9, 11
def test_upgrade_only_adds_the_side_table_and_its_triggers(tmp_path):
    project, store = _initialize(tmp_path)

    connection = _connect(project)
    try:
        v10_columns = _table_columns(connection)
        v10_triggers = _trigger_names(connection)
    finally:
        connection.close()

    _rewind_to_v9(project)
    connection = _connect(project)
    try:
        v9_columns = _table_columns(connection)
        v9_triggers = _trigger_names(connection)
        assert _SIDE_TABLE not in v9_columns
        assert not (_SIDE_TRIGGERS & v9_triggers)
    finally:
        connection.close()

    store.status_snapshot()

    connection = _connect(project)
    try:
        upgraded_columns = _table_columns(connection)
        upgraded_triggers = _trigger_names(connection)
    finally:
        connection.close()

    # 3 / 6: nothing but the side table appeared; every pre-existing table keeps
    # exactly its old column list (dirty_causes included).
    assert set(upgraded_columns) - set(v9_columns) == {_SIDE_TABLE}
    for table, columns in v9_columns.items():
        assert upgraded_columns[table] == columns, table
    assert upgraded_columns == v10_columns 

    # 3 / 9: only the two side-table triggers appeared.
    assert upgraded_triggers - v9_triggers == _SIDE_TRIGGERS
    assert upgraded_triggers == v10_triggers

    # 11: running the migration again changes nothing.
    before_second = (_table_columns(_connect(project)), _schema_version(project))
    store.status_snapshot()
    connection = _connect(project)
    try:
        after_second = (_table_columns(connection), _schema_version(project))
    finally:
        connection.close()
    assert before_second == after_second


# --------------------------------------------------------------------------- 4
def test_side_table_has_its_own_domain_key(tmp_path):
    _project, store = _initialize(tmp_path)
    root = store.aggregate_domain_root()
    assert _SIDE_TABLE in root["effect_hashes"]


# --------------------------------------------------------------------------- 5
def test_v9_project_upgrades_and_stays_aggregate_valid(tmp_path):
    project, store = _initialize(tmp_path)
    _rewind_to_v9(project)

    before = store.status_snapshot()["aggregate_valid"]
    store.status_snapshot()  # upgrade
    after = store.status_snapshot()["aggregate_valid"]

    assert before is True, "the v9 fixture should itself be self-consistent"
    assert after is True, "upgrading must not break the historical aggregate"


# --------------------------------------------------------------------------- 7
def test_cause_without_provenance_reads_legacy_unrecorded(tmp_path):
    project, store = _initialize(tmp_path)
    state = store.load()
    store.transition(
        expected_revision=state.revision,
        event_type="DIRTY_FOR_TEST",
        changes={},
        dirty_changes=[
            {
                "flag": "RESULT_DIRTY",
                "owner_stage": 4,
                "cause_artifact": "results/canonical_results.json",
                "baseline_fingerprint": "a" * 64,
                "current_fingerprint": "b" * 64,
                "classifier_contract_sha256": "c" * 64,
                # no classification_source: a caller that cannot say how the cause
                # was classified must not have one invented for it
            }
        ],
    )
    connection = _connect(project)
    try:
        cause_id = connection.execute(
            "SELECT cause_id FROM dirty_causes ORDER BY rowid DESC LIMIT 1"
        ).fetchone()[0]
        assert recorded_source(connection, cause_id) == LEGACY_UNRECORDED
        assert (
            connection.execute(
                f"SELECT COUNT(*) FROM {_SIDE_TABLE} WHERE cause_id=?", (cause_id,)
            ).fetchone()[0]
            == 0
        )
    finally:
        connection.close()


def test_legacy_unrecorded_is_not_re_derived_from_the_current_classifier(tmp_path):
    """A known-dirty path must still read legacy_unrecorded when no row exists.

    'results/canonical_results.json' has a frozen owner today; if the read path
    consulted the current classifier it would answer 'frozen_rule' instead.
    """

    project, store = _initialize(tmp_path)
    state = store.load()
    store.transition(
        expected_revision=state.revision,
        event_type="DIRTY_FOR_TEST",
        changes={},
        dirty_changes=[
            {
                "flag": "RESULT_DIRTY",
                "owner_stage": 4,
                "cause_artifact": "results/canonical_results.json",
                "baseline_fingerprint": "a" * 64,
                "current_fingerprint": "b" * 64,
                "classifier_contract_sha256": "c" * 64,
            }
        ],
    )
    connection = _connect(project)
    try:
        cause_id = connection.execute(
            "SELECT cause_id FROM dirty_causes ORDER BY rowid DESC LIMIT 1"
        ).fetchone()[0]
        assert recorded_source(connection, cause_id) == LEGACY_UNRECORDED
    finally:
        connection.close()

    # and the derivation itself would have said frozen_rule -- proving the read
    # path deliberately does not consult it
    derived = classification_sources({}, {"results/canonical_results.json": "new"})
    assert derived[("RESULT_DIRTY", "results/canonical_results.json")] == "frozen_rule"


# --------------------------------------------------------------------------- 8
def test_new_cause_records_provenance_in_same_transaction(tmp_path):
    project, store = _initialize(tmp_path)
    state = store.load()
    store.transition(
        expected_revision=state.revision,
        event_type="DIRTY_FOR_TEST",
        changes={},
        dirty_changes=[
            {
                "flag": "RESULT_DIRTY",
                "owner_stage": 4,
                "cause_artifact": "results/canonical_results.json",
                "baseline_fingerprint": "a" * 64,
                "current_fingerprint": "b" * 64,
                "classifier_contract_sha256": "c" * 64,
                "classification_source": "frozen_rule",
            }
        ],
    )
    connection = _connect(project)
    try:
        row = connection.execute(
            "SELECT c.cause_id, k.classification_source, k.policy_schema, "
            "k.policy_contract_sha256 FROM dirty_causes c "
            f"JOIN {_SIDE_TABLE} k ON k.cause_id = c.cause_id "
            "ORDER BY c.rowid DESC LIMIT 1"
        ).fetchone()
        assert row is not None, "a new cause must carry provenance"
        assert row["classification_source"] == "frozen_rule"
        assert row["policy_schema"] == DIRTY_CAUSE_CLASSIFICATION_SCHEMA
        assert row["policy_contract_sha256"] == classification_contract_sha256()
    finally:
        connection.close()


def test_provenance_failure_rolls_back_the_cause(tmp_path, monkeypatch):
    """Same-transaction proof: if provenance cannot be written, no cause is left."""

    project, store = _initialize(tmp_path)
    state = store.load()

    import factory_core.dirty_classification as module

    def _boom(*args, **kwargs):
        raise RuntimeError("provenance insert failed")

    monkeypatch.setattr(module, "record_classification", _boom)
    with pytest.raises(RuntimeError, match="provenance insert failed"):
        store.transition(
            expected_revision=state.revision,
            event_type="DIRTY_FOR_TEST",
            changes={},
            dirty_changes=[
                {
                    "flag": "RESULT_DIRTY",
                    "owner_stage": 4,
                    "cause_artifact": "results/canonical_results.json",
                    "baseline_fingerprint": "a" * 64,
                    "current_fingerprint": "b" * 64,
                    "classifier_contract_sha256": "c" * 64,
                    "classification_source": "frozen_rule",
                }
            ],
        )

    connection = _connect(project)
    try:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM dirty_causes WHERE cause_artifact=?",
                ("results/canonical_results.json",),
            ).fetchone()[0]
            == 0
        ), "the cause must not survive without its provenance"
    finally:
        connection.close()


def test_unknown_classification_source_is_rejected(tmp_path):
    _project, store = _initialize(tmp_path)
    state = store.load()
    with pytest.raises(ValueError, match="unknown classification source"):
        store.transition(
            expected_revision=state.revision,
            event_type="DIRTY_FOR_TEST",
            changes={},
            dirty_changes=[
                {
                    "flag": "RESULT_DIRTY",
                    "owner_stage": 4,
                    "cause_artifact": "results/canonical_results.json",
                    "baseline_fingerprint": "a" * 64,
                    "current_fingerprint": "b" * 64,
                    "classifier_contract_sha256": "c" * 64,
                    "classification_source": "made_up",
                }
            ],
        )


# --------------------------------------------------------------------------- 9
def test_side_table_rejects_update_and_delete(tmp_path):
    project, store = _initialize(tmp_path)
    state = store.load()
    store.transition(
        expected_revision=state.revision,
        event_type="DIRTY_FOR_TEST",
        changes={},
        dirty_changes=[
            {
                "flag": "RESULT_DIRTY",
                "owner_stage": 4,
                "cause_artifact": "results/canonical_results.json",
                "baseline_fingerprint": "a" * 64,
                "current_fingerprint": "b" * 64,
                "classifier_contract_sha256": "c" * 64,
                "classification_source": "frozen_rule",
            }
        ],
    )
    connection = _connect(project)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                f"UPDATE {_SIDE_TABLE} SET classification_source='fallback'"
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(f"DELETE FROM {_SIDE_TABLE}")
    finally:
        connection.close()


# -------------------------------------------------------------------------- 10
def test_old_v9_writer_cannot_continue_writing_a_schema_10_database(tmp_path, monkeypatch):
    """Downgrade barrier: the v9 binary sees schema 10 and must refuse.

    Simulated by pinning the SCHEMA_VERSION constant the v9 writer used.  Its
    accept-set was {1..8}; 10 is outside both that set and the current one, so
    the simulated writer reaches the same refusal.
    """

    project, store = _initialize(tmp_path)
    assert _schema_version(project) == 10

    monkeypatch.setattr(storage_module, "SCHEMA_VERSION", 9)
    with pytest.raises(RuntimeError, match="unsupported workflow schema"):
        store.status_snapshot()


# -------------------------------------------------------------------------- 12
def test_failed_migration_rolls_back_completely(tmp_path, monkeypatch):
    project, store = _initialize(tmp_path)
    _rewind_to_v9(project)
    assert _schema_version(project) == 9

    def _boom(*args, **kwargs):
        raise RuntimeError("migration aborted")

    # 0.7.1 removed the classifier rebase from _upgrade_schema, so inject the
    # failure at a step the 9->10 migration still performs.
    import factory_core.dirty_classification as dc_module

    monkeypatch.setattr(dc_module, "ensure_dirty_cause_classification_schema", _boom)
    with pytest.raises(RuntimeError, match="migration aborted"):
        store.status_snapshot()

    connection = _connect(project)
    try:
        # no half-migrated state: version still 9 and the v10 table never landed
        assert _schema_version(project) == 9
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?",
                (_SIDE_TABLE,),
            ).fetchone()[0]
            == 0
        )
        assert not (_SIDE_TRIGGERS & _trigger_names(connection))
    finally:
        connection.close()


# ------------------------------------------------------ compatibility branch use
def test_first_v10_event_ends_the_tolerance_window(tmp_path):
    project, store = _initialize(tmp_path)

    # The migration itself writes no event when no rebase is due, so the new
    # domain key is still covered only by the tolerant branch.
    connection = _connect(project)
    try:
        effects = store.aggregate_domain_root()["effect_hashes"]
    finally:
        connection.close()
    assert _SIDE_TABLE in effects

    # From the first v10 event onward the key sets match, so the strict
    # aggregate comparison applies to the new table too.
    state = store.load()
    store.transition(
        expected_revision=state.revision,
        event_type="DIRTY_FOR_TEST",
        changes={},
        dirty_changes=[],
    )
    snapshot = store.status_snapshot()
    connection = _connect(project)
    try:
        latest = connection.execute(
            "SELECT payload_json FROM events ORDER BY revision DESC LIMIT 1"
        ).fetchone()[0]
    finally:
        connection.close()
    prior = json.loads(latest)["_workflow"]["effect_hashes_after"]
    assert set(prior) == set(effects)
    assert snapshot["aggregate_valid"] is True

    # Any unexpected change to the side table must now fail the aggregate.
    connection = _connect(project)
    try:
        for trigger in _SIDE_TRIGGERS:
            connection.execute(f"DROP TRIGGER IF EXISTS {trigger}")
        connection.execute(
            f"INSERT INTO {_SIDE_TABLE}"
            "(cause_id, classification_source, policy_schema, policy_contract_sha256)"
            " VALUES ('tampered', 'fallback', 'x', 'y')"
        )
        connection.commit()
    finally:
        connection.close()

    assert store.status_snapshot()["aggregate_valid"] is False


# ------------------------------------------------- derivation completeness (extra)
@pytest.mark.parametrize(
    "before,after,expected",
    [
        ({}, {"results/canonical_results.json": "x"}, "frozen_rule"),
        ({}, {"judge_evidence.json": "x"}, "current_rule"),
        ({}, {"@protected:paper/main.tex:BUG1": "x"}, "protected"),
        ({}, {"@paper:tables.tex:format": "x"}, "paper_semantic"),
        ({}, {"unknown_authored_contract.json": "x"}, "fallback"),
    ],
)
def test_derivation_assigns_the_expected_source(before, after, expected):
    for source in classification_sources(before, after).values():
        assert source == expected
        assert source in CLASSIFICATION_SOURCES


@pytest.mark.parametrize(
    "before,after",
    [
        ({}, {"results/canonical_results.json": "x"}),
        ({}, {"unknown_authored_contract.json": "x"}),
        ({}, {"judge_evidence.json": "x"}),
        ({}, {"@protected:paper/main.tex:BUG1": "x"}),
        ({}, {"@paper:tables.tex:format": "x"}),
        (
            {"paper/main_paper.tex": "a"},
            {"paper/main_paper.tex": "b", "@paper:paper/main_paper.tex:math": "b"},
        ),
    ],
)
def test_derivation_covers_every_change_the_classifier_emits(before, after):
    """No change may be left without a derived source.

    A miss would be recorded as legacy_unrecorded for a brand-new cause, which
    defeats the point of the side table.
    """

    sources = classification_sources(before, after)
    for change in classify_manifest_changes(before, after):
        assert (
            source_for(sources, change.flag.value, change.cause_artifact)
            != LEGACY_UNRECORDED
        ), f"underived change: {change.flag.value} {change.cause_artifact}"


# ===========================================================================
# 0.7.1 additions
# ===========================================================================

def test_read_paths_do_not_mutate_revision_or_events(tmp_path):
    """I7: opening a project for reading must not append events or bump revision.

    The schema DDL migration is allowed on a read path; mutating the workflow
    event stream and the business revision is not.
    """

    project, store = _initialize(tmp_path)
    _rewind_to_v9(project)

    connection = _connect(project)
    try:
        before_events = connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        before_revision = connection.execute(
            "SELECT revision FROM project_state WHERE singleton=1"
        ).fetchone()[0]
    finally:
        connection.close()
    assert _schema_version(project) == 9

    # every read entry point that previously ran the migration
    store.load()
    store.status_snapshot()
    store.events()
    store.dirty_flags()
    store.stage_checkpoints()
    store.aggregate_domain_root()
    store.solver_jobs()

    connection = _connect(project)
    try:
        after_events = connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        after_revision = connection.execute(
            "SELECT revision FROM project_state WHERE singleton=1"
        ).fetchone()[0]
        last_types = [
            r[0] for r in connection.execute(
                "SELECT type FROM events ORDER BY revision DESC LIMIT 3"
            )
        ]
    finally:
        connection.close()

    assert _schema_version(project) == 10, "the DDL migration should still happen"
    assert after_events == before_events, "a read must not append events"
    assert after_revision == before_revision, "a read must not move the revision"
    assert "DIRTY_CLASSIFIER_REBASED" not in last_types


def test_explicit_rebase_is_cas_guarded_and_is_the_only_writer(tmp_path):
    """I7: the rebase stays available, but only as an explicit CAS-guarded action."""

    from factory_core.domain import RevisionConflict

    project, store = _initialize(tmp_path)
    state = store.load()
    store.transition(
        expected_revision=state.revision,
        event_type="DIRTY_FOR_TEST",
        changes={},
        dirty_changes=[
            {
                "flag": "RESULT_DIRTY",
                "owner_stage": 4,
                "cause_artifact": "results/canonical_results.json",
                "baseline_fingerprint": "a" * 64,
                "current_fingerprint": "b" * 64,
                "classifier_contract_sha256": "0" * 64,  # stale -> a rebase is due
                "classification_source": "frozen_rule",
            }
        ],
    )

    current = store.load()
    rebased = store.rebase_dirty_classifier(expected_revision=current.revision)
    assert rebased.revision == current.revision + 1
    assert store.events()[-1].type == "DIRTY_CLASSIFIER_REBASED"

    with pytest.raises(RevisionConflict):
        store.rebase_dirty_classifier(expected_revision=current.revision)


def test_effect_domain_generation_must_be_monotonic(tmp_path):
    """I2: a v9-shaped event after a v10 event must invalidate the aggregate.

    The tolerant branch would accept such an event on its own (the latest event's
    domain set differs from the current one), so this asserts the verifier
    enforces the invariant instead of relying on writers never regressing.
    """

    project, store = _initialize(tmp_path)
    state = store.load()
    store.transition(
        expected_revision=state.revision,
        event_type="DIRTY_FOR_TEST",
        changes={},
        dirty_changes=[],
    )
    assert store.status_snapshot()["aggregate_valid"] is True

    connection = _connect(project)
    try:
        connection.execute("DROP TRIGGER IF EXISTS events_append_only_update")
        connection.execute("DROP TRIGGER IF EXISTS events_append_only_delete")
        row = connection.execute(
            "SELECT revision, created_at, attempt, payload_json FROM events "
            "ORDER BY revision DESC LIMIT 1"
        ).fetchone()
        payload = json.loads(row["payload_json"])
        envelope = payload["_workflow"]
        effects = dict(envelope["effect_hashes_after"])
        effects.pop(_SIDE_TABLE)  # regress to a v9-shaped domain set
        envelope["effect_hashes_after"] = effects
        envelope["aggregate_root_hash_after"] = canonical_hash(effects)
        new_revision = int(row["revision"]) + 1
        connection.execute(
            "INSERT INTO events(revision, type, created_at, step, attempt, payload_json) "
            "VALUES (?, 'DIRTY_FOR_TEST', ?, NULL, ?, ?)",
            (
                new_revision,
                int(row["created_at"]),
                int(row["attempt"]),
                json.dumps(payload, ensure_ascii=True, sort_keys=True),
            ),
        )
        connection.execute(
            "UPDATE project_state SET revision=? WHERE singleton=1", (new_revision,)
        )
        connection.commit()
    finally:
        connection.close()

    assert store.status_snapshot()["aggregate_valid"] is False


def test_provenance_insert_rejected_at_db_level_rolls_back_cause(tmp_path):
    """I6: database-level (not monkeypatch) proof of the shared transaction."""

    project, store = _initialize(tmp_path)
    state = store.load()

    connection = _connect(project)
    try:
        connection.execute(
            f"""
            CREATE TRIGGER reject_classification_insert
            BEFORE INSERT ON {_SIDE_TABLE}
            BEGIN
                SELECT RAISE(ABORT, 'classification insert rejected');
            END
            """
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(sqlite3.IntegrityError):
        store.transition(
            expected_revision=state.revision,
            event_type="DIRTY_FOR_TEST",
            changes={},
            dirty_changes=[
                {
                    "flag": "RESULT_DIRTY",
                    "owner_stage": 4,
                    "cause_artifact": "results/canonical_results.json",
                    "baseline_fingerprint": "a" * 64,
                    "current_fingerprint": "b" * 64,
                    "classifier_contract_sha256": "c" * 64,
                    "classification_source": "frozen_rule",
                }
            ],
        )

    connection = _connect(project)
    try:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM dirty_causes WHERE cause_artifact=?",
                ("results/canonical_results.json",),
            ).fetchone()[0]
            == 0
        ), "the cause must not survive a rejected provenance insert"
    finally:
        connection.close()


def test_bespoke_recovery_is_a_recordable_distinct_source(tmp_path):
    """I5: the bespoke recovery writer records provenance, not legacy_unrecorded."""

    from factory_core.dirty_classification import record_classification

    assert "bespoke_recovery" in CLASSIFICATION_SOURCES

    project, _store = _initialize(tmp_path)
    connection = _connect(project)
    try:
        connection.execute(
            "INSERT INTO dirty_causes(cause_id, flag, owner_stage, cause_revision, "
            "cause_artifact, baseline_fingerprint, current_fingerprint, "
            "classifier_contract_sha256) VALUES ('x','RESULT_DIRTY',4,1,'a','b','c','d')"
        )
        # no provenance row -> the pre-existing reading
        assert recorded_source(connection, "x") == LEGACY_UNRECORDED
        record_classification(
            connection,
            cause_id="x",
            classification_source="bespoke_recovery",
            contract_sha256="e" * 64,
        )
        # and the recovery path's own value is distinguishable from that reading
        assert recorded_source(connection, "x") == "bespoke_recovery"
        connection.commit()
    finally:
        connection.close()
# ===========================================================================
# 0.7.2 additions
# ===========================================================================

def test_read_migration_keeps_replay_equal_to_current_state(tmp_path):
    """Major 1 gate: a read-triggered DDL migration must not desynchronise replay.

    project_state.schema_version is a _REPLAY_FIELDS member, so bumping it from a
    migration that a read path can trigger would leave the database saying 10
    while the event stream replays to 9 -- with no event recording the change.
    """

    from factory_core.workflow_events import REPLAY_FIELDS, replay_events, replay_state

    project, store = _initialize(tmp_path)
    _rewind_to_v9(project)

    connection = _connect(project)
    try:
        before = (
            connection.execute("SELECT revision FROM project_state").fetchone()[0],
            connection.execute("SELECT COUNT(*) FROM events").fetchone()[0],
        )
    finally:
        connection.close()
    store.load()
    store.status_snapshot()

    snapshot = store.status_snapshot()
    assert snapshot["event_replay_valid"] is True
    assert snapshot["aggregate_valid"] is True

    connection = _connect(project)
    try:
        after = (
            connection.execute("SELECT revision FROM project_state").fetchone()[0],
            connection.execute("SELECT COUNT(*) FROM events").fetchone()[0],
            connection.execute(
                "SELECT schema_version FROM schema_info WHERE singleton=1"
            ).fetchone()[0],
            connection.execute(
                "SELECT schema_version FROM project_state WHERE singleton=1"
            ).fetchone()[0],
        )
    finally:
        connection.close()

    assert after[0] == before[0], "revision must not move"
    assert after[1] == before[1], "no event may be appended"
    assert after[2] == 10, "physical schema is upgraded"
    assert after[3] == 9, "replay-recorded generation is untouched by a read"

    replayed = replay_events(store.events())
    current = replay_state(store.load())
    assert all(replayed.get(f) == current.get(f) for f in REPLAY_FIELDS)


def test_genuine_write_converges_workflow_schema_version(tmp_path):
    """The replay-recorded generation converges on an event-carrying write."""

    project, store = _initialize(tmp_path)
    _rewind_to_v9(project)
    state = store.load()
    assert state.schema_version == 9, "a read leaves it alone"

    written = store.transition(
        expected_revision=state.revision,
        event_type="SCHEMA_GENERATION_CONVERGENCE_FOR_TEST",
        changes={},
    )
    assert written.schema_version == 10

    snapshot = store.status_snapshot()
    assert snapshot["event_replay_valid"] is True
    assert snapshot["aggregate_valid"] is True

    from factory_core.workflow_events import REPLAY_FIELDS, replay_events, replay_state

    replayed = replay_events(snapshot["events"])
    current = replay_state(snapshot["state"])
    assert all(replayed.get(f) == current.get(f) for f in REPLAY_FIELDS)


def test_attestation_without_domain_map_fails_closed(tmp_path):
    """I2 addition: an attested aggregate root must carry its domain map."""

    project, store = _initialize(tmp_path)
    state = store.load()
    store.transition(
        expected_revision=state.revision,
        event_type="DIRTY_FOR_TEST",
        changes={},
        dirty_changes=[],
    )
    assert store.status_snapshot()["effect_domain_generation_valid"] is True

    connection = _connect(project)
    try:
        connection.execute("DROP TRIGGER IF EXISTS events_append_only_update")
        connection.execute("DROP TRIGGER IF EXISTS events_append_only_delete")
        row = connection.execute(
            "SELECT revision, created_at, attempt, payload_json FROM events "
            "ORDER BY revision DESC LIMIT 1"
        ).fetchone()
        payload = json.loads(row["payload_json"])
        envelope = payload["_workflow"]
        # malformed: attests a root but drops the domain map it was computed from
        del envelope["effect_hashes_after"]
        new_revision = int(row["revision"]) + 1
        connection.execute(
            "INSERT INTO events(revision, type, created_at, step, attempt, payload_json) "
            "VALUES (?, 'DIRTY_FOR_TEST', ?, NULL, ?, ?)",
            (new_revision, int(row["created_at"]), int(row["attempt"]),
             json.dumps(payload, ensure_ascii=True, sort_keys=True)),
        )
        connection.execute(
            "UPDATE project_state SET revision=? WHERE singleton=1", (new_revision,)
        )
        connection.commit()
    finally:
        connection.close()

    snapshot = store.status_snapshot()
    assert snapshot["effect_domain_generation_valid"] is False
    assert snapshot["aggregate_valid"] is False


def test_dirty_clear_rebase_is_bound_into_the_transition_payload(tmp_path):
    """Major 2: the dirty-clear path rebases inside its own transaction.

    That second legitimate rebase path emits no DIRTY_CLASSIFIER_REBASED event of
    its own, so its receipt identity must be bound into the transition payload;
    otherwise no event records which rebase happened.
    """

    project, store = _initialize(tmp_path)
    state = store.load()
    dirtied = store.transition(
        expected_revision=state.revision,
        event_type="DIRTY_FOR_TEST",
        changes={},
        dirty_changes=[
            {
                "flag": "RESULT_DIRTY",
                "owner_stage": 4,
                "cause_artifact": "results/canonical_results.json",
                "baseline_fingerprint": "a" * 64,
                "current_fingerprint": "b" * 64,
                "classifier_contract_sha256": "0" * 64,  # stale -> rebase is due
                "classification_source": "frozen_rule",
            }
        ],
    )
    from factory_core.current_dirty import (
        capture_artifact_manifest,
        classifier_contract_sha256,
        manifest_fingerprint,
    )

    # The dirty obligation carries a stale classifier identity, so the clear
    # triggers an in-transaction rebase; the clear and its receipt must name the
    # CURRENT identity, which is what the rebase rewrites the row to before the
    # per-row check runs.
    output = manifest_fingerprint(capture_artifact_manifest(project))
    current = classifier_contract_sha256()
    success_receipt = {
        "schema_version": "factory-stage-checkpoint-v1",
        "status": "PASS",
        "stage": 4,
        "output_fingerprint": output,
        "classifier_contract_sha256": current,
    }
    cleared = store.transition(
        expected_revision=dirtied.revision,
        event_type="STAGE_SUCCEEDED_FOR_TEST",
        changes={},
        stage_checkpoint={
            "stage_id": 4,
            "subtask": "canonical_solve",
            "source_step_id": 7,
            "completed_step_id": 7,
            "input_fingerprint": output,
            "output_fingerprint": output,
            "receipt": success_receipt,
        },
        clear_dirty_stage={
            "owner_stage": 4,
            "cleared_fingerprint": output,
            "classifier_contract_sha256": current,
            "success_receipt": success_receipt,
        },
    )

    envelope = store.events()[-1].payload.get("_workflow") or {}
    embedded = store.events()[-1].payload.get("embedded_rebase")
    assert cleared.revision > dirtied.revision
    assert embedded is not None, "the embedded rebase must be recorded"
    assert embedded["bound_by"] == "dirty_clear_transaction"
    assert embedded["rebase_id"], "receipt identity must be present"
    assert envelope.get("event_version") == 2


def test_provenance_contract_covers_more_than_this_module(tmp_path):
    """Major 3: the provenance identity must pin the semantics it depends on.

    Hashing only dirty_classification.py's own bytes would leave the pair
    (classifier sha, policy sha) unable to uniquely rebuild an attribution.
    """

    root = Path(__file__).resolve().parents[1] / "factory_core"
    narrow = hashlib.sha256(
        DIRTY_CAUSE_CLASSIFICATION_SCHEMA.encode("ascii")
        + b"\0"
        + (root / "dirty_classification.py").read_bytes()
    ).hexdigest()

    assert classification_contract_sha256() != narrow, (
        "the provenance contract must cover the ownership registries and matcher, "
        "not only this module's bytes"
    )
    # and it must stay distinct from the classifier identity (Q14: kept separate)
    from factory_core.current_dirty import classifier_contract_sha256 as classifier_sha

    assert classification_contract_sha256() != classifier_sha()

# ===========================================================================
# S1-D: policy-aware provenance derivation
# ===========================================================================

#: Paths exercising every attribution branch the derivation can take.
_DERIVATION_CORPUS = (
    "results/canonical_results.json",                 # frozen rule
    "results/problem1/values.json",                   # frozen rule
    "solve_log.md",                                   # frozen rule
    "judge_evidence.json",                            # current rule (ADDITIONAL)
    "models/reporting_scope/scope_review_manifest.json",  # current rule
    "STEP5_RECEIPT.json",                             # current rule
    "method_fit_suggestions.json",                    # current rule
    ".factory/solver_inputs/snap.json",               # current rule
    "unknown_authored_contract.json",                 # fallback
    "step5_results_gap_report.md",                     # fallback (S1-B will register)
    "tables.tex",                                     # format rule
    "paper/main_paper.tex",                           # paper source rule
    "abstract_draft.md",                              # stage 9 prose
    "result1.xlsx",                                   # declared result
    "data/intermediate/scratch.csv",                  # stage 4, final_input False
)


def _legacy_derivation(before: dict, after: dict) -> dict[tuple[str, str], str]:
    """The pre-S1-D ownership-based derivation, reproduced verbatim.

    Used only as a reference oracle so the S1-D rewrite can be shown to be
    equivalent while NATIVE_POLICY is empty.
    """

    from factory_core.artifact_ownership import artifact_ownership
    from factory_core.current_artifact_ownership import (
        ADDITIONAL_OWNERSHIP,
        artifact_pattern_matches,
    )
    from factory_core.dirty_classification import _authored_source

    changed = sorted(
        path for path in set(before) | set(after) if before.get(path) != after.get(path)
    )
    sources: dict[tuple[str, str], str] = {}
    for artifact in changed:
        if artifact.startswith("@protected:"):
            sources[("MATH_DIRTY", artifact)] = "protected"
        elif artifact.startswith("@paper:"):
            _, relative, domain = artifact.split(":", 2)
            flag = _PAPER_DOMAIN_FLAGS[domain]
            sources[(flag, relative)] = "paper_semantic"
    for artifact in changed:
        if artifact.startswith("@"):
            continue
        if artifact.endswith(".tex") and _paper_key_present(artifact, before, after):
            if not any(key.startswith(f"@paper:{artifact}:") for key in changed):
                sources[("FORMAT_DIRTY", artifact)] = "paper_semantic"
            else:
                for key, value in _authored_source(artifact).items():
                    sources.setdefault(key, value)
            continue
        current_rule = next(
            (rule for rule in ADDITIONAL_OWNERSHIP
             if artifact_pattern_matches(rule.pattern, artifact)),
            None,
        )
        if current_rule is not None:
            sources[(current_rule.dirty_flag, artifact)] = "current_rule"
            continue
        frozen_rule = artifact_ownership(artifact)
        if frozen_rule is not None:
            sources[(frozen_rule.dirty_flag, artifact)] = "frozen_rule"
        else:
            sources[("MATH_DIRTY", artifact)] = "fallback"
            sources[("RESULT_DIRTY", artifact)] = "fallback"
    return sources


def _unregistered_corpus():
    """Corpus entries with no NATIVE_POLICY entry, where the pre-S1-D oracle
    and the policy-aware derivation must still agree exactly."""

    from factory_core.artifact_policy import artifact_policy

    return tuple(
        path for path in _DERIVATION_CORPUS
        if artifact_policy(path) is None
        or artifact_policy(path).ownership_rule is not None
    )


@pytest.mark.parametrize("path", _unregistered_corpus())
def test_policy_aware_derivation_matches_the_legacy_oracle(path):
    """For every path the policy layer resolves to an ownership rule (or not at
    all), attribution must be byte-identical to the pre-S1-D ownership-based
    derivation.  Paths S1-B registered are asserted separately below."""

    before = {path: "a"}
    after = {path: "b"}
    assert classification_sources(before, after) == _legacy_derivation(before, after)


@pytest.mark.parametrize(
    "path,expected",
    [
        # EXPLICIT_ONLY: no obligation, so no provenance entry at all
        ("step5_results_gap_report.md", {}),
        ("m1_reuse_gap_record.md", {}),
        ("m1_solver_evidence_failed.json", {}),
        ("paper/appendix_sources/pro01/input_arrays.npz", {}),
        # routed evidence: keeps the result rewind, attributed policy_only
        ("m1_solver_evidence.json", {("RESULT_DIRTY", "m1_solver_evidence.json"): "policy_only"}),
        ("model_source_map.json", {("RESULT_DIRTY", "model_source_map.json"): "policy_only"}),
        # rebuildable presentation: format obligation, attributed policy_only
        ("tables.tex", {("FORMAT_DIRTY", "tables.tex"): "policy_only"}),
        ("results_values.tex", {("FORMAT_DIRTY", "results_values.tex"): "policy_only"}),
        ("paper/appendix_sources/06_figures.py",
         {("FORMAT_DIRTY", "paper/appendix_sources/06_figures.py"): "policy_only"}),
    ],
)
def test_s1b_registered_paths_attribute_as_policy_only(path, expected):
    """The registered paths now differ from the pre-S1-D derivation on purpose.

    This is the behaviour change S1-B exists to make, asserted per path so the
    mapping cannot drift silently.  An EXPLICIT_ONLY entry contributes no
    provenance at all because it produces no obligation to explain.
    """

    assert classification_sources({}, {path: "x"}) == expected


@pytest.mark.parametrize(
    "path,expected",
    [
        # the fail-closed pair is gone for every registered path
        ("step5_results_gap_report.md", set()),
        ("m1_solver_evidence.json", {("RESULT_DIRTY", 4)}),
        ("tables.tex", {("FORMAT_DIRTY", 9)}),
        ("results_values.tex", {("FORMAT_DIRTY", 9)}),
        # and still present for a genuinely unknown path
        ("truly_unknown_authored_contract.json", {("MATH_DIRTY", 8), ("RESULT_DIRTY", 4)}),
    ],
)
def test_s1b_removes_the_fail_closed_pair_for_registered_paths(path, expected):
    """The classifier's view of the same question, asserted alongside the
    provenance view so the two cannot disagree."""

    from factory_core.current_dirty import classify_manifest_changes

    got = {(c.flag.value, c.owner_stage) for c in classify_manifest_changes({}, {path: "x"})}
    assert got == expected, path


def test_derivation_oracle_agrees_on_synthetic_and_paper_keys():
    cases = [
        ({}, {"@protected:paper/main.tex:BUG1": "x"}),
        ({}, {"@paper:tables.tex:format": "x"}),
        ({}, {"@paper:tables.tex:math": "x"}),
        ({"paper/p.tex": "a"}, {"paper/p.tex": "b", "@paper:paper/p.tex:prose": "b"}),
    ]
    for before, after in cases:
        assert classification_sources(before, after) == _legacy_derivation(before, after)


def test_derivation_covers_every_emitted_change_for_the_corpus():
    for path in _DERIVATION_CORPUS:
        before, after = {path: "a"}, {path: "b"}
        sources = classification_sources(before, after)
        for change in classify_manifest_changes(before, after):
            assert (
                source_for(sources, change.flag.value, change.cause_artifact)
                != LEGACY_UNRECORDED
            ), f"underived: {change.flag.value} {change.cause_artifact}"


def test_every_emitted_change_is_attributed_for_paper_key_changes():
    """The corpus varies only the artifact itself, so it never built the pair that
    breaks this: a ``.tex`` whose ``@paper:`` key changed.

    The frozen classifier diverts such a path into paper_raw_changes and emits its
    own FORMAT_DIRTY obligation for it, whichever domain changed.  The synthetic
    loop records the changed domain's flag, and the FORMAT_DIRTY obligation ended
    up with no entry at all - so ``source_for`` read it back as
    ``legacy_unrecorded`` for an obligation the classifier had definitely emitted.
    Only the ``format`` domain happened to work, because there the synthetic flag
    and the emitted flag are the same key.
    """

    for domain in ("math", "citation", "prose", "format"):
        before: dict = {}
        after = {"tables.tex": "x", f"@paper:tables.tex:{domain}": "y"}
        sources = classification_sources(before, after)
        emitted = classify_manifest_changes(before, after)
        assert emitted, f"precondition: the {domain} pair emits a change"
        for change in emitted:
            assert (
                source_for(sources, change.flag.value, change.cause_artifact)
                != LEGACY_UNRECORDED
            ), (
                f"underived after a changed {domain} key: "
                f"{change.flag.value} {change.cause_artifact}"
            )


def test_policy_only_entry_is_attributed_as_policy_only(monkeypatch):
    """The reason S1-D exists: a policy-only entry must be distinguishable from
    the fail-closed fallback, so provenance can tell "described but unrouted"
    from "nothing knows this path".

    Injected through the real registry so the assertion exercises the production
    lookup path rather than a stubbed one.
    """

    # the real registration, no injection
    sources = classification_sources({}, {"m1_solver_evidence.json": "x"})
    assert sources[("RESULT_DIRTY", "m1_solver_evidence.json")] == "policy_only"
    # and it is NOT the fail-closed fallback pair any more
    assert ("MATH_DIRTY", "m1_solver_evidence.json") not in sources
    assert "policy_only" in CLASSIFICATION_SOURCES


def test_policy_only_attribution_differs_from_fallback():
    """Contrast: the same path with no policy at all is the fail-closed pair."""

    from factory_core.artifact_policy import artifact_policy

    path = "truly_unknown_authored_contract.json"
    assert artifact_policy(path) is None, "precondition: genuinely unregistered"
    sources = classification_sources({}, {path: "x"})
    assert sources[("MATH_DIRTY", path)] == "fallback"
    assert sources[("RESULT_DIRTY", path)] == "fallback"


#: The provenance contract before S1-D, when artifact_policy.py was not yet a
#: dependency of the derivation.
_PROVENANCE_CONTRACT_AT_0_7_2 = None  # recorded in the S1-D stage record


def test_classification_contract_covers_the_policy_layer_but_not_the_classifier():
    """S1-D made attribution depend on artifact_policy.py, so the provenance
    contract must cover it.  The classifier identity must NOT move: keeping the
    two apart is Q14, and is what stops a provenance-only change forcing a dirty
    rebase.
    """

    import inspect

    import factory_core.dirty as dirty
    from factory_core.current_dirty import classifier_contract_sha256 as classifier_sha

    members_source = inspect.getsource(classification_contract_sha256)
    assert "artifact_policy.py" in members_source

    # provenance identity != classifier identity
    assert classification_contract_sha256() != classifier_sha()

    # the frozen classifier trust root is untouched by S1-D
    assert dirty.classifier_contract_sha256() == (
        "c451a9d0be64fabd185c7561b67093956e663db6e845915cd320fea1cbabc515"
    )


def test_provenance_contract_moves_when_a_contract_member_moves(tmp_path):
    """Guard against a vacuous "the contract covers X" claim: perturbing a
    covered module's bytes must change the identity."""

    import factory_core.dirty_classification as dc_module

    original = dc_module.classification_contract_sha256()
    real_read = Path.read_bytes

    def fake_read(self):
        data = real_read(self)
        if self.name == "artifact_policy.py":
            return data + b"\n# perturbation\n"
        return data

    try:
        Path.read_bytes = fake_read
        assert dc_module.classification_contract_sha256() != original
    finally:
        Path.read_bytes = real_read


# --------------------------------------------------------- replay regression
def test_s1d_replay_regression_on_v9_fixture(tmp_path):
    """The 0.7.2 replay gate must still hold after the S1-D changes."""

    project, store = _initialize(tmp_path)
    _rewind_to_v9(project)

    store.status_snapshot()  # triggers the DDL migration
    state = store.load()
    assert state.schema_version == 9, "a read still leaves the replay generation alone"

    snapshot = store.status_snapshot()
    assert snapshot["event_replay_valid"] is True
    assert snapshot["aggregate_valid"] is True

    # a genuine write converges it, and replay still equals current state
    written = store.transition(
        expected_revision=state.revision,
        event_type="S1D_REPLAY_FOR_TEST",
        changes={},
    )
    assert written.schema_version == 10
    after = store.status_snapshot()
    assert after["event_replay_valid"] is True
    assert after["aggregate_valid"] is True


def test_s1d_new_cause_records_policy_aware_provenance(tmp_path):
    """A cause created after S1-D carries a source from the extended vocabulary."""

    project, store = _initialize(tmp_path)
    state = store.load()
    store.transition(
        expected_revision=state.revision,
        event_type="DIRTY_FOR_TEST",
        changes={},
        dirty_changes=[
            {
                "flag": "RESULT_DIRTY",
                "owner_stage": 4,
                "cause_artifact": "results/canonical_results.json",
                "baseline_fingerprint": "a" * 64,
                "current_fingerprint": "b" * 64,
                "classifier_contract_sha256": "c" * 64,
                "classification_source": "frozen_rule",
            }
        ],
    )
    connection = _connect(project)
    try:
        row = connection.execute(
            "SELECT c.cause_id, k.classification_source, k.policy_contract_sha256 "
            "FROM dirty_causes c "
            f"JOIN {_SIDE_TABLE} k ON k.cause_id=c.cause_id ORDER BY c.rowid DESC LIMIT 1"
        ).fetchone()
        assert row["classification_source"] == "frozen_rule"
        assert row["policy_contract_sha256"] == classification_contract_sha256()
    finally:
        connection.close()
