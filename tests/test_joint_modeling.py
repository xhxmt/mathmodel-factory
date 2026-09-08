from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from factory_core.adapters.infrastructure.process import ProcessResult
from factory_core.adapters.models.backends import ModelRequest
from factory_core.adapters.models.dispatcher import ModelDispatcher
from factory_core.domain import (
    ExecutionResult, PrepareResult, RevisionConflict, StepContext, ValidationResult, WorkflowStatus,
)
from factory_core.engine import FactoryEngine
from factory_core.joint_modeling import (
    ATTESTATIONS, CANDIDATE_GATE, MODEL, RISK_ATTESTATION, RISK_GATE,
    JointModelingError, accepted_response, candidate_subject, configuration_blocker,
    configure, consultation_action, consultation_bundle, consultation_view,
    current_synthesis, ensure_package, modeling_prompt_context, package_evidence,
    parse_json, policy, risk_review, selection_evidence, status_view,
    validate_response, validate_synthesis, verify_execution,
)
from factory_core.joint_modeling_executor import JointClaudeBackend
from factory_core.registry import ModelBackendRegistry, StepDefinition, StepRegistry
from factory_core.service import FactoryService
from factory_core.steps.joint_modeling import JointModelingStep
from factory_core.storage import SQLiteStateStore
from factory_core.workflow_events import replay_events, replay_state
from web.backend.selection_service import build_step3_options, write_selection_decision


def write(project, relative, text):
    path = project / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class ClaudeSupervisor:
    def __init__(self, result="{}", model=MODEL, returncode=0):
        self.result = result
        self.model = model
        self.returncode = returncode
        self.requests = []

    def run(self, request):
        self.requests.append(request)
        request.stdout_path.parent.mkdir(parents=True, exist_ok=True)
        request.stdout_path.write_text(json.dumps({
            "type": "result", "is_error": False, "result": self.result,
            "modelUsage": {self.model: {"inputTokens": 20}},
        }) + "\n", encoding="utf-8")
        return ProcessResult(self.returncode, False, 0.1, 123)


def model_request(project, purpose="step2_proposal_1", *, isolated=False):
    return ModelRequest(project_dir=project, step_id=2, attempt=1, prompt=f"AGENT_KEY: {purpose}\nmodel this problem",
                        model=MODEL, timeout_seconds=30, hang_timeout_seconds=10, isolated=isolated)


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.delenv("CODEX_ONLY", raising=False)
    monkeypatch.setenv("CLAUDE_CLI_PATH", sys.executable)
    path = tmp_path / "ongoing" / "demo"
    path.mkdir(parents=True)
    store = SQLiteStateStore(path)
    state = store.initialize(project_id="demo", project_type="modeling", runtime_generation="native_v2", last_completed_step=1)
    configure(path, enabled=True, expected_revision=state.revision, actor="human")
    write(path, "viable_streams.md", "## Stream m1:\nstream one\n## Stream m2:\nstream two\n")
    write(path, "research_brief.md", "Evidence from the official problem.\n" * 30)
    write(path, "problem/source.md", "Minimize cost subject to nonnegative allocations.\n")
    for stream in (1, 2):
        write(path, f"m{stream}_spec.md", f"# m{stream}\nconstraint x >= 0\n" + "Validated modeling detail.\n" * 35)
        write(path, f"m{stream}_demo_result.json", '{"status":"OPTIMAL","runtime_seconds":5}')
        write(path, f"m{stream}_critique.md", "VERDICT: VALIDATED\nBound feasible example.\n")
        for role in ("proposal", "critic"):
            result = JointClaudeBackend(path, supervisor=ClaudeSupervisor()).execute(model_request(path, f"step2_{role}_{stream}"))
            assert result.returncode == 0
    return path


def pending_request(project, *, gate=CANDIDATE_GATE, subject=None):
    package = ensure_package(project, gate, subject or candidate_subject(project))
    store = SQLiteStateStore(project)
    engine = FactoryEngine(project, store=store, registry=StepRegistry())
    state = engine._await_action(store.load(), consultation_action(package).to_dict(), reason="review", evidence=package_evidence(package))
    request = state.pending_action["metadata"]["human_decision"]
    return package, request


