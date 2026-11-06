"""Liveness regression gate: replay recorded workflow decisions.

The fixtures under ``tests/fixtures/run_history`` are content-free extracts of
16 real runs (2026-08-03 .. 2026-10-16).  These tests pin three things:

* the fixtures stay free of credentials, home paths and paper content;
* the replay is faithful: change sets recorded under the classifier that is
  checked out today reproduce the decision the engine actually made;
* liveness never regresses: a rule may decide fewer rewinds and fewer
  non-content attempt losses than the 8e8f872 baseline, never more.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import pytest

from scripts import extract_run_history as extract_module
from scripts import replay_run_history as replay

FIXTURES = Path(__file__).parent / "fixtures" / "run_history"
BASELINE = json.loads((FIXTURES / "_baseline_8e8f872.json").read_text(encoding="utf-8"))
BASELINE_CLASSIFIER = BASELINE["current_classifier"]

# Change sets recorded under the baseline classifier whose recorded decision
# legitimately differs from the checked-in engine, each with its evidence.
KNOWN_FIDELITY_EXCEPTIONS = {
    ("cumcm_2026_a_postmerge_run2", 774): (
        "an operator-applied local engine.py patch exempted the revision subtask from "
        "the upstream rewind from 2026-10-16T12:48Z (pf-acceptance "
        "records/p4/FINDINGS_20261016_livelocks_and_masking.json); main never carried it"
    ),
}


@pytest.fixture(scope="module")
def fixtures():
    return replay.load_fixtures(FIXTURES)


@pytest.fixture(scope="module")
def report(fixtures):
    return replay.build_report(fixtures, "current", replay.load_labels(FIXTURES))


def test_fixture_set_is_the_sixteen_recorded_runs(fixtures):
    assert len(fixtures) == 16
    assert {fixture["schema"] for fixture in fixtures} == {"factory-run-history-fixture-v1"}
    assert sum(len(fixture["change_sets"]) for fixture in fixtures) == 346


def test_fixtures_carry_no_credentials_home_paths_or_addresses():
    forbidden = re.compile(
        r"sk[-_][A-Za-z0-9_\-]{8,}"
        r"|[Bb]earer\s+[A-Za-z0-9._\-]{8,}"
        r"|/home/[A-Za-z0-9_\-]+"
        r"|/Users/[A-Za-z0-9_\-]+"
        r"|/root(?:/|\b)"
        r"|[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"
    )
    for path in sorted(FIXTURES.glob("*.json")):
        hits = forbidden.findall(path.read_text(encoding="utf-8"))
        assert not hits, f"{path.name}: {sorted(set(hits))[:5]}"


def test_every_label_points_at_a_recorded_failure(fixtures):
    events = {(fixture["run"], event["rev"]): event for fixture in fixtures for event in fixture["events"]}
    labels = replay.load_labels(FIXTURES)
    assert labels, "labels.json must not be empty"
    for run, entries in labels.items():
        for rev, label in entries.items():
            event = events.get((run, rev))
            assert event is not None, f"{run} rev {rev} is not in the fixture"
            assert event["type"] in {"STEP_FAILED", "RETRY_SCHEDULED"}, (run, rev, event["type"])
            assert label["cause"] in {"infrastructure", "provider", "workflow"}
            assert label["evidence"].strip()


def test_time_accounting_reproduces_the_baseline(report):
    totals = report["totals"]
    for key in ("span_h", "kept_h", "redone_h", "reopened_mid_h", "idle_h"):
        assert totals[key] == pytest.approx(BASELINE["totals"][key], abs=0.01), key
    assert totals["completed"] == 2
    assert totals["kept_share"] == pytest.approx(0.0733, abs=0.0005)


def test_recorded_decisions_match_the_baseline(report):
    decisions = report["decisions"]
    assert decisions["change_sets"] == BASELINE["decisions"]["change_sets"]
    assert decisions["evaluated_change_sets"] == BASELINE["decisions"]["evaluated_change_sets"]
    assert decisions["recorded_reopens"] == 49


def test_replay_reproduces_decisions_recorded_under_the_checked_in_classifier(report):
    """The replay is only a regression gate if it agrees with the real engine.

    The fixtures hold 47 commit-time change sets recorded under classifier
    ``9f8ba8f601d1`` (main at 8e8f872).  While that classifier is checked out,
    every one of them must replay to the decision the engine recorded.  Once
    a later phase changes the classifier on purpose, this agreement is no
    longer expected and the monotonicity gate below takes over.
    """

    if report["current_classifier"] != BASELINE_CLASSIFIER:
        pytest.skip(
            f"classifier changed from {BASELINE_CLASSIFIER} to {report['current_classifier']}; "
            "fidelity was established against the 8e8f872 baseline"
        )
    fidelity = report["decisions"]["fidelity"]
    assert fidelity["change_sets_under_current_classifier"] == 47
    mismatched = {(row["run"], row["rev"]) for row in fidelity["mismatches"]}
    assert mismatched == set(KNOWN_FIDELITY_EXCEPTIONS)


def test_liveness_never_regresses_against_the_baseline(report):
    """A rule may rewind less and lose fewer attempts to non-content causes."""

    decided = report["decisions"]["decided_reopens"]
    baseline_decided = BASELINE["decisions"]["decided_reopens"]
    assert decided <= baseline_decided, f"rewinds rose from {baseline_decided} to {decided}"
    losses = report["failures"]["non_content_consuming_attempts"]
    baseline_losses = BASELINE["failures"]["non_content_consuming_attempts"]
    assert losses <= baseline_losses, f"non-content attempt losses rose from {baseline_losses} to {losses}"


def test_failed_attempts_never_trigger_a_rewind_decision(report):
    for row in report["decisions"]["rows"]:
        if row["event_type"] not in {"STEP_SUCCEEDED", "STAGE_SEMANTIC_REOPENED"}:
            assert row["evaluated"] is False
            assert row["decided"]["reopen"] is False


def test_rebuild_manifests_restores_paper_semantic_keys():
    before, after, passthrough = replay.rebuild_manifests(
        [
            {"artifact": "paper/x_paper.tex", "flag": "MATH_DIRTY", "owner_stage": 8,
             "baseline": "aaaa", "current": "bbbb"},
            {"artifact": "models/m1/solve.py", "flag": "MODEL_DIRTY", "owner_stage": 3,
             "baseline": "MISSING", "current": "cccc"},
            {"artifact": "MISSING_STAGE_INPUT_BASELINE", "flag": "RESULT_DIRTY", "owner_stage": 4,
             "baseline": "MISSING", "current": "dddd"},
        ]
    )
    assert before == {"@paper:paper/x_paper.tex:math": "aaaa"}
    assert after == {"@paper:paper/x_paper.tex:math": "bbbb", "models/m1/solve.py": "cccc"}
    assert [row["artifact"] for row in passthrough] == ["MISSING_STAGE_INPUT_BASELINE"]


def _synthetic_project(root: Path) -> Path:
    project = root / "demo_project"
    database = project / ".factory" / "state.db"
    database.parent.mkdir(parents=True)
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        create table project_state (status text, revision integer, last_completed_step integer);
        create table events (revision integer, type text, created_at real, step integer,
                             attempt integer, payload_json text);
        create table dirty_causes (cause_id text, flag text, owner_stage integer, cause_revision integer,
                                   cause_artifact text, baseline_fingerprint text, current_fingerprint text,
                                   classifier_contract_sha256 text);
        create table solver_jobs (job_id text, owner_stage integer);
        """
    )
    connection.execute("insert into project_state values ('failed', 3, 4)")
    home = str(Path.home())
    rows = [
        (1, "STEP_STARTED", 100.0, 5, 1, {"stage": 4, "subtask": "solve", "_workflow": {"state_hash_after": "x" * 64}}),
        (2, "PROMPT_INPUT_BOUND", 101.0, 5, 1, {"prompt_inputs_sha256": "y" * 64}),
        (3, "STEP_FAILED", 160.0, 5, 1, {
            "stage": 4, "error_class": "PERMANENT_BACKEND_UNAVAILABLE",
            "reason": f"log at {home}/run/log.txt key=sk-abcdefghijklmnop",
        }),
    ]
    for revision, kind, created, step, attempt, payload in rows:
        connection.execute(
            "insert into events values (?, ?, ?, ?, ?, ?)",
            (revision, kind, created, step, attempt, json.dumps(payload)),
        )
    connection.execute(
        "insert into dirty_causes values ('c1', 'MODEL_DIRTY', 3, 3, 'models/m1/solve.py', 'MISSING', ?, ?)",
        ("f" * 64, "e" * 64),
    )
    connection.execute("insert into solver_jobs values ('local_python_1', 4)")
    connection.commit()
    connection.close()
    return project


