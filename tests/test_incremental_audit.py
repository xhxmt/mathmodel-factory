from __future__ import annotations

import json
from pathlib import Path

from factory_core.adapters.infrastructure.commands import CommandResult
from factory_core.audit import AuditProfile, AuditStatus, IncrementalAuditService
from factory_core.audit.ledger import has_unresolved_blocking
from factory_core.cli import build_parser


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def lines(count: int) -> str:
    return "\n".join(f"line {index}" for index in range(count)) + "\n"


def valid_model(project: Path) -> None:
    write(project / "problem/problem_brief.md", "### 问题 1：测试问题\n")
    write(project / "model.md", lines(100))
    write(project / "symbol_table.md", lines(10))
    write(project / "assumption_ledger.md", lines(10))
    write(project / "modeling_scope_gate.md", "VERDICT: PASS\n")
    write(
        project / "claim_registry.json",
        json.dumps(
            {
                "contract_version": "claim-registry-v1",
                "questions": [
                    {
                        "id": "Q1",
                        "statement": "测试问题",
                        "source": {
                            "path": "problem/problem_brief.md",
                            "line": 1,
                        },
                        "required_roles": ["paper", "math", "execution"],
                    }
                ],
                "claims": [
                    {
                        "id": "Q1_MODEL_AND_RESULT",
                        "statement": "问题 1 有可审计模型与结果",
                        "question_ids": ["Q1"],
                        "required_roles": ["paper", "math", "execution"],
                        "artifacts": [
                            {
                                "path": "model.md",
                                "roles": ["paper", "math", "execution"],
                            }
                        ],
                    }
                ],
                "delivery_requirements": [],
            }
        )
        + "\n",
    )
    write(project / "quality_contract.json", '{"version":3,"claims":[],"anomaly_checks":[]}\n')


def valid_results(project: Path) -> None:
    valid_model(project)
    write(project / "solve_log.md", lines(20))
    write(
        project / "results/problem1/values.json",
        '{"status":"OPTIMAL","objective":1.0}\n',
    )
    write(project / "results/invariants.json", "{}\n")
    write(
        project / "results/canonical_results.json",
        json.dumps(
            {
                "project": project.name,
                "primary_method": "m1",
                "p1": {
                    "status": "OPTIMAL",
                    "value": 1.0,
                    "source_file": "results/problem1/values.json",
                },
            }
        )
        + "\n",
    )


class FakeRunner:
    def __init__(self, failures: set[str] | None = None) -> None:
        self.failures = failures or set()
        self.calls: list[str] = []

    def python(self, _root, _project, _script, _args, *, label, log_path, **_kwargs):
        self.calls.append(label)
        failed = label in self.failures
        write(Path(log_path), f"VERDICT: {'FAIL' if failed else 'PASS'}\n")
        return CommandResult(1 if failed else 0, not failed, Path(log_path))


def test_model_audit_is_cached_and_never_authorizes_delivery(tmp_path: Path) -> None:
    root = tmp_path / "factory"
    project = root / "ongoing" / "demo"
    valid_model(project)
    service = IncrementalAuditService(root, runner=FakeRunner())

    first = service.run_project(project, AuditProfile.MODEL)
    second = service.run_project(project, AuditProfile.MODEL)

    assert first.record.status is AuditStatus.PASS
    assert first.record.decision == "MODEL_READY"
    assert first.record.delivery_allowed is False
    assert second.record.reused is True
    assert second.execution.metadata["audit_reused"] is True
    profile_root = project / ".factory/audits/profiles/model"
    assert (profile_root / first.snapshot.snapshot_id / "snapshot.json").is_file()
    assert not (project / ".factory/audits/latest.json").exists()


def test_model_cache_invalidates_when_problem_contract_changes(
    tmp_path: Path,
) -> None:
    root = tmp_path / "factory"
    project = root / "ongoing" / "demo"
    valid_model(project)
    runner = FakeRunner()
    service = IncrementalAuditService(root, runner=runner)
    first = service.run_project(project, AuditProfile.MODEL)
    write(
        project / "problem/problem_brief.md",
        "### 问题 1：测试问题\n### 问题 2：新增问题\n",
    )

    second = service.run_project(project, AuditProfile.MODEL)

    assert first.record.status is AuditStatus.PASS
    assert second.record.status is AuditStatus.FAIL
    assert second.record.reused is False
    assert second.snapshot.snapshot_id != first.snapshot.snapshot_id
    assert "claim_registry_schema" in second.record.evidence["hard_failures"]


