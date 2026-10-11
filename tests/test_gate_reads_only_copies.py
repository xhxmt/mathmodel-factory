"""No test may hand a real project to Factory code.

``SQLiteStateStore`` migrates a database **in place** on any read path: ``load``,
``status_snapshot``, ``events`` and ``dirty_flags`` all call ``_upgrade_schema``,
which promotes a generation-9 database to 10 and commits.  So a test that passes a
real project directory to Factory code is a test that can rewrite it.

That was the state of the real-history layer until this guard: ``test_gate_g3_replay``
built ``SQLiteStateStore(_requires(name))``, and ``test_gate_g2_solver`` and
``test_solver_reconcile`` fed ``_requires(name)`` to ``evaluate_solver_jobs``,
which opens a store.  It happened not to bite, because the three projects that
layer points at are all generation 10, so ``_upgrade_schema`` returns early.  The
safety was incidental - it depended on *which* projects ``A``, ``B`` and ``R``
happen to be - and ``PF_GATE_PROJECTS_ROOT`` makes that worse, since it lets the
layer be pointed at any checkout whose generations nobody checked.

CI cannot catch this either: the trees are absent there, so every test that would
do the damage is skipped.  A green run is not evidence.

Two things fix it, and this file asserts both:

* the real-history layer reads a **copy** (``_gate_projects.snapshot``), so the
  original can only ever be opened by raw SQLite;
* no test passes a real-path accessor into a store-backed API.

The second is the one that keeps the first from being undone by a later edit.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent

#: Accessors that resolve to the ORIGINAL project on disk.
REAL_PATH_ACCESSORS = (
    r"_gate_projects\.require",
    r"_gate_projects\.real_path",
    r"_gate_projects\.real_db",
    r"_requires",
    r"_db",
)

#: Calls that open a store, or reach one, and therefore migrate on read.
STORE_BACKED = (
    "SQLiteStateStore",
    "evaluate_solver_jobs",
    "completion_blockers",
    "dirty_flags",
    "status_snapshot",
)

FORBIDDEN = re.compile(
    r"(?:" + "|".join(STORE_BACKED) + r")\s*\(\s*(?:" + "|".join(REAL_PATH_ACCESSORS) + r")\b"
)

#: This file names the patterns in order to forbid them.
SELF = Path(__file__).name


def _test_sources():
    for path in sorted(TESTS.glob("test_*.py")):
        if path.name == SELF:
            continue
        yield path, path.read_text(encoding="utf-8")


def test_no_test_hands_a_real_project_to_store_backed_code():
    """The invariant, checked over every test file at once."""

    offenders = []
    for path, text in _test_sources():
        for match in FORBIDDEN.finditer(text):
            line = text[: match.start()].count("\n") + 1
            offenders.append(f"{path.name}:{line}  {match.group(0)}")

    assert offenders == [], (
        "these tests open a real project with Factory code, which migrates a "
        "generation-9 database in place:\n  " + "\n  ".join(offenders) + "\n"
        "Use _gate_projects.snapshot(name, tmp_path) instead."
    )


def test_the_real_history_layer_actually_uses_copies():
    """Non-vacuity: the previous test would pass if nothing read real history.

    The real-history tests must go through ``_gate_projects.snapshot``, so this
    asserts that they do - otherwise "no real path reaches a store" could be
    satisfied by a layer that stopped exercising real projects altogether.
    """

    users = [
        path.name
        for path, text in _test_sources()
        if "_gate_projects.snapshot(" in text
    ]
    assert len(users) >= 4, f"expected the real-history tests to use copies, got {users}"

    from tests import _gate_projects

    assert callable(_gate_projects.snapshot)
    # and the accessor it wraps still resolves to the original, so the copy is a
    # copy of something real rather than of nothing
    assert callable(_gate_projects.require)


def test_a_generation_nine_original_is_never_migrated(tmp_path):
    """The behaviour, not just the source pattern.

    A genuine generation-9 database is built by ``bde49712``'s own code, then
    opened through the copy path - which really does migrate the copy - and the
    original is shown to be untouched afterwards, down to its mtime.  Before the
    copies were introduced the real-history tests did the first half of this to
    the original, and it only escaped notice because the three projects that layer
    points at happen to be generation 10 already.
    """

    import shutil

    from factory_core.storage import SCHEMA_VERSION, SQLiteStateStore

    from tests.test_gate_g3_version_compat import make_v9_project, raw_facts, sha256

    original = make_v9_project(tmp_path / "original")
    database = original / ".factory" / "state.db"
    assert raw_facts(database)["physical_schema"] == 9, "the fixture must be a real v9"

    before_sha = sha256(database)
    before_mtime = database.stat().st_mtime_ns

    copy = tmp_path / "copy"
    (copy / ".factory").mkdir(parents=True)
    shutil.copy2(database, copy / ".factory" / "state.db")
    SQLiteStateStore(copy).load()

    assert raw_facts(copy / ".factory" / "state.db")["physical_schema"] == SCHEMA_VERSION, (
        "reading the copy is what performs the migration"
    )
    assert sha256(database) == before_sha, "the original must not be rewritten"
    assert database.stat().st_mtime_ns == before_mtime, "not even its mtime"


def test_snapshot_copies_without_touching_the_source(tmp_path, monkeypatch):
    """The helper's own contract, on a tree this test controls.

    Pointing ``PF_GATE_PROJECTS_ROOT`` at a synthetic root is also the case that
    makes the whole concern concrete: that variable lets the real-history layer be
    aimed at any checkout, whose projects' generations nobody has checked.
    """

    import shutil

    from tests import _gate_projects

    root = tmp_path / "root"
    project = root / "ongoing" / _gate_projects.PROJECTS["A"]
    (project / ".factory").mkdir(parents=True)
    (project / ".factory" / "state.db").write_bytes(b"pretend-database")
    (project / ".factory" / "solver_receipts").mkdir()
    (project / ".factory" / "solver_receipts" / "one.json").write_text("{}")
    (project / "work").mkdir()
    (project / "work" / "huge.bin").write_bytes(b"x" * 1024)

    monkeypatch.setenv(_gate_projects.GATE_ROOT_ENV, str(root))

    before = {
        path.relative_to(project).as_posix(): path.read_bytes()
        for path in project.rglob("*")
        if path.is_file()
    }

    snapshot = _gate_projects.snapshot("A", tmp_path / "destination")

    # the copy carries .factory/ and nothing else
    assert (snapshot / ".factory" / "state.db").read_bytes() == b"pretend-database"
    assert (snapshot / ".factory" / "solver_receipts" / "one.json").is_file()
    assert not (snapshot / "work").exists(), "copying work/ would move a huge tree"

    after = {
        path.relative_to(project).as_posix(): path.read_bytes()
        for path in project.rglob("*")
        if path.is_file()
    }
    assert after == before, "snapshot must be read-only with respect to the source"


def test_the_snapshot_helper_is_the_only_way_in():
    """``snapshot`` copies ``.factory/`` and nothing else.

    That is everything the store and the solver evaluator read, and copying the
    whole project would mean moving a 979 MiB ``work/`` tree per test.
    """

    from tests import _gate_projects

    source = _gate_projects.__file__
    text = Path(source).read_text(encoding="utf-8")
    assert 'copytree(source / ".factory"' in text
    assert "shutil.copytree" in text
