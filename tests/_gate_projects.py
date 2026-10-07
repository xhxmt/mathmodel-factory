"""Shared locator for the real-project G2/G3/S5 gate layers.

The gate tests assert properties of *real production history* (projects A, B and
R).  That history only exists on the machine that ran those projects, so the
gate has always had two layers:

* a **hermetic** layer (``tests/test_gate_hermetic.py`` plus the ``tmp_path``
  tests in ``tests/test_solver_reconcile.py``) that proves the general
  invariants and runs everywhere, including CI;
* a **real-history regression** layer (this module's callers) that re-asserts the
  answers recorded when the gate was built, and can only run where the trees are.

Before this module the real-history layer hard-coded three absolute server paths
with no override, so in CI every real-history test skipped and
``test_the_gate_actually_exercises_all_three_streams`` failed - correctly, since
it exists to stop the gate passing vacuously, but with no way to satisfy it.

Two things are therefore separated here:

* ``PF_GATE_PROJECTS_ROOT`` names a directory containing ``ongoing/`` so the
  real-history layer can be pointed at any checkout (default: the historical
  absolute path, keeping server behaviour identical);
* an absent project makes the real-history tests **skip**, never fail.  The
  non-vacuity duty moves to the hermetic layer, which is always present.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

#: Directory that contains ``ongoing/``.  Override to run the real-history layer
#: against another checkout.
GATE_ROOT_ENV = "PF_GATE_PROJECTS_ROOT"

_DEFAULT_ROOT = Path("/home/tfisher/paper_factory")

#: gate name -> project directory name under ``<root>/ongoing``
PROJECTS = {
    "A": "cumcm_2026_a_fable_pro_20260910",
    "B": "cumcm_2025_b_gpt_formal_20260908t153023z",
    "R": "cumcm_2025_b_codex_luna_stability_20260817_run4",
}


def gate_root() -> Path:
    """The directory containing ``ongoing/``."""

    override = os.environ.get(GATE_ROOT_ENV)
    return Path(override) if override else _DEFAULT_ROOT


def real_path(name: str) -> Path:
    """The path of a real gate project, whether or not it exists."""

    return gate_root() / "ongoing" / PROJECTS[name]


def real_db(name: str) -> Path:
    return real_path(name) / ".factory" / "state.db"


def available() -> list[str]:
    """Gate projects present in this environment, in stable order."""

    return [name for name in sorted(PROJECTS) if real_path(name).is_dir()]


def require(name: str) -> Path:
    """A gate project, or a skip when this environment does not have it.

    The real-history layer must not fail for absence: it re-asserts recorded
    answers, and those answers are only meaningful where the history is.
    """

    path = real_path(name)
    if not path.is_dir():
        pytest.skip(
            f"real project {name} unavailable at {path}; "
            f"set {GATE_ROOT_ENV} to a directory containing ongoing/"
        )
    return path
