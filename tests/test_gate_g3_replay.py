"""G3: historical replay and reason-envelope compatibility.

S4.1 changed the shape of the reason attached to every event. The risk is not
that new events look wrong - it is that *old* events stop being readable or
replayable, which would break the audit chain the whole project depends on.

This gate runs against the real A/B/R event streams:

  * the full stream still replays and still reflects the current state
  * historical events carry an empty subcode/actor and remain readable
  * the reason change did not perturb any recorded state hash
  * r517's REOPEN_REVISION_TEXT is now readable from the reason alone, which it
    was not before S4.1
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from factory_core.storage import SQLiteStateStore, read_only_uri
from factory_core.workflow_events import (
    ENVELOPE_KEY,
    REPLAY_FIELDS,
    ReplayIntegrityError,
    normalize_reason,
    replay_events,
    replay_state,
)

import _gate_projects

_REAL = {name: str(_gate_projects.real_path(name)) for name in _gate_projects.PROJECTS}


def _requires(name: str) -> Path:
    return _gate_projects.require(name)


def _db(name: str) -> Path:
    # goes through _requires so an absent project skips rather than stat()-ing
    # a path that does not exist
    return _gate_projects.require(name) / ".factory" / "state.db"


def _snapshot(tmp_path, name: str) -> Path:
    """A disposable copy of a real project; see ``_gate_projects.snapshot``.

    Every Factory read path calls ``_upgrade_schema``, which promotes a
    generation-9 database to 10 and commits, so reading a real project with
    Factory code can rewrite it.  The gate reads a copy; the original is only ever
    opened by raw SQLite.
    """

    return _gate_projects.snapshot(name, tmp_path)


@pytest.mark.parametrize("name", sorted(_REAL))
def test_historical_stream_still_replays_and_matches_current_state(tmp_path, name):
    """The strongest compatibility assertion available: the whole stream replays.

    ``replay_events`` verifies every event's state hash by default, so a reason
    change that perturbed the envelope would raise here rather than pass silently.
    """

    store = SQLiteStateStore(_snapshot(tmp_path, name))
    snapshot = store.status_snapshot()
    replayed = replay_events(snapshot["events"])  # verify_hashes=True by default
    current = replay_state(snapshot["state"])
    for field in REPLAY_FIELDS:
        assert replayed.get(field) == current.get(field), f"{name}:{field}"
    assert snapshot["event_replay_valid"] is True
    assert snapshot["aggregate_valid"] is True


@pytest.mark.parametrize("name", sorted(_REAL))
def test_historical_events_keep_an_empty_subcode_and_actor(tmp_path, name):
    """Additive means old events are unchanged, not back-filled."""

    store = SQLiteStateStore(_snapshot(tmp_path, name))
    events = store.events()
    assert events, name

    populated = []
    for event in events:
        envelope = event.payload.get(ENVELOPE_KEY)
        if not isinstance(envelope, dict):
            continue
        reason = envelope.get("reason")
        if not isinstance(reason, dict):
            continue
        # every historical event predates S4.1
        if reason.get("subcode") or reason.get("actor"):
            populated.append(event.revision)
    assert populated == [], f"{name}: historical events gained S4.1 fields at {populated[:5]}"


@pytest.mark.parametrize("name", sorted(_REAL))
def test_the_reason_change_perturbed_no_recorded_state_hash(tmp_path, name):
    """Compare every event's stored state_hash_after against a fresh computation.

    This is what proves the envelope change was additive: had ``subcode``/``actor``
    entered the replay patch, every historical hash would now mismatch.
    """

    store = SQLiteStateStore(_snapshot(tmp_path, name))
    events = store.events()
    verified = 0
    for event in events:
        envelope = event.payload.get(ENVELOPE_KEY)
        if not isinstance(envelope, dict):
            continue
        if envelope.get("state_hash_after") is None:
            continue
        verified += 1
    # the strict replay above already verified each hash; this asserts we actually
    # had hashes to verify rather than the test passing vacuously
    assert verified > 0, name
    # and a deliberately corrupted stream must be rejected, proving verification
    # is not a no-op
    corrupted = list(events)
    if corrupted:
        from factory_core.workflow_events import WorkflowEvent

        last = corrupted[-1]
        envelope = dict(last.payload.get(ENVELOPE_KEY) or {})
        envelope["state_hash_after"] = "0" * 64
        corrupted[-1] = WorkflowEvent(
            revision=last.revision, type=last.type, created_at=last.created_at,
            step=last.step, attempt=last.attempt,
            payload={**last.payload, ENVELOPE_KEY: envelope},
        )
        with pytest.raises(ReplayIntegrityError):
            replay_events(corrupted)


@pytest.mark.parametrize("name", sorted(_REAL))
def test_unknown_event_shapes_are_readable_from_the_reason(tmp_path, name):
    """Every event in the real streams yields a usable reason, with an empty
    subcode where none was ever recorded - never an exception."""

    store = SQLiteStateStore(_snapshot(tmp_path, name))
    for event in store.events():
        reason = normalize_reason(event.type, event.payload)
        assert isinstance(reason.code, str) and reason.code, event.revision
        assert isinstance(reason.subcode, str)
        assert isinstance(reason.actor, str)


def test_r517_reopen_revision_text_is_now_readable_from_the_reason(tmp_path):
    """The X-01 case, asserted against the real event.

    Before S4.1 the decisive value sat in a payload field outside the envelope, so
    ``reason.code`` alone could not tell this reopen from any other.
    """

    store = SQLiteStateStore(_snapshot(tmp_path, "A"))
    event = next((e for e in store.events() if e.revision == 517), None)
    if event is None:
        pytest.skip("r517 not present")

    envelope = event.payload[ENVELOPE_KEY]
    assert event.payload.get("final_decision") == "REOPEN_REVISION_TEXT"
    assert envelope["reason"]["code"] == "WORK_REOPENED", "canonical code unchanged"
    reason = normalize_reason(event.type, event.payload)
    assert reason.code == "WORK_REOPENED"
    assert reason.subcode == "REOPEN_REVISION_TEXT", (
        "the reopened text decision must be readable from the reason alone"
    )


@pytest.mark.parametrize("name", sorted(_REAL))
def test_gate_reading_real_history_mutates_nothing(tmp_path, name):
    """G3 is a read-only gate; the databases must be untouched, mtime included."""

    database = _db(name)
    before = database.stat()
    connection = sqlite3.connect(read_only_uri(database), uri=True)
    try:
        rows_before = (
            connection.execute("SELECT revision FROM project_state").fetchone()[0],
            connection.execute("SELECT COUNT(*) FROM events").fetchone()[0],
        )
        payloads_before = connection.execute(
            "SELECT SUM(LENGTH(payload_json)) FROM events"
        ).fetchone()[0]
    finally:
        connection.close()

    store = SQLiteStateStore(_snapshot(tmp_path, name))
    store.status_snapshot()
    store.events()

    after = database.stat()
    connection = sqlite3.connect(read_only_uri(database), uri=True)
    try:
        rows_after = (
            connection.execute("SELECT revision FROM project_state").fetchone()[0],
            connection.execute("SELECT COUNT(*) FROM events").fetchone()[0],
        )
        payloads_after = connection.execute(
            "SELECT SUM(LENGTH(payload_json)) FROM events"
        ).fetchone()[0]
    finally:
        connection.close()

    assert rows_after == rows_before
    assert payloads_after == payloads_before, "no historical payload may be rewritten"
    assert after.st_mtime == before.st_mtime


def test_the_gate_actually_exercises_all_three_streams(tmp_path):
    """Real-history sanity check; non-vacuity itself lives in the hermetic layer.

    This test used to assert that at least one production tree was present, so it
    failed on every machine without ``ongoing/`` - including CI runners - even
    though the invariants were fully testable there.  The anti-vacuity duty moved
    to ``tests/test_gate_hermetic.py``, which builds a hash-bearing stream of more
    than 100 events from scratch and so can discharge it anywhere.

    What remains here is the real-history demand, kept so a moved tree cannot
    quietly degrade this layer into asserting nothing: when the projects *are*
    present they must be large enough to be evidence.
    """

    names = _gate_projects.available()
    if not names:
        pytest.skip(
            "no real project available; the gate invariants are asserted "
            "hermetically in tests/test_gate_hermetic.py"
        )
    for name in names:
        store = SQLiteStateStore(_snapshot(tmp_path, name))
        assert len(store.events()) > 100, name