def pro_answer(package, request, *, findings=None):
    return {
        "request_id": request["request_id"], "generation": request["generation"],
        "subject_fingerprint": request["subject_fingerprint"],
        "package_sha256": package["package_sha256"], "nonce": package["nonce"],
        "summary": "Recommend the validated first candidate, after checking the constraint.",
        "recommended_candidate_ids": ["m1"],
        "findings": findings if findings is not None else [{
            "id": "F1", "candidate_ids": ["m1"], "severity": "LOW",
            "summary": "Retain the nonnegative constraint.", "evidence_path": "m1_spec.md",
            "evidence_quote": "constraint x >= 0", "requires_second_review": False,
        }],
    }


def accept_pro(project, package, request, answer=None):
    answer = pro_answer(package, request) if answer is None else answer
    resolution = {key: request[key] for key in ("request_id", "generation", "subject_fingerprint", "options_fingerprint")}
    resolution.update(gate=request["gate"], answer=json.dumps(answer), source="manual-cli",
                      attestations={**{key: True for key in ATTESTATIONS}, RISK_ATTESTATION: True})
    state = SQLiteStateStore(project).load()
    FactoryService(project.parents[1]).resolve(project, resolution, expected_revision=state.revision)
    return answer


class OriginalStep:
    requires_prompt_input_receipt = True

    def __init__(self):
        self.executions = 0

    def prepare(self, context):
        from factory_core.steps.gates import prepare_human_gates
        return prepare_human_gates(context.project_dir, context.step_id)

    def execute(self, context):
        self.executions += 1
        return ExecutionResult.succeeded()

    def validate(self, context):
        return ValidationResult.valid()


def synthesize(project):
    package, request = pending_request(project)
    response = accept_pro(project, package, request)
    synthesis = {"summary": "The constraint is retained; choose the candidate manually.", "items": [{
        "finding_id": "F1", "action": "ACCEPT", "rationale": "The official nonnegativity requirement supports this.",
        "proposed_change": "", "pro_quote": "nonnegative constraint", "human_needed": False,
    }]}
    backend = JointClaudeBackend(project, supervisor=ClaudeSupervisor(json.dumps(synthesis)))
    original = OriginalStep()
    step = JointModelingStep(original, 3, project, backend)
    context = StepContext(project, project.name, 3, 1, 60, SQLiteStateStore(project).load().revision)
    assert step.prepare(context).ready
    assert step.execute(context).returncode == 0
    return step, context, original, response


def choose(project):
    step, context, original, response = synthesize(project)
    validation = step.validate(context)
    assert validation.pending_action.gate == "step3"
    store = SQLiteStateStore(project)
    FactoryEngine(project, store=store, registry=StepRegistry())._await_action(
        store.load(), validation.pending_action.to_dict(), reason="choose", evidence=validation.evidence)
    decision = write_selection_decision(project, gate="step3", selected_option_id="m1", selected_aux_id="NONE",
                                        source="manual-cli", reason="I reviewed both advisors and choose m1.")
    for name in ("symbol_table.md", "assumption_ledger.md", "modeling_scope_gate.md"):
        write(project, name, "VERDICT: PASS\n" + "Verified model contract.\n" * 30)
    write(project, "quality_contract.json", '{"schema_version":4}')
    write(project, "model.md", (project / "m1_spec.md").read_text())
    return decision


def test_default_off_read_is_inert(tmp_path):
    assert policy(tmp_path)["enabled"] is False
    assert status_view(tmp_path)["phase"] == "disabled"
    assert not (tmp_path / ".factory").exists()


def test_stale_consultation_refresh_rebuilds_package_and_rejects_old_answer(project):
    package, request = pending_request(project)
    old_answer = pro_answer(package, request)
    write(project, "problem/source.md", "New official constraint: allocations must be integral.\n")
    store = SQLiteStateStore(project)
    fresh = store.supersede_pending_decision_request(expected_revision=store.load().revision, gate=CANDIDATE_GATE)
    new_request = fresh.pending_action["metadata"]["human_decision"]
    assert new_request["generation"] == request["generation"] + 1
    assert new_request["metadata"]["joint_package_path"] != package["path"]
    assert b"allocations must be integral" in consultation_bundle(project)
    with pytest.raises(JointModelingError):
        validate_response(project, new_request, json.dumps(old_answer), {key: True for key in ATTESTATIONS})


