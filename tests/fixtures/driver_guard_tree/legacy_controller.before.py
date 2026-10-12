"""An exempted ``.before.py`` copy, kept because renaming invalidates the archive.

The real tree keeps three of these: the pre-migration source of drivers that were
migrated afterwards, preserved so the sealed POST archive still describes the files
it was taken from.  The fixture reproduces one so that CI exercises the exemption path
and the "an empty scan is not a clean one" rule together.

Its specimen is behind ``if __name__ == "__main__"`` on purpose.  A fixture is *data
for a scanner*, and a bare module-level specimen would advance the real project the
path points at if anything ever imported it - a test fixture must not be able to
mutate production.  The checker reads text, so the patterns are found either way.
"""

from __future__ import annotations

from pathlib import Path

from factory_core.service import FactoryService

R = Path("/home/tfisher/paper_factory")
P = R / "ongoing/cumcm_2026_a_fable_pro_20260910"
W = P / "work/legacy_controller"


def main() -> None:
    import json

    record = {"status": "ready"}
    (W / "progress.json").write_text(json.dumps(record), encoding="utf-8")
    state = FactoryService(R).engine(P).run(max_steps=1)
    print(state.status.value)


if __name__ == "__main__":
    main()
