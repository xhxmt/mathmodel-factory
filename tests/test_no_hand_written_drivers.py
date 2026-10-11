"""Keep the hand-written driver layer from growing back.

G4.5c migrated the twenty drivers that existed.  This file makes that a standing
constraint rather than a one-off cleanup, by testing the checker that enforces
it: ``scripts/check_no_hand_written_drivers.py``.

Two layers, for the same reason the acceptance gates have two:

* the **detection logic** is tested here against synthetic sources, so it runs
  everywhere including CI - every forbidden pattern must be caught and every
  allowed one must not be;
* the **assertion against the real project tree** runs where that tree exists and
  skips otherwise, because ``work/`` is not in this repository.

The criterion is semantic, not a count of ``.run(``.  Of the eighty-six
occurrences in the tree, twenty-five are ``subprocess.run``, thirteen are ordinary
method calls, four are the engine's own pipeline, and thirty-eight are inside
comments, docstrings or strings.  What must be zero is a *workflow advance*
outside ``service.advance_bounded``.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from scripts.check_no_hand_written_drivers import scan

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The three files that still contain a workflow advance, and why the exemption
#: list is enumerated here rather than pattern-matched: an exemption that could
#: grow silently would defeat the check.
#:
#: All three are ``.before.py`` copies under
#: ``work/final_workflow_resume_20260911/``, preserved by the preparation scripts
#: as the pre-migration source of drivers that have since been migrated.  Their
#: content is redundant with the verified PRE archive, and they are kept because
#: renaming them would invalidate the POST archive that seals the migrated tree.
EXEMPTED_BACKUPS = frozenset(
    {
        "final_workflow_resume_20260911/entry_freshness_reconciliation/run_controller.before.py",
        "final_workflow_resume_20260911/step13_cli_repair/controller.before.py",
        "final_workflow_resume_20260911/user_authorized_step13_skip/controller.before.py",
    }
)

#: Where the tree lives on the machine that has it.  Overridable so the check can
#: be pointed at another checkout.
PROJECT_TREE = Path(
    "/home/tfisher/paper_factory/ongoing/cumcm_2026_a_fable_pro_20260910/work"
)


def _tree(tmp_path, name: str, source: str) -> Path:
    root = tmp_path / "work"
    root.mkdir(exist_ok=True)
    (root / name).write_text(textwrap.dedent(source), encoding="utf-8")
    return root


FORBIDDEN = [
    pytest.param(
        "service_engine_run.py",
        """
        from factory_core.service import FactoryService
        state = FactoryService(ROOT).engine(P).run(max_steps=1)
        """,
        "workflow_advance",
        id="factoryservice-engine-run",
    ),
    pytest.param(
        "engine_run_max_steps.py",
        """
        engine = service.engine(P)
        state = engine.run(max_steps=1)
        """,
        "workflow_advance",
        id="engine-run-max-steps",
    ),
    pytest.param(
        "private_registry.py",
        """
        from factory_core.registry import StepRegistry
        class SingleAttemptRegistry(StepRegistry):
            def get(self, step_id):
                return base.get(step_id)
        engine.registry = SingleAttemptRegistry()
        """,
        "registry_shim",
        id="private-step-registry",
    ),
    pytest.param(
        "replace_ceilings.py",
        """
        import dataclasses
        definition = dataclasses.replace(definition, max_attempts=1, max_reopens=0)
        """,
        "registry_shim",
        id="dataclasses-replace-ceilings",
    ),
    pytest.param(
        "journal_with_advance.py",
        """
        from factory_core.service import FactoryService
        (W / 'progress.json').write_text('{}')
        state = FactoryService(ROOT).engine(P).run(max_steps=1)
        """,
        "progress_journal_with_advance",
        id="progress-journal-beside-advance",
    ),
]

ALLOWED = [
    pytest.param(
        "subprocess_call.py",
        """
        import subprocess
        done = subprocess.run(['git', 'status'], capture_output=True)
        """,
        id="subprocess-run",
    ),
    pytest.param(
        "ordinary_methods.py",
        """
        result = self.runner.run(context)
        outcome = self._pipeline.run(state)
        value = release_q23.run(log)
        """,
        id="ordinary-method-calls",
    ),
    pytest.param(
        "migrated_driver.py",
        '''
        """Migrated to the bounded-run contract.

        Replaces ``engine.run(max_steps=1)`` with service.advance_bounded, and the
        old class SingleAttemptRegistry shim with contract ceilings.
        """
        from factory_core.bounded_run import BoundedRunContract
        outcome = service.advance_bounded(P, contract)
        ''',
        id="a-migrated-drivers-own-documentation",
    ),
    pytest.param(
        "advance_bounded_only.py",
        """
        from factory_core.service import FactoryService
        outcome = FactoryService(ROOT).advance_bounded(P, contract)
        """,
        id="the-supported-entry-point",
    ),
]


@pytest.mark.parametrize("name, source, expected_kind", FORBIDDEN)
def test_each_forbidden_pattern_is_caught(tmp_path, name, source, expected_kind):
    report = scan(_tree(tmp_path, name, source))
    assert report["ok"] is False
    assert expected_kind in {entry["kind"] for entry in report["violations"]}


@pytest.mark.parametrize("name, source", ALLOWED)
def test_each_allowed_pattern_is_not_flagged(tmp_path, name, source):
    report = scan(_tree(tmp_path, name, source))
    assert report["ok"] is True, report["violations"]


def test_a_migrated_drivers_documentation_is_never_a_violation(tmp_path):
    """The checker must not read a script's own explanation of what it removed.

    This is not hypothetical: an earlier metric counted ``class
    SingleAttemptRegistry(`` inside a migrated file's comment and reported the
    shim as still present.
    """

    root = _tree(
        tmp_path,
        "documented.py",
        '''
        """The old shape was:

            engine = FactoryService(ROOT).engine(P)
            class SingleAttemptRegistry(StepRegistry):
                ...

        and it is now service.advance_bounded.
        """
        outcome = service.advance_bounded(P, contract)
        ''',
    )
    report = scan(root)
    assert report["ok"] is True, report["violations"]
    assert report["residual"], "the mentions are still recorded, just not as violations"


def test_an_exemption_suppresses_exactly_one_file(tmp_path):
    root = _tree(
        tmp_path,
        "controller.before.py",
        """
        from factory_core.service import FactoryService
        state = FactoryService(ROOT).engine(P).run(max_steps=1)
        """,
    )
    assert scan(root)["ok"] is False
    assert scan(root, exemptions=frozenset({"controller.before.py"}))["ok"] is True


def test_the_project_tree_has_no_hand_written_driver():
    """The real assertion, on the machine that has the tree.

    Skipped in CI, where ``work/`` does not exist.  The detection logic above is
    what CI enforces; this is the check that has to be run before and after any
    new driver work on the server.
    """

    if not PROJECT_TREE.is_dir():
        pytest.skip(f"project tree unavailable at {PROJECT_TREE}")

    report = scan(PROJECT_TREE, exemptions=EXEMPTED_BACKUPS)
    assert report["ok"] is True, "\n".join(
        f"{entry['file']}:{entry['line']} [{entry['kind']}] {entry['snippet']}"
        for entry in report["violations"]
    )
    assert report["files_scanned"] >= 200

    # and the exemptions are exactly the three enumerated backups, no more
    if report["exemptions"]:
        assert set(report["exemptions"]) == set(EXEMPTED_BACKUPS)
