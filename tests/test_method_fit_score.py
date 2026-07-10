import json
from pathlib import Path

from scripts.method_fit_score import learn_from_history


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def seed_project(complete: Path, name: str, status: str) -> None:
    project = complete / name
    write(project / "problem" / "problem_brief.md", "最大化动态资源分配目标。\n")
    write(project / "chosen_method.md", "PRIMARY: m1 family=MILP\n")
    write(
        project / "delivery_manifest.json",
        json.dumps({"status": status, "evaluation": {"passed": status == "CURRENT_PASS"}}),
    )


def test_history_learning_excludes_non_current_pass_projects(tmp_path):
    complete = tmp_path / "complete"
    write(
        tmp_path / "method_library" / "index.json",
        json.dumps([{"method": "MILP", "path": "method_library/optimization/milp.md"}]),
    )
    seed_project(complete, "legacy", "LEGACY_DELIVERED")

    model = learn_from_history(str(complete))

    assert model["summary"]["total_projects"] == 0


def test_history_learning_requires_explicit_human_or_award_quality_label(tmp_path):
    complete = tmp_path / "complete"
    write(
        tmp_path / "method_library" / "index.json",
        json.dumps([{"method": "MILP", "path": "method_library/optimization/milp.md"}]),
    )
    seed_project(complete, "current", "CURRENT_PASS")

    model = learn_from_history(str(complete))

    assert model["summary"]["total_projects"] == 0