def test_added_problem_material_invalidates_old_pro_request(project):
    package, request = pending_request(project)
    write(project, "problem/addendum.md", "Additional official condition.\n")
    with pytest.raises(JointModelingError):
        validate_response(project, request, json.dumps(pro_answer(package, request)), {key: True for key in ATTESTATIONS})
    store = SQLiteStateStore(project)
    store.supersede_pending_decision_request(expected_revision=store.load().revision, gate=CANDIDATE_GATE)
    assert b"Additional official condition." in consultation_bundle(project)


@pytest.mark.parametrize("field,value", [("recommended_candidate_ids", [{}]), ("severity", []), ("candidate_ids", [{}])])
def test_malformed_pro_fields_are_rejected_cleanly(project, field, value):
    package, request = pending_request(project)
    answer = pro_answer(package, request)
    if field == "recommended_candidate_ids":
        answer[field] = value
    else:
        answer["findings"][0][field] = value
    with pytest.raises(JointModelingError):
        validate_response(project, request, json.dumps(answer), {key: True for key in ATTESTATIONS})


def test_pro_advice_is_not_projected_into_general_judge_material(project):
    package, request = pending_request(project)
    answer = accept_pro(project, package, request)
    assert answer["summary"] not in (project / "human_review.md").read_text()
    assert answer["summary"] in accepted_response(project, CANDIDATE_GATE)[0]["answer"]


def test_stage_engine_waits_for_pro_then_synthesis_then_manual_selection(project):
    from test_stage_scheduler import Lifecycle, stage_registry
    from factory_core.stages import STAGE_CATALOG_VERSION, STAGE_SCHEDULER_GENERATION

    class SelectionLifecycle(OriginalStep):
        requires_prompt_input_receipt = False

    store = SQLiteStateStore(project)
    store.transition(expected_revision=store.load().revision, event_type="TEST_STAGE_SCHEDULER", changes={
        "scheduler_generation": STAGE_SCHEDULER_GENERATION,
        "stage_catalog_version": STAGE_CATALOG_VERSION,
        "last_completed_stage": 1,
    })
    synthesis = {"summary": "Both candidates are feasible; human selection is required.", "items": []}
    supervisor = ClaudeSupervisor(json.dumps(synthesis))
    original = SelectionLifecycle()
    wrapped = JointModelingStep(original, 3, project, JointClaudeBackend(project, supervisor=supervisor))
    registry, lifecycles = stage_registry(overrides={2: JointModelingStep(Lifecycle(), 2, project), 3: wrapped})
    engine = FactoryEngine(project, store=store, registry=registry, sleeper=lambda _: None)
    awaiting = engine.run(max_steps=6)
    assert awaiting.pending_action, (awaiting, store.events()[-2:])
    assert awaiting.pending_action["gate"] == CANDIDATE_GATE
    assert original.executions == 0 and supervisor.requests == []
    request = awaiting.pending_action["metadata"]["human_decision"]
    package = ensure_package(project, CANDIDATE_GATE, candidate_subject(project))
    accept_pro(project, package, request, pro_answer(package, request, findings=[]))
    awaiting = engine.run(max_steps=1)
    assert awaiting.pending_action["gate"] == "step3"
    assert original.executions == 0 and len(supervisor.requests) == 1
    decision = write_selection_decision(project, gate="step3", selected_option_id="m1", selected_aux_id="NONE", source="manual-cli", reason="I choose the first validated candidate after both reviews.")
    assert decision["joint_modeling_synthesis_sha256"]
    finished = engine.run(max_steps=1)
    # Native Stage selection materializes chosen_method deterministically;
    # the next task is the full model specification in Step 4.
    assert finished.last_completed_step == 4
    assert lifecycles[4].calls == [1]
    assert original.executions == 0 and len(supervisor.requests) == 1


def test_policy_requires_manual_cas_and_does_not_enable_other_projects(project, tmp_path):
    store = SQLiteStateStore(project)
    cfg = policy(project)
    assert cfg["model"] == "claude-fable-5-1"
    with pytest.raises(RevisionConflict):
        configure(project, enabled=False, expected_revision=0, actor="human")
    assert policy(tmp_path / "other")["enabled"] is False
    state = configure(project, enabled=False, expected_revision=store.load().revision, actor="human")
    assert policy(project)["enabled"] is False
    assert replay_events(store.events()) == replay_state(state)


def test_mode_cannot_change_during_or_after_candidate_execution(project):
    store = SQLiteStateStore(project)
    state = store.transition(expected_revision=store.load().revision, event_type="STEP_STARTED",
                             changes={"status": WorkflowStatus.RUNNING, "active_step": 2}, event_step=2)
    with pytest.raises(JointModelingError):
        configure(project, enabled=False, expected_revision=state.revision, actor="human")
    state = store.transition(expected_revision=state.revision, event_type="PAUSED", changes={"status": WorkflowStatus.PAUSED})
    assert "Step 2" in configuration_blocker(project)