def test_extractor_keeps_decisions_and_drops_evidence_and_secrets(tmp_path):
    project = _synthetic_project(tmp_path)
    fixture = extract_module.extract(project)
    assert fixture["schema"] == "factory-run-history-fixture-v1"
    assert [event["type"] for event in fixture["events"]] == ["STEP_STARTED", "STEP_FAILED"]
    failed = fixture["events"][1]
    assert failed["error_class"] == "PERMANENT_BACKEND_UNAVAILABLE"
    assert str(Path.home()) not in failed["reason"]
    assert "sk-abcdefghijklmnop" not in failed["reason"]
    serialized = json.dumps(fixture)
    assert "x" * 64 not in serialized and "y" * 64 not in serialized
    assert fixture["change_sets"] == [
        {
            "rev": 3,
            "classifier": ["eeeeeeeeeeee"],
            "changes": [
                {"owner_stage": 3, "flag": "MODEL_DIRTY", "artifact": "models/m1/solve.py",
                 "baseline": "MISSING", "current": "ffffffffffff"},
            ],
        }
    ]
    assert fixture["solver_job_owners"] == {"local_python_1": 4}


def test_extractor_never_writes_to_the_source_database(tmp_path):
    project = _synthetic_project(tmp_path)
    database = project / ".factory" / "state.db"
    before = database.read_bytes()
    extract_module.extract(project)
    assert database.read_bytes() == before
    assert sorted(path.name for path in database.parent.iterdir()) == ["state.db"]


