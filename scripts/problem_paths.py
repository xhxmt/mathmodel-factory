from __future__ import annotations

from pathlib import Path


def resolve_problem_constraints_path(project_dir: str | Path) -> Path:
    """Return the canonical constraints file, with legacy fallback support."""
    problem_dir = Path(project_dir) / "problem"
    for name in ("feasibility_constraints.md", "constraints.md"):
        candidate = problem_dir / name
        if candidate.is_file():
            return candidate
    return problem_dir / "feasibility_constraints.md"