def test_failed_model_audit_updates_and_resolves_machine_ledger_rows(
    tmp_path: Path,
) -> None:
    root = tmp_path / "factory"
    project = root / "ongoing" / "demo"
    project.mkdir(parents=True)
    service = IncrementalAuditService(root, runner=FakeRunner())

    failed = service.run_project(project, "model")
    assert failed.record.status is AuditStatus.FAIL
    ledger = project / "audit_issue_ledger.md"
    assert has_unresolved_blocking(ledger) is True

    valid_model(project)
    passed = service.run_project(project, "model")

    assert passed.record.status is AuditStatus.PASS
    assert has_unresolved_blocking(ledger) is False
    text = ledger.read_text(encoding="utf-8")
    assert "AUDIT-MODEL-MODEL-ARTIFACTS" in text
    assert "RESOLVED" in text


def test_model_audit_rejects_invalid_claim_registry_schema(
    tmp_path: Path,
) -> None:
    root = tmp_path / "factory"
    project = root / "ongoing" / "demo"
    valid_model(project)
    write(project / "claim_registry.json", "{}\n")

    outcome = IncrementalAuditService(root, runner=FakeRunner()).run_project(
        project, "model"
    )

    assert outcome.record.status is AuditStatus.FAIL
    assert "claim_registry_schema" in outcome.record.evidence["hard_failures"]


def test_results_audit_rejects_incomplete_canonical_state(tmp_path: Path) -> None:
    root = tmp_path / "factory"
    project = root / "ongoing" / "demo"
    valid_model(project)
    write(project / "solve_log.md", lines(20))
    write(project / "results/problem1/values.json", '{"status":"OPTIMAL","value":1.0}\n')
    write(project / "results/canonical_results.json", '{"p1":{"status":"PARTIAL","value":1.0}}\n')

    outcome = IncrementalAuditService(root, runner=FakeRunner()).run_project(
        project, "results", checkpoint_step=5
    )

    assert outcome.record.status is AuditStatus.FAIL
    assert "canonical_results" in outcome.record.evidence["hard_failures"]
    assert outcome.record.evidence["repair_step"] == 5


def test_results_audit_pass_is_cached_for_same_checkpoint_snapshot(
    tmp_path: Path,
) -> None:
    root = tmp_path / "factory"
    project = root / "ongoing" / "demo"
    valid_results(project)
    runner = FakeRunner()
    service = IncrementalAuditService(root, runner=runner)

    first = service.run_project(project, "results", checkpoint_step=5)
    second = service.run_project(project, "results", checkpoint_step=5)

    assert first.record.status is AuditStatus.PASS
    assert first.record.decision == "RESULTS_VERIFIED"
    assert first.record.delivery_allowed is False
    assert second.record.reused is True
    assert second.snapshot.snapshot_id == first.snapshot.snapshot_id
    assert runner.calls == [
        "audit_results_provenance",
        "audit_results_invariants",
        "audit_results_spec_impl",
        "audit_results_quality_contract",
    ]


def test_incremental_profile_rejects_another_steps_checkpoint(
    tmp_path: Path,
) -> None:
    root = tmp_path / "factory"
    project = root / "ongoing" / "demo"
    valid_model(project)

    try:
        IncrementalAuditService(root, runner=FakeRunner()).run_project(
            project, "model", checkpoint_step=5
        )
    except ValueError as exc:
        assert "model" in str(exc)
        assert "4" in str(exc)
    else:
        raise AssertionError("model audit accepted a results checkpoint")


def test_paper_symbol_findings_are_warning_not_stage_blocker(tmp_path: Path) -> None:
    root = tmp_path / "factory"
    project = root / "ongoing" / "demo"
    valid_model(project)
    write(project / "demo_paper.tex", lines(30))
    write(project / "code_review.md", lines(20))
    runner = FakeRunner({"audit_paper_symbols"})

    outcome = IncrementalAuditService(root, runner=runner).run_project(
        project, "paper"
    )

    assert outcome.record.status is AuditStatus.PASS
    assert outcome.record.delivery_allowed is False
    assert outcome.record.evidence["warnings"] == ["symbols"]
    assert has_unresolved_blocking(project / "audit_issue_ledger.md") is False


