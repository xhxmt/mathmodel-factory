"""An ordinary helper beside a migrated driver: no workflow advance here.

The retired shape was reached through a bound name::

    service = FactoryService(ROOT)
    state = service.engine(P).run(max_steps=1)

That much is documentation, and the checker must not read it as a binding: the
live code below contains ordinary method calls and ``subprocess.run`` only.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def rebuild(path: Path) -> None:
    runner = subprocess.run(["pdflatex", "-interaction=nonstopmode", str(path)], check=False)
    if runner.returncode:
        raise SystemExit(f"compile failed: {runner.returncode}")


def summarize(service, project) -> str:
    report = service.inspect(project)
    return report.status.value