def test_stable_package_and_current_prompt_include_all_material(project):
    subject = candidate_subject(project)
    package = ensure_package(project, CANDIDATE_GATE, subject)
    assert ensure_package(project, CANDIDATE_GATE, subject) == package
    _, request = pending_request(project)
    view = consultation_view(project)
    assert request["request_id"] in view["prompt_text"]
    assert all(key in view["attestations_required"] for key in ATTESTATIONS)
    assert b"constraint x >= 0" in consultation_bundle(project)
    assert SQLiteStateStore(project).load().runner_pid is None


@pytest.mark.parametrize("field,value", [("request_id", "old"), ("generation", 999), ("subject_fingerprint", "old"), ("package_sha256", "old"), ("nonce", "old")])
def test_pro_reply_must_match_current_request(project, field, value):
    package, request = pending_request(project)
    answer = pro_answer(package, request)
    answer[field] = value
    with pytest.raises(JointModelingError):
        validate_response(project, request, json.dumps(answer), {key: True for key in ATTESTATIONS})
    assert SQLiteStateStore(project).decision(CANDIDATE_GATE) is None


def test_pro_reply_requires_explicit_attestation_and_exact_quote(project):
    package, request = pending_request(project)
    answer = pro_answer(package, request)
    with pytest.raises(JointModelingError):
        validate_response(project, request, json.dumps(answer), {})
    answer["findings"][0]["evidence_quote"] = "invented quote"
    with pytest.raises(JointModelingError):
        validate_response(project, request, json.dumps(answer), {key: True for key in ATTESTATIONS})


def test_stale_inputs_reject_reply_before_resolving(project):
    package, request = pending_request(project)
    write(project, "m1_spec.md", "changed formulation\n" * 35)
    with pytest.raises(JointModelingError):
        accept_pro(project, package, request)
    assert SQLiteStateStore(project).load().status is WorkflowStatus.AWAITING_CONSULTATION


def test_direct_sqlite_writer_cannot_skip_pro_validation(project):
    _, request = pending_request(project)
    store = SQLiteStateStore(project)
    with pytest.raises(JointModelingError):
        store.resolve_human_decision(expected_revision=store.load().revision,
                                     resolution={"gate": CANDIDATE_GATE, "answer": "just proceed"})
    assert store.decision(CANDIDATE_GATE) is None


def test_candidate_artifacts_without_pinned_receipts_are_rejected(project):
    for path in (project / ".factory/joint_modeling/calls").glob("*/result.json"):
        path.unlink()
    with pytest.raises(JointModelingError, match="执行回执"):
        candidate_subject(project)


def test_synthesis_covers_each_finding_and_checks_grounding():
    response = {"findings": [{"id": "F1", "summary": "check bound"}]}
    item = {"finding_id": "F1", "action": "PARTIAL", "rationale": "Need a tighter proof.", "proposed_change": "Bound analysis.", "pro_quote": "bound", "human_needed": True}
    assert validate_synthesis({"summary": "Review", "items": [item]}, response)
    for items in ([], [item, item], [{**item, "pro_quote": "not in response"}]):
        with pytest.raises(JointModelingError):
            validate_synthesis({"summary": "Review", "items": items}, response)


def test_full_candidate_review_synthesis_and_human_selection_sequence(project):
    step, context, original, _ = synthesize(project)
    assert original.executions == 0
    pending = step.validate(context)
    assert pending.pending_action.gate == "step3"
    current = current_synthesis(project)
    assert current and current[2]["requested_model"] == MODEL
    options = build_step3_options(project)
    assert options["deadline_epoch"] is None
    assert options["joint_modeling"]["synthesis_sha256"] == current[2]["content_sha256"]
    assert current[3] in options["options"][0]["evidence_files"]
    assert "not instructions" in modeling_prompt_context(project)


def test_missing_synthesis_blocks_manual_or_timeout_selection(project):
    with pytest.raises(JointModelingError):
        build_step3_options(project)
    from scripts.selection_gate import default_step3
    with pytest.raises(Exception, match="不能超时"):
        default_step3(project, now_epoch=None, no_resume=True)