def test_paper_audit_runs_deterministic_derived_artifact_gate(tmp_path: Path) -> None:
    root = tmp_path / "factory"
    project = root / "ongoing" / "demo"
    valid_model(project)
    write(project / "demo_paper.tex", lines(30))
    write(project / "code_review.md", lines(20))
    runner = FakeRunner()

    outcome = IncrementalAuditService(root, runner=runner).run_project(project, "paper")

    assert outcome.record.status is AuditStatus.PASS
    assert "audit_paper_derived_artifacts" in runner.calls
    assert len(outcome.record.evidence["draft_content_fingerprint"]) == 64
    assert outcome.record.evidence["paper_audit_input_fingerprint"] == (
        outcome.snapshot.snapshot_id
    )
    assert len(outcome.record.evidence["checker_contract_sha256"]) == 64


def test_model_audit_accepts_quality_contract_v4(tmp_path: Path) -> None:
    root = tmp_path / "factory"
    project = root / "ongoing" / "demo"
    valid_model(project)
    write(
        project / "quality_contract.json",
        json.dumps(
            {
                "version": 4,
                "claims": [],
                "anomaly_checks": [],
                "competitiveness_checks": [],
                "derived_artifacts": {"manifest": "results/derived_artifacts.json"},
            }
        )
        + "\n",
    )

    outcome = IncrementalAuditService(root, runner=FakeRunner()).run_project(
        project, "model"
    )

    assert outcome.record.status is AuditStatus.PASS


def test_audit_cli_exposes_all_profiles() -> None:
    args = build_parser().parse_args(
        [
            "audit",
            "ongoing/demo",
            "--profile",
            "results",
            "--checkpoint-step",
            "5",
            "--no-reuse",
        ]
    )

    assert args.profile == "results"
    assert args.checkpoint_step == 5
    assert args.no_reuse is True


def test_ledger_parser_supports_existing_column_order(tmp_path: Path) -> None:
    ledger = tmp_path / "audit_issue_ledger.md"
    write(
        ledger,
        "| id | title | severity | location | required action | notes | status | dependency |\n"
        "|---|---|---|---|---|---|---|---|\n"
        "| S11-B1 | issue | BLOCKING | paper.tex | fix | note | OPEN | Step 12 |\n",
    )

    assert has_unresolved_blocking(ledger) is True

    write(
        ledger,
        ledger.read_text(encoding="utf-8").replace("| OPEN |", "| RESOLVED |"),
    )
    assert has_unresolved_blocking(ledger) is False


class ScriptedRunner:
    """Each label maps to (returncode, log text); unlisted labels pass."""

    def __init__(self, outcomes: dict[str, tuple[int, str]]) -> None:
        self.outcomes = outcomes

    def python(self, _root, _project, _script, _args, *, label, log_path, **_kwargs):
        returncode, text = self.outcomes.get(label, (0, "VERDICT: PASS\n"))
        write(Path(log_path), text)
        return CommandResult(returncode, returncode == 0, Path(log_path))


DISK_FULL_TRACEBACK = (
    "checking provenance\n"
    "Traceback (most recent call last):\n"
    '  File "scripts/verify_provenance.py", line 88, in main\n'
    "    connection.execute(query)\n"
    "sqlite3.OperationalError: database or disk is full\n"
)
UNCAUGHT_TRACEBACK = (
    "Traceback (most recent call last):\n"
    '  File "scripts/verify_invariants.py", line 12, in <module>\n'
    "KeyError: 'p1'\n"
)


def test_a_checker_that_crashes_on_a_full_disk_is_indeterminate_not_a_finding(tmp_path: Path) -> None:
    root = tmp_path / "factory"
    project = root / "ongoing" / "demo"
    valid_results(project)
    service = IncrementalAuditService(
        root, runner=ScriptedRunner({"audit_results_provenance": (1, DISK_FULL_TRACEBACK)})
    )

    outcome = service.run_project(project, AuditProfile.RESULTS, checkpoint_step=5)

    assert outcome.record.status is AuditStatus.INDETERMINATE
    assert outcome.execution.error_class == "TRANSIENT_INCREMENTAL_AUDIT"
    check = next(item for item in outcome.record.evidence["checks"] if item["name"] == "provenance")
    assert check["indeterminate"] is True
    assert "no verdict" in check["detail"] and "disk is full" in check["detail"]
    assert outcome.record.evidence["indeterminate_checks"] == ["provenance"]


