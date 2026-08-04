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
