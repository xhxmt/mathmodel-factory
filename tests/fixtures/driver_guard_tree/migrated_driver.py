"""A representative migrated driver, committed so CI can scan a real tree.

G4.5c migrated the twenty drivers that existed on the server, and ``work/`` is not
in this repository, so the checker's only always-available subject is a synthetic
one.  These four files reproduce the shapes the real tree contains - the migrated
form, an ordinary helper, a class whose name merely ends in ``Engine``, and one
exempted ``.before.py`` copy - so that "the checker clears the tree it is pointed
at" is asserted in CI instead of being skipped there.

Migrated to the bounded-run contract.  What this file used to hand-roll and what
replaced it:

    assert status == 'ready' and active_step == 5        -> kept as an explicit guard
    class SingleAttemptRegistry(StepRegistry)            -> max_attempts_per_step
    (W / 'progress.json').write_text(...)                -> BoundedRunResult
    engine.run(max_steps=1)                              -> service.advance_bounded
"""

from __future__ import annotations

from pathlib import Path

from factory_core.bounded_run import BoundedRunContract, RunPolicy
from factory_core.service import FactoryService
from factory_core.storage import SQLiteStateStore

ROOT = Path("/home/tfisher/paper_factory")
P = ROOT / "ongoing/cumcm_2026_a_fable_pro_20260910"


def main() -> None:
    service = FactoryService(ROOT)
    state = SQLiteStateStore(P).load()
    if state.status.value != "ready":
        raise SystemExit("refusing: the project is not ready")
    contract = BoundedRunContract(
        expected_revision=state.revision,
        expected_cursor=(state.active_stage, state.active_subtask, state.source_step_id),
        max_subtasks=1,
        actor="operator",
        run_policy=RunPolicy.BOUNDED_SUBTASKS,
    )
    outcome = service.advance_bounded(P, contract)
    print(outcome.to_dict()["outcome"])


if __name__ == "__main__":
    main()