def test_an_uncaught_exception_or_signal_is_indeterminate(tmp_path: Path) -> None:
    for returncode, text in ((1, UNCAUGHT_TRACEBACK), (-9, ""), (137, "Killed\n")):
        root = tmp_path / f"factory{returncode}"
        project = root / "ongoing" / "demo"
        valid_results(project)
        service = IncrementalAuditService(
            root, runner=ScriptedRunner({"audit_results_invariants": (returncode, text)})
        )
        outcome = service.run_project(project, AuditProfile.RESULTS, checkpoint_step=5)
        assert outcome.record.status is AuditStatus.INDETERMINATE, returncode


def test_a_reported_finding_stays_a_failure(tmp_path: Path) -> None:
    root = tmp_path / "factory"
    project = root / "ongoing" / "demo"
    valid_results(project)
    # A script that reports a finding exits normally, even if its report
    # quotes an exception name.
    report = "VERDICT: FAIL\nmissing value for p1 (KeyError in source data)\n"
    service = IncrementalAuditService(
        root, runner=ScriptedRunner({"audit_results_invariants": (1, report)})
    )

    outcome = service.run_project(project, AuditProfile.RESULTS, checkpoint_step=5)

    assert outcome.record.status is AuditStatus.FAIL
    assert outcome.execution.error_class == "AUDIT_REPAIR_REQUIRED"


def test_a_determinate_finding_wins_over_a_crashed_sibling_check(tmp_path: Path) -> None:
    root = tmp_path / "factory"
    project = root / "ongoing" / "demo"
    valid_results(project)
    service = IncrementalAuditService(
        root,
        runner=ScriptedRunner(
            {
                "audit_results_provenance": (1, DISK_FULL_TRACEBACK),
                "audit_results_invariants": (1, "VERDICT: FAIL\n"),
            }
        ),
    )

    outcome = service.run_project(project, AuditProfile.RESULTS, checkpoint_step=5)

    assert outcome.record.status is AuditStatus.FAIL
    assert outcome.execution.metadata["failed_check"] == "invariants"
    assert outcome.record.evidence["indeterminate_checks"] == ["provenance"]


def test_any_uncaught_exception_name_is_recognised(tmp_path: Path) -> None:
    root = tmp_path / "factory"
    project = root / "ongoing" / "demo"
    valid_results(project)
    crash = (
        "Traceback (most recent call last):\n"
        '  File "scripts/verify_invariants.py", line 3, in <module>\n'
        "    raise CheckerCrash('boom')\n"
        "CheckerCrash: boom\n"
    )
    service = IncrementalAuditService(root, runner=ScriptedRunner({"audit_results_invariants": (1, crash)}))
    assert service.run_project(project, AuditProfile.RESULTS, checkpoint_step=5).record.status is AuditStatus.INDETERMINATE


def test_a_finding_that_quotes_resource_errors_is_still_a_finding(tmp_path: Path) -> None:
    root = tmp_path / "factory"
    project = root / "ongoing" / "demo"
    valid_results(project)
    report = (
        "solver log excerpt: MemoryError at iteration 40; database or disk is full\n"
        "the reported optimum is therefore not supported by the log\n"
        "VERDICT: FAIL\n"
    )
    service = IncrementalAuditService(root, runner=ScriptedRunner({"audit_results_invariants": (1, report)}))
    assert service.run_project(project, AuditProfile.RESULTS, checkpoint_step=5).record.status is AuditStatus.FAIL


