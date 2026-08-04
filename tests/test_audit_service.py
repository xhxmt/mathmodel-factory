from __future__ import annotations

import fcntl
import json
from pathlib import Path

from factory_core.adapters.infrastructure.commands import CommandResult
from factory_core.audit import AuditStatus, FinalAuditService
from factory_core.cli import build_parser
from factory_core.domain import ExecutionResult, StepContext


class FakeValidator:
    @staticmethod
    def _gate2_resume(_project: Path, _decision: str) -> int:
        return 3


class RecordingRunner:
    def __init__(self) -> None:
        self.labels: list[str] = []

    def _ok(self, project: Path, label: str) -> CommandResult:
        self.labels.append(label)
        log = project / "logs" / f"{label}.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("ok\n", encoding="utf-8")
        return CommandResult(0, True, log)

    def run(self, project, argv, *, label, **_kwargs):
        project = Path(project)
        if str(argv[0]).endswith("compile_paper.sh"):
            (project / f"{project.name}_paper.pdf").write_bytes(b"%PDF fixture\n")
        return self._ok(project, label)

    def python(self, _root, project, script, args, *, label, **_kwargs):
        project = Path(project)
        if script.endswith("pdf_visual_gate.py"):
            output = project / "judge_outputs/visual_gate.json"
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text('{"status":"PASS"}\n', encoding="utf-8")
        elif script.endswith("judge_decision_router.py"):
            output = project / "judge_outputs/decision_route.json"
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(
                '{"new_decision":"PASS","effective_decision":"PASS"}\n',
                encoding="utf-8",
            )
        return self._ok(project, label)


class PassingJudge:
    def __init__(self) -> None:
        self.packet_calls = 0
        self.judge_calls = 0

    def prepare_packets(self, _context):
        self.packet_calls += 1
        return ExecutionResult.succeeded(packets_prepared=True)

    def execute_prepared(self, context):
        self.judge_calls += 1
        outputs = context.project_dir / "judge_outputs"
        outputs.mkdir(parents=True, exist_ok=True)
        (outputs / "aggregate.json").write_text(
            '{"verdict":"PASS"}\n', encoding="utf-8"
        )
        (context.project_dir / "judge_evaluation.md").write_text(
            "VERDICT: PASS\n", encoding="utf-8"
        )
        return ExecutionResult.succeeded(
            judge_completed=True,
            judge_verdict="PASS",
            gate2_delivery_override=False,
        )

    def execute(self, context):
        return self.execute_prepared(context)


def make_context(project: Path) -> StepContext:
    return StepContext(project, project.name, 16, 1, 3_600, 0)


def test_final_audit_writes_snapshot_without_publishing(tmp_path: Path) -> None:
    root = tmp_path / "factory"
    project = root / "ongoing" / "demo"
    project.mkdir(parents=True)
    runner = RecordingRunner()
    judge = PassingJudge()
    service = FinalAuditService(
        root,
        judge,
        FakeValidator(),
        runner,
        fingerprinter=lambda _project, _base: "a" * 64,
    )

    outcome = service.run(make_context(project))

    assert outcome.record.status is AuditStatus.PASS
    assert outcome.record.profile == "final"
    assert outcome.record.delivery_allowed is True
    assert outcome.snapshot.snapshot_id == "a" * 64
    assert (project / ".factory/audits" / ("a" * 64) / "snapshot.json").is_file()
    latest = json.loads(
        (project / ".factory/audits/latest.json").read_text(encoding="utf-8")
    )
    assert latest["status"] == "PASS"
    assert (project / "judge_outputs/final_submission.sha256").read_text(
        encoding="ascii"
    ).strip() == "a" * 64
    assert not (root / "papers").exists()
    assert "package_submission" not in runner.labels
    assert "delivery_cleanup" not in runner.labels