def _events(*rows):
    return [{"rev": index + 1, "type": kind, "ts": ts, **({"step": step} if step is not None else {})}
            for index, (kind, ts, step) in enumerate(rows)]


def test_a_paused_attempt_that_resumes_and_succeeds_is_kept_work():
    # Recovery promotes the interrupted Step after RESUMED without a new STEP_STARTED.
    events = _events(("STEP_STARTED", 0, 5), ("PAUSED", 600, None), ("RESUMED", 4200, None),
                     ("STEP_SUCCEEDED", 4800, 5))
    [attempt] = replay.attempt_ledger(events)
    assert (attempt.outcome, attempt.fate) == ("ok", "kept")
    assert attempt.hours == pytest.approx(1200 / 3600)
    assert attempt.segments() == [(0, 600), (4200, 4800)]


def test_a_paused_attempt_that_is_restarted_ends_at_its_pause():
    events = _events(("STEP_STARTED", 0, 4), ("PAUSED", 600, None), ("RESUMED", 4200, None),
                     ("STEP_STARTED", 4300, 4), ("STEP_SUCCEEDED", 5000, 4))
    first, second = replay.attempt_ledger(events)
    assert (first.outcome, first.end, first.pauses) == ("paused", 600, [])
    assert (second.outcome, second.fate) == ("ok", "kept")