def test_a_check_without_a_verdict_leaves_its_ledger_row_alone(tmp_path: Path) -> None:
    root = tmp_path / "factory"
    project = root / "ongoing" / "demo"
    valid_results(project)
    ledger = project / "audit_issue_ledger.md"

    IncrementalAuditService(root, runner=ScriptedRunner({})).run_project(
        project, AuditProfile.RESULTS, checkpoint_step=5
    )
    before = ledger.read_text(encoding="utf-8")
    assert "AUDIT-RESULTS-PROVENANCE" in before
    write(project / "solve_log.md", lines(21))  # new snapshot, no cache hit
    outcome = IncrementalAuditService(
        root, runner=ScriptedRunner({"audit_results_provenance": (1, DISK_FULL_TRACEBACK)})
    ).run_project(project, AuditProfile.RESULTS, checkpoint_step=5)

    assert outcome.record.status is AuditStatus.INDETERMINATE
    row = next(line for line in ledger.read_text(encoding="utf-8").splitlines() if "AUDIT-RESULTS-PROVENANCE" in line)
    assert row == next(line for line in before.splitlines() if "AUDIT-RESULTS-PROVENANCE" in line)
    assert "OPEN" not in row
    assert not has_unresolved_blocking(ledger)


class DerivedReportRunner(FakeRunner):
    """The derived-artifact checker writes a volatile stdout trace (it names a
    random temporary output directory) and, when asked, a stable report."""

    def __init__(self, *, write_report: bool) -> None:
        super().__init__()
        self.write_report = write_report
        self.runs = 0

    def python(self, _root, _project, _script, args, *, label, log_path, **kwargs):
        result = super().python(_root, _project, _script, args, label=label, log_path=log_path, **kwargs)
        if label == "audit_paper_derived_artifacts":
            self.runs += 1
            write(Path(log_path), f"DERIVED_RUNTIME_REPORT: /tmp/paper-factory-derived-{self.runs}\nVERDICT: PASS\n")
            if self.write_report:
                target = Path(args[args.index("--json-out") + 1])
                write(target, '{"passed": true}\n')
        return result


def test_derived_artifact_check_records_its_stable_report_not_the_trace(tmp_path: Path) -> None:
    # s16: the final paper checks hashed the stdout trace, whose random temp
    # directory changed the final snapshot on every recheck.
    root = tmp_path / "factory"
    project = root / "ongoing" / "demo"
    valid_model(project)
    write(project / "demo_paper.tex", lines(30))
    write(project / "code_review.md", lines(20))
    service = IncrementalAuditService(root, runner=DerivedReportRunner(write_report=True))

    checks = service.paper_checks(project)
    first = (project / "derived_artifacts_verification.latest.json").read_bytes()
    second_checks = service.paper_checks(project)

    recorded = next(check for check in checks if check.name == "derived_artifacts")
    assert recorded.report == "derived_artifacts_verification.latest.json"
    assert (project / "derived_artifacts_verification.latest.json").read_bytes() == first
    assert next(c for c in second_checks if c.name == "derived_artifacts").report == recorded.report
    # The runtime trace is still kept in full.
    assert "paper-factory-derived-2" in (
        project / "logs/native_paper_derived_artifacts.log"
    ).read_text(encoding="utf-8")


def test_a_checker_that_writes_no_report_records_its_log(tmp_path: Path) -> None:
    root = tmp_path / "factory"
    project = root / "ongoing" / "demo"
    valid_model(project)
    write(project / "demo_paper.tex", lines(30))
    write(project / "code_review.md", lines(20))
    # A report left by an earlier run must not be credited to this one.
    write(project / "derived_artifacts_verification.latest.json", '{"passed": true}\n')
    service = IncrementalAuditService(root, runner=DerivedReportRunner(write_report=False))

    checks = service.paper_checks(project)

    recorded = next(check for check in checks if check.name == "derived_artifacts")
    assert recorded.report == "logs/native_paper_derived_artifacts.log"
    assert not (project / "derived_artifacts_verification.latest.json").exists()


def test_an_unremovable_stale_report_fails_the_check_not_the_audit(tmp_path: Path) -> None:
    # A directory where the report should be makes unlink raise; the check
    # records a command error instead of crashing the whole audit.
    root = tmp_path / "factory"
    project = root / "ongoing" / "demo"
    valid_model(project)
    write(project / "demo_paper.tex", lines(30))
    write(project / "code_review.md", lines(20))
    (project / "derived_artifacts_verification.latest.json").mkdir()
    service = IncrementalAuditService(root, runner=DerivedReportRunner(write_report=False))

    checks = service.paper_checks(project)

    recorded = next(check for check in checks if check.name == "derived_artifacts")
    assert recorded.passed is False
    assert recorded.detail.startswith("command error:")