def test_risk_review_is_conditional_and_binds_full_spec(project):
    decision = choose(project)
    no_risk = risk_review(project)
    assert no_risk["required"] is False
    assert no_risk["selection_decision_id"] == decision["decision_id"]
    write(project, "model.md", (project / "model.md").read_text() + "\nChanged objective.\n")
    risk = risk_review(project)
    assert risk["required"] is True
    assert "FULL_MODEL_SPEC_CHANGED" in risk["reasons"]
    step = JointModelingStep(OriginalStep(), 5, project)
    context = StepContext(project, project.name, 5, 1, 60, SQLiteStateStore(project).load().revision)
    pending = step.prepare(context)
    assert pending.pending_action.gate == RISK_GATE


def test_risk_reply_also_requires_human_model_spec_approval(project):
    choose(project)
    write(project, "model.md", (project / "model.md").read_text() + "\nChanged objective.\n")
    risk = risk_review(project)
    _, request = pending_request(project, gate=RISK_GATE, subject={k: v for k, v in risk.items() if k != "path"})
    view = consultation_view(project)
    assert RISK_ATTESTATION in view["attestations_required"]


def test_joint_claude_is_pinned_isolated_and_does_not_put_large_prompt_in_argv(project):
    supervisor = ClaudeSupervisor()
    backend = JointClaudeBackend(project, supervisor=supervisor)
    request = replace(model_request(project, "synthesis-test", isolated=True), prompt="AGENT_KEY: synthesis-test\n" + "x" * 200_000)
    result = backend.execute(request)
    assert result.returncode == 0
    call = supervisor.requests[0]
    assert call.argv[call.argv.index("--model") + 1] == MODEL
    assert "--fallback-model" not in call.argv
    assert call.argv[call.argv.index("--tools") + 1] == ""
    assert call.stdin_path.read_text() == request.prompt
    assert max(len(item) for item in call.argv) < 1000
    assert call.cwd != project
    assert verify_execution(project, result.metadata["joint_execution_receipt"])


def test_reported_model_mismatch_fails_without_fallback(project):
    supervisor = ClaudeSupervisor(model="different-model")
    result = JointClaudeBackend(project, supervisor=supervisor).execute(model_request(project, "different-model-test"))
    assert result.returncode != 0
    assert "ROUTING_POLICY" in result.error_class
    assert len(supervisor.requests) == 1


def test_codex_only_conflict_does_not_launch_claude(project, monkeypatch):
    monkeypatch.setenv("CODEX_ONLY", "1")
    supervisor = ClaudeSupervisor()
    result = JointClaudeBackend(project, supervisor=supervisor).execute(model_request(project, "codex-only-test"))
    assert result.returncode != 0 and not supervisor.requests


def test_enabled_dispatcher_never_uses_registry_fallback(project, monkeypatch):
    class NeverBackend:
        def execute(self, request):
            pytest.fail("normal fallback must not run")
    registry = ModelBackendRegistry()
    registry.register("codex", NeverBackend())
    monkeypatch.setattr(JointClaudeBackend, "execute", lambda self, req: ExecutionResult.failed("PERMANENT_MODEL_UNSUPPORTED"))
    result = ModelDispatcher(project, registry).execute(model_request(project), step_key=2, defaults=("codex", "claude"))
    assert result.error_class == "PERMANENT_MODEL_UNSUPPORTED"


def test_uncertain_dispatch_is_not_automatically_repeated(project):
    class InterruptedSupervisor:
        def run(self, request):
            raise RuntimeError("worker lost after reservation")
    with pytest.raises(RuntimeError):
        JointClaudeBackend(project, supervisor=InterruptedSupervisor()).execute(model_request(project, "interrupted"))
    supervisor = ClaudeSupervisor()
    result = JointClaudeBackend(project, supervisor=supervisor).execute(model_request(project, "interrupted"))
    assert result.error_class == "PERMANENT_MODELING_DISPATCH_UNCERTAIN"
    assert not supervisor.requests


def test_duplicate_json_keys_are_rejected():
    with pytest.raises(JointModelingError):
        parse_json('{"request_id":"old","request_id":"new"}')


def test_final_judge_preamble_does_not_contain_joint_advisory(project):
    package, request = pending_request(project)
    accept_pro(project, package, request)
    from factory_core.consultation_projection import authoritative_consultation_prompt
    assert "Retain the nonnegative constraint" not in authoritative_consultation_prompt(project)