def test_step_reopened_ends_the_active_attempt():
    events = _events(("STEP_STARTED", 0, 13), ("STEP_REOPENED", 900, None), ("STEP_STARTED", 1000, 11),
                     ("STEP_SUCCEEDED", 1500, 11))
    first, second = replay.attempt_ledger(events)
    assert (first.outcome, first.end) == ("reopened_mid", 900)
    assert second.outcome == "ok"


def test_extractor_reads_committed_wal_frames_without_touching_the_source(tmp_path):
    project = _synthetic_project(tmp_path)
    database = project / ".factory" / "state.db"
    writer = sqlite3.connect(database)
    try:
        writer.execute("pragma journal_mode=wal")
        writer.execute("pragma wal_autocheckpoint=0")
        writer.execute(
            "insert into events values (4, 'STEP_STARTED', 170.0, 5, 2, ?)", (json.dumps({"stage": 4}),)
        )
        writer.commit()
        wal = Path(f"{database}-wal")
        assert wal.stat().st_size > 0
        before = {path.name: path.read_bytes() for path in database.parent.iterdir()}
        fixture = extract_module.extract(project)
        after = {path.name: path.read_bytes() for path in database.parent.iterdir()}
    finally:
        writer.close()
    assert [event["rev"] for event in fixture["events"]] == [1, 3, 4]
    assert after == before


def test_other_accounts_home_paths_are_redacted():
    text = extract_module.sanitize_text("/home/alice/x /Users/bob/y /root/.config/z")
    assert text == "~/x ~/y ~/.config/z"
    assert extract_module.sanitize_text("/home/josé/run /Users/张三/log") == "~/run ~/log"


def test_non_content_failures_and_their_terminal_class_consume_no_content_attempt():
    events = _events(
        ("STEP_STARTED", 0, 10), ("RETRY_SCHEDULED", 60, 10),
        ("STEP_STARTED", 120, 10), ("STEP_FAILED", 180, 10),
        ("STEP_STARTED", 240, 11), ("STEP_FAILED", 300, 11),
    )
    events[1]["error_class"] = "TRANSIENT_INCREMENTAL_AUDIT"
    events[3]["error_class"] = "PERMANENT_AUDIT_INFRASTRUCTURE"
    events[5]["error_class"] = "AUDIT_REPAIR_REQUIRED"
    records = replay.failure_records({"run": "demo", "events": events}, {})
    assert [record["consumed_attempt"] for record in records] == [False, False, True]


def test_stage_catalog_v2_removes_the_paper_segment_loops(fixtures):
    """Phase 2 exit condition, replayed on the 16 recorded runs.

    v2 must remove the abstract/polish math loop (Stage 9 -> 8), the final-audit
    projection loop (Stage 10 -> 8), and the reopens a revision caused only by
    editing documentation (Stage 8 -> 3/6 with no computation input changed).
    """

    decided = [
        row for row in replay.replay_decisions(fixtures, replay.RULES["v2"])
        if row["evaluated"] and row["decided"]["reopen"]
    ]
    current = [
        row for row in replay.replay_decisions(fixtures, replay.RULES["current"])
        if row["evaluated"] and row["decided"]["reopen"]
    ]
    pairs = {(row["stage"], row["decided"]["owner_stage"]) for row in decided}

    assert len(decided) <= 27 < len(current)
    assert (9, 8) not in pairs and (10, 8) not in pairs
    assert (8, 6) not in pairs
    # Every remaining Stage 8 -> 3/4 reopen changed executable model code or a
    # solver result, which phase 4 handles; none is documentation alone.
    from factory_core.stage_catalog_v2_policy import FORWARD_REPAIR_DOCUMENTS

    by_key = {(c.run, c.rev): c for f in fixtures for c in replay.change_sets(f)}
    for row in decided:
        if row["stage"] == 8 and row["decided"]["owner_stage"] in (3, 4):
            artifacts = {change["artifact"] for change in by_key[(row["run"], row["rev"])].changes}
            assert artifacts - FORWARD_REPAIR_DOCUMENTS, artifacts