def test_final_audit_reuses_valid_pass_for_same_snapshot(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "factory"
    project = root / "ongoing" / "demo"
    project.mkdir(parents=True)
    runner = RecordingRunner()
    judge = PassingJudge()
    service = FinalAuditService(
        root,
        judge,
        FakeValidator(),
        runner,
        fingerprinter=lambda _project, _base: "b" * 64,
    )
    first = service.run(make_context(project))
    assert first.record.status is AuditStatus.PASS
    monkeypatch.setattr(
        "scripts.judgment_receipt.verify_receipt",
        lambda *_args, **_kwargs: (True, []),
    )

    second = service.run(make_context(project), compile_pdf=False)

    assert second.record.status is AuditStatus.PASS
    assert second.record.reused is True
    assert second.execution.metadata["audit_reused"] is True
    assert judge.packet_calls == 2
    assert judge.judge_calls == 1


def test_final_audit_does_not_reuse_non_final_profile(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "factory"
    project = root / "ongoing" / "demo"
    project.mkdir(parents=True)
    runner = RecordingRunner()
    judge = PassingJudge()
    snapshot_id = "d" * 64
    service = FinalAuditService(
        root,
        judge,
        FakeValidator(),
        runner,
        fingerprinter=lambda _project, _base: snapshot_id,
    )
    first = service.run(make_context(project))
    monkeypatch.setattr(
        "scripts.judgment_receipt.verify_receipt",
        lambda *_args, **_kwargs: (True, []),
    )
    cached_path = (
        project / ".factory" / "audits" / snapshot_id / "latest.json"
    )
    cached = json.loads(cached_path.read_text(encoding="utf-8"))
    cached["profile"] = "model"
    cached_path.write_text(json.dumps(cached) + "\n", encoding="utf-8")

    second = service.run(make_context(project), compile_pdf=False)

    assert first.record.status is AuditStatus.PASS
    assert second.record.reused is False
    assert second.record.profile == "final"
    assert judge.judge_calls == 2


def test_audit_cli_is_independent_command() -> None:
    args = build_parser().parse_args(
        ["audit", "ongoing/demo", "--no-compile", "--no-reuse"]
    )

    assert args.command == "audit"
    assert args.no_compile is True
    assert args.no_reuse is True


def test_concurrent_audit_returns_busy_without_overwriting_latest(
    tmp_path: Path,
) -> None:
    root = tmp_path / "factory"
    project = root / "ongoing" / "demo"
    lock_path = project / ".factory" / "audits" / ".lock"
    lock_path.parent.mkdir(parents=True)
    service = FinalAuditService(
        root,
        PassingJudge(),
        FakeValidator(),
        RecordingRunner(),
        fingerprinter=lambda _project, _base: "c" * 64,
    )

    with lock_path.open("a+", encoding="ascii") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        outcome = service.run(make_context(project))

    assert outcome.record.status is AuditStatus.INDETERMINATE
    assert outcome.record.decision == "AUDIT_BUSY"
    assert outcome.execution.error_class == "TRANSIENT_AUDIT_BUSY"
    assert not (project / ".factory/audits/latest.json").exists()


def test_no_judge_ablation_is_visible_and_never_fabricates_pass(
    tmp_path: Path, monkeypatch
) -> None:
    class AblatedJudge:
        @staticmethod
        def execute(_context):
            return ExecutionResult.succeeded(ablation="ABLATE_NO_JUDGE")

    root = tmp_path / "factory"
    project = root / "ongoing" / "demo"
    project.mkdir(parents=True)
    monkeypatch.setenv("ABLATE_NO_JUDGE", "1")
    service = FinalAuditService(
        root,
        AblatedJudge(),
        FakeValidator(),
        RecordingRunner(),
        fingerprinter=lambda _project, _base: "d" * 64,
    )

    outcome = service.run(make_context(project))

    assert outcome.record.status is AuditStatus.OVERRIDDEN
    assert outcome.record.decision == "ABLATE_NO_JUDGE"
    assert outcome.record.judge_completed is False
    marker = json.loads(
        (project / "judge_outputs/final_submission.ablation.json").read_text(
            encoding="utf-8"
        )
    )
    assert marker["judge_executed"] is False
    assert marker["quality_pass_fabricated"] is False
    from scripts.workflow_state import final_audit_is_current

    assert final_audit_is_current(project) is True
