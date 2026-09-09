import json

from scripts.canonical_claims import accept, verify, DERIVED, LEDGER
from scripts.solver_job_receipt import (
    build_submission_receipt, build_completion_receipt, write_receipt,
)


def candidate(tmp_path, name, thickness, job):
    path = tmp_path / f"results/{name}/values.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"thickness": thickness}))
    script = tmp_path / f"{job}.py"
    script.write_text("pass")
    submitted = tmp_path / f"{job}.submitted.json"
    write_receipt(submitted, build_submission_receipt(project_dir=tmp_path, job_id=job,
        backend="local", runtime="python", script=script, workdir=tmp_path, argv=[],
        max_time_seconds=1, requested_at=1, output_paths=[path]))
    completed = tmp_path / f"{job}.completed.json"
    write_receipt(completed, build_completion_receipt(project_dir=tmp_path,
        submission_path=submitted, status="COMPLETED", finished_at=2, result_refs={}))
    return path.relative_to(tmp_path).as_posix() + "::thickness", completed.name


def test_explicit_canonical_version_blocks_mixed_summary_and_body(tmp_path):
    first, receipt = candidate(tmp_path, "problem3B", 5.392895418547354, "first")
    second, _ = candidate(tmp_path, "step12", 21.309545353610655, "second")
    paper = tmp_path / f"{tmp_path.name}_paper.tex"
    paper.write_text("\\begin{document}\nSummary 5.392895; body 21.309545\n\\end{document}")
    accepted = accept(tmp_path, "Q3B_THICKNESS", first, receipt, [second], "explicit model choice")
    assert accepted["job_id"] == "first"
    assert any("MIXED_CANONICAL_VERSION" in error for error in verify(tmp_path))
    paper.write_text("\\begin{document}\nSummary 5.392895; body 5.392895\n\\end{document}")
    assert verify(tmp_path) == []
    before = (tmp_path / DERIVED).read_bytes()
    accept(tmp_path, "Q3B_THICKNESS", first, receipt, [second], "explicit model choice")
    assert (tmp_path / DERIVED).read_bytes() == before
    assert accepted["version"] == json.loads((tmp_path / LEDGER).read_text())["claims"]["Q3B_THICKNESS"]["version"]


def test_canonical_upstream_mutation_invalidates_derived_values(tmp_path):
    first, receipt = candidate(tmp_path, "problem3B", 5.39, "first")
    accept(tmp_path, "Q3B_THICKNESS", first, receipt, [], "accepted")
    (tmp_path / "results/problem3B/values.json").write_text('{"thickness":21.3}')
    assert any("upstream source changed" in e for e in verify(tmp_path))


def test_key_results_without_accepted_version_fail_closed(tmp_path):
    (tmp_path / "results").mkdir()
    (tmp_path / "results/summary.json").write_text('{"key_results":[{"value":5.39,"label":"thickness"}]}')
    assert verify(tmp_path)[0].startswith("CANONICAL_CLAIM_VERSION_MISSING")
