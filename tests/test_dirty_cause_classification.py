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

import json
import sqlite3
from pathlib import Path

import pytest

from factory_core import storage as storage_module
from factory_core.current_dirty import classify_manifest_changes
from factory_core.dirty_classification import (
    CLASSIFICATION_SOURCES,
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
        for row in connection.execute(
            "SELECT revision, payload_json FROM events"
        ).fetchall():
            payload = json.loads(row["payload_json"])
            envelope = payload.get("_workflow")
            if not isinstance(envelope, dict):
                continue
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

    # imported inside _upgrade_schema, so patch the defining module
    import factory_core.dirty_rebase as rebase_module

    monkeypatch.setattr(rebase_module, "rebase_dirty_classifier_state", _boom)
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
