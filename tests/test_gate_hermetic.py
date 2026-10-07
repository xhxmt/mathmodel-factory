"""G2/G3 hermetic layer: the gate invariants that must hold in every environment.

The real-history layer (the ``tests/_gate_projects.py`` callers) re-asserts
answers recorded on the machine that produced projects A/B/R.  Those trees only
exist there, so that layer skips in CI.

This file is its counterpart.  It builds a project from scratch and proves the
same *invariants*, so the gate can never pass vacuously merely because the
production history is absent - which is exactly what
``test_gate_g3_replay.py::test_the_gate_actually_exercises_all_three_streams``
was guarding, and why it failed on CI runners that have no ``ongoing/`` trees.

Covered here:

  * a >100-event stream replays, hash-verified, and matches current state
  * hash verification is not a no-op: a tampered ``state_hash_after`` is rejected
  * every event yields a usable reason
  * pre-S4.1 reasons stay readable, with an empty subcode/actor
  * the X-01 lift (``final_decision`` -> ``subcode``) works
  * reading the stream is pure - revision, event count, payload bytes and mtime

The solver-relevance half of the gate is covered hermetically by the
``tmp_path`` tests in ``tests/test_solver_reconcile.py``, which exercise every
``WorkflowRelevance`` value without any real project.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from factory_core.solver_reconcile import BLOCKING_RELEVANCE, PROVEN_RELEVANCE
from factory_core.storage import SQLiteStateStore
from factory_core.workflow_events import (
    ENVELOPE_KEY,
    REPLAY_FIELDS,
    ReplayIntegrityError,
    WorkflowEvent,
    normalize_reason,
    replay_events,
    replay_state,
)

import _gate_projects

#: How many versioned events the built stream must carry.  The real gate asserts
#: ``len(store.events()) > 100`` for each production project; the hermetic stream
#: has to be at least as demanding or it could not stand in for that check.
_MIN_EVENTS = 100


def _build(root: Path, *, events: int = 120) -> Path:
    """A project whose event stream is versioned and hash-verifiable."""

    root.mkdir(parents=True, exist_ok=True)
    store = SQLiteStateStore(root)
    store.initialize(project_id="gate-hermetic", project_type="modeling")
    state = store.load()
    for index in range(events):
        state = store.transition(
            expected_revision=state.revision,
            event_type="STEP_SUCCEEDED",
            changes={"last_completed_step": index % 20, "active_step": None},
            payload={"step": index % 20, "index": index},
        )
    return root


@pytest.fixture(scope="module")
def hermetic_project(tmp_path_factory) -> Path:
    return _build(tmp_path_factory.mktemp("gate-hermetic") / "project")


# --------------------------------------------------------- replay compatibility
def test_a_built_stream_replays_and_matches_current_state(hermetic_project):
    """The strongest compatibility assertion, without needing real history.

    ``replay_events`` verifies every event's state hash by default, so a reason
    or envelope change that perturbed the patch would raise here rather than
    pass silently.
    """

    store = SQLiteStateStore(hermetic_project)
    snapshot = store.status_snapshot()
    replayed = replay_events(snapshot["events"])  # verify_hashes=True by default
    current = replay_state(snapshot["state"])

    mismatches = [
        field
        for field in REPLAY_FIELDS
        if replayed.get(field) != current.get(field)
    ]
    assert mismatches == []
    assert snapshot["event_replay_valid"] is True
    assert snapshot["aggregate_valid"] is True


def test_hash_verification_is_not_a_no_op(hermetic_project):
    """A deliberately corrupted stream must be rejected.

    Without this the compatibility assertion above could pass because hashing
    was never actually applied.
    """

    store = SQLiteStateStore(hermetic_project)
    events = store.events()
    assert events

    last = events[-1]
    envelope = dict(last.payload[ENVELOPE_KEY])
    assert envelope.get("state_hash_after") is not None, "no hashes to verify"
    envelope["state_hash_after"] = "0" * 64

    corrupted = list(events)
    corrupted[-1] = WorkflowEvent(
        revision=last.revision,
        type=last.type,
        created_at=last.created_at,
        step=last.step,
        attempt=last.attempt,
        payload={**last.payload, ENVELOPE_KEY: envelope},
    )

    with pytest.raises(ReplayIntegrityError):
        replay_events(corrupted)


def test_every_event_yields_a_usable_reason(hermetic_project):
    """Never an exception, and always the three string fields the gate reads."""

    store = SQLiteStateStore(hermetic_project)
    events = store.events()
    assert len(events) > _MIN_EVENTS

    for event in events:
        reason = normalize_reason(event.type, event.payload)
        assert isinstance(reason.code, str) and reason.code, event.revision
        assert isinstance(reason.subcode, str)
        assert isinstance(reason.actor, str)


# --------------------------------------------------------------- reason shape
@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"reason": {"code": "STEP_SUCCEEDED"}}, id="reason-with-code-only"),
        pytest.param({"reason": "STEP_SUCCEEDED"}, id="bare-string-reason"),
        pytest.param({}, id="no-reason-at-all"),
        pytest.param({"error_class": "SOME_FAILURE"}, id="error-class-only"),
    ],
)
def test_pre_s41_reasons_stay_readable_with_an_empty_subcode_and_actor(payload):
    """Additive means old events are unchanged, not back-filled.

    Every shape A/B/R actually contain predates S4.1, so the gate requires an
    empty subcode/actor rather than an exception.  These are the shapes; the
    real-history layer asserts it against the production events themselves.
    """

    reason = normalize_reason("STEP_SUCCEEDED", payload)
    assert reason.code
    assert reason.subcode == ""
    assert reason.actor == ""


def test_the_x01_lift_makes_the_reopen_decision_readable():
    """The mechanism behind r517: ``final_decision`` was outside the envelope, so
    ``reason.code`` alone could not tell this reopen from any other.

    The real r517 event is asserted in the real-history layer; this pins the
    mechanism wherever the tree is absent.
    """

    payload = {
        "reason": {"code": "WORK_REOPENED"},
        "final_decision": "REOPEN_REVISION_TEXT",
    }
    reason = normalize_reason("WORK_REOPENED", payload)
    assert reason.code == "WORK_REOPENED", "canonical code unchanged"
    assert reason.subcode == "REOPEN_REVISION_TEXT", (
        "the reopened text decision must be readable from the reason alone"
    )
    assert reason.actor == ""


def test_a_populated_reason_keeps_its_subcode_and_actor():
    """The other direction: an S4.1 event does carry them."""

    reason = normalize_reason(
        "PAUSED",
        {"reason": {"code": "PAUSED", "subcode": "OPERATOR", "actor": "operator"}},
    )
    assert reason.code == "PAUSED"
    assert reason.subcode == "OPERATOR"
    assert reason.actor == "operator"


# ------------------------------------------------------------------- pure read
def test_reading_the_stream_mutates_nothing(hermetic_project):
    """G3 is a read-only gate; not even the mtime may move."""

    database = hermetic_project / ".factory" / "state.db"

    def observe():
        stat = database.stat()
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        try:
            rows = (
                connection.execute("SELECT revision FROM project_state").fetchone()[0],
                connection.execute("SELECT COUNT(*) FROM events").fetchone()[0],
                connection.execute("SELECT SUM(LENGTH(payload_json)) FROM events").fetchone()[0],
            )
        finally:
            connection.close()
        return stat, rows

    before_stat, before_rows = observe()

    store = SQLiteStateStore(hermetic_project)
    store.status_snapshot()
    store.events()
    replay_events(store.status_snapshot()["events"])

    after_stat, after_rows = observe()
    assert after_rows == before_rows
    assert after_stat.st_mtime == before_stat.st_mtime
    assert after_stat.st_size == before_stat.st_size


# ------------------------------------------------------------- non-vacuity
def test_the_gate_has_non_vacuous_coverage(hermetic_project):
    """The guard the real-history layer used to carry, made satisfiable.

    The previous guard asserted that at least one production tree was present,
    so it failed on any machine without ``ongoing/`` - including CI - even
    though the invariants themselves were fully testable.  Non-vacuity is
    enforced here instead: the built stream must be large, versioned and
    hash-bearing, and any production tree that *is* present must still satisfy
    the original ``> 100 events`` demand.
    """

    store = SQLiteStateStore(hermetic_project)
    events = store.events()
    assert len(events) > _MIN_EVENTS, "the hermetic stream is too small to be evidence"
    enveloped = [
        event for event in events if isinstance(event.payload.get(ENVELOPE_KEY), dict)
    ]
    assert len(enveloped) == len(events), "every event must carry a replay envelope"
    assert all(
        event.payload[ENVELOPE_KEY].get("state_hash_after") is not None
        for event in enveloped
    ), "no state hashes, so replay verification would be a no-op"

    # the solver half of the gate is hermetic too, and must stay that way
    assert BLOCKING_RELEVANCE & PROVEN_RELEVANCE == frozenset()

    for name in _gate_projects.available():
        real_events = SQLiteStateStore(_gate_projects.require(name)).events()
        assert len(real_events) > _MIN_EVENTS, name
