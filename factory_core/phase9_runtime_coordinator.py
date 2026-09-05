"""Default-off orchestration between prepared Step13 inputs and Authority.

Generation creation and independent entry/start authorization remain explicit
operator paths. This module never manufactures either authorization.
"""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import time
from pathlib import Path

from .canonical import canonical_bytes, canonical_sha256
from .deadline import deadline_scope
from .domain import StepContext
from .governance.overrides import NullOverrideProvider
from .phase9_runtime import (
    Phase9RuntimeError, _ObservedDispatcher, _source_identity, _write_new,
    run_step13_components,
)
from .phase9_runtime_authority import dispatch_target
from .phase9_provider_identity import provider_identity
from .steps.registry import build_native_registry


def prepared_packet(project):
    """Bind every registry claim to the actual complete role input fingerprints."""
    from scripts.judge_packet import packet_payloads, packet_fingerprints
    fingerprints = packet_fingerprints(project)
    payloads = packet_payloads(project, objective_evidence=project / "judge_packets/objective_evidence.json")
    for role, payload in payloads.items():
        completeness = payload["manifest"]["completeness"]
        if completeness.get("status") != "COMPLETE" or completeness.get("eligible") is not True:
            raise Phase9RuntimeError(f"incomplete {role} packet; zero dispatch required")
    raw = (project / "claim_registry.json").read_bytes()
    registry = json.loads(raw)
    claims = registry.get("claims", [])
    if not claims or len({claim["id"] for claim in claims}) != len(claims):
        raise Phase9RuntimeError("required claim inventory is empty or duplicated")
    entries = []
    for claim in claims:
        roles = claim["required_roles"]
        if not roles or any(role not in fingerprints for role in roles):
            raise Phase9RuntimeError("claim role inventory differs")
        entries.append({"claim_id": claim["id"], "content_sha256": canonical_sha256({
            "claim_registry_sha256": hashlib.sha256(raw).hexdigest(), "claim_id": claim["id"],
            "role_packet_fingerprints": {role: fingerprints[role] for role in sorted(roles)},
        })})
    packet = {"schema": "authority-phase9-packet-v2", "rebuild_start": "STEP13_PACKET_REBUILD",
              "required_claims": sorted(claim["id"] for claim in claims),
              "claims": sorted(entries, key=lambda c: c["claim_id"])}
    return packet, fingerprints


def plan_runtime(*, source, project, records, request, model="gpt-6-astra", effort="medium",
                 timeout_seconds=900, total_timeout_seconds=3600):
    prepared = run_step13_components(source=source, project=project, records=records,
                                    mode="FORENSIC_THREE_ROLE", timeout_seconds=timeout_seconds,
                                    total_timeout_seconds=total_timeout_seconds, model=model, effort=effort, prepare_only=True)
    if prepared["status"] != "PREPARED":
        return prepared
    packet, fingerprints = prepared_packet(project)
    target = dispatch_target(request, packet_sha256=canonical_sha256(packet),
                             project_input_sha256=canonical_sha256(fingerprints), model=model, effort=effort,
                             timeout_seconds=timeout_seconds, total_timeout_seconds=total_timeout_seconds, provider=provider_identity(project))
    _write_new(records / "formal_packet.json", packet)
    _write_new(records / "dispatch_target.json", target)
    return {"status": "PREPARED", "target": target, "target_sha256": canonical_sha256(target),
            "model_dispatch_count": 0, "formal_phase9_completed": False, "delivery_capability": "DISABLED"}


def execute_runtime(*, source, project, records, request, entry, target, grant, authority):
    """Acquire entry plus distinct grant, then execute the repository retry path."""
    if project != authority.project_root or source != authority.finalizer.source_repository:
        raise Phase9RuntimeError("execution directories differ from the Authority coordinates")
    if source == project or source in project.parents or project in source.parents:
        raise Phase9RuntimeError("Authority project and execution source must not overlap")
    for path in (source, project, records.parent):
        if not path.is_absolute() or path.resolve() != path or path.is_symlink() or not path.is_dir():
            raise Phase9RuntimeError("runtime directories must be canonical and ordinary")
    if any(records == p or p in records.parents or records in p.parents for p in (source, project)):
        raise Phase9RuntimeError("runtime records must be outside source and project")
    records.mkdir()
    (records / "calls").mkdir()
    source_identity = _source_identity(source)
    _write_new(records / "source_identity.json", source_identity)
    packet, fingerprints = prepared_packet(project)
    if target != dispatch_target(request, packet_sha256=canonical_sha256(packet),
                                 project_input_sha256=canonical_sha256(fingerprints), model=target["model"], effort=target["effort"],
                                 timeout_seconds=target["timeout_seconds"], total_timeout_seconds=target["total_timeout_seconds"], provider=provider_identity(project)):
        raise Phase9RuntimeError("prepared packet no longer matches the dispatch grant")
    _write_new(records / "formal_packet.json", packet)
    start = authority.start(request, entry, target, grant)
    _write_new(records / "started.json", start)

    def check_inputs():
        current_packet, current_fingerprints = prepared_packet(project)
        if (int(time.time()) >= start["deadline_at"] or current_packet != packet
                or current_fingerprints != fingerprints or _source_identity(source) != source_identity):
            raise Phase9RuntimeError("source/input content changed or runtime deadline exhausted")

    dispatcher = _ObservedDispatcher(source, records / "calls", check_inputs, target["model"], target["effort"],
                                     authority=authority, runtime_id=start["runtime_id"])
    result = None
    try:
        with deadline_scope(start["deadline_at"]):
            step = build_native_registry(source).get(13).lifecycle
            step.override_provider = NullOverrideProvider()
            step.dispatcher = dispatcher
            context = StepContext(project, project.name, 13, 1, target["timeout_seconds"], 0, deadline_epoch=start["deadline_at"])
            result = step.prepare_packets(context)
            check_inputs()
            if result.returncode == 0:
                result = step.execute_prepared(context)
            check_inputs()
            _write_new(records / "component_result.json", asdict(result))
            # Local lifecycle probes are separately authorized by the same
            # target; they never invoke a model or a scientific solver.
            from .phase9_runtime_probes import run_process_scope_probes
            run_process_scope_probes(authority, start["runtime_id"], records / "process_scopes")
    except BaseException as exc:
        _write_new(records / "exception.json", {"type": type(exc).__name__, "message": str(exc)})
        raise
    finally:
        terminal = authority.finish(start["runtime_id"])
        _write_new(records / "terminal.json", terminal)
    return {"runtime": terminal, "component_result": asdict(result), "model_dispatch_count": len(dispatcher.calls),
            "formal_phase9_completed": False, "delivery_capability": "DISABLED"}


def write_finalizer_controls(*, authority, runtime_id, request, entry, project, evidence_root, outputs, validate_only=False):
    """Recompute packet, verdict layers and snapshot reads from the completed run."""
    from scripts.aggregate_judges import aggregate_outputs, _read_role
    from .phase9_forensic_replay import _effective
    from .phase9_runtime_export import write_equal as write_bytes
    from .phase9_authority_lease import authority_state_commit_lease, isolated_authority_snapshot_ro

    state = authority.collect(runtime_id)
    target = state["start"]["target"]
    packet, fingerprints = prepared_packet(project)
    if canonical_sha256(packet) != target["packet_sha256"] or canonical_sha256(fingerprints) != target["project_input_sha256"]:
        raise Phase9RuntimeError("completed runtime inputs changed before finalizer preparation")
    for role in target["roles"]:
        if (project / "judge_outputs" / f"{role}.md").read_bytes() != outputs[role]:
            raise Phase9RuntimeError("aggregate role file differs from actual observed output")
    aggregate = aggregate_outputs(**{
        **{role + "_path": project / "judge_outputs" / f"{role}.md" for role in target["roles"]},
        **{role + "_manifest": project / "judge_packets" / role / "manifest.json" for role in target["roles"]},
    })
    layers = {}
    for role in target["roles"]:
        parsed = _read_role(project / "judge_outputs" / f"{role}.md", role)
        raw = parsed.verdict if parsed.verdict in {"PASS", "FAIL", "INDETERMINATE"} else "INDETERMINATE"
        if parsed.status == "REVISE":
            raw = "FAIL"
        values = {"raw": raw, "protocol": "PASS" if parsed.error is None else "INDETERMINATE",
                  "grounding": "PASS" if aggregate.evidence_grounding[role].get("valid") is True else "INDETERMINATE"}
        layers[role] = {**values, "effective": _effective(list(values.values()))}
    coordinate = {"project_id": request.project_id, "project_revision": request.project_revision, "run_generation": request.run_generation}
    # Both snapshot sections are read at the exact live Authority coordinate.
    with authority_state_commit_lease(authority.project_root):
        with isolated_authority_snapshot_ro(authority.database) as connection:
            connection.execute("BEGIN")
            authority.finalizer._verify_coordinate(connection, request)
            authority.finalizer._verify_external_generation_inputs(connection, request)
            live = authority.finalizer._live_entry_state(connection, request)
            if live.state_receipt_sha256 != state["start"]["entry_state_receipt_sha256"]:
                raise Phase9RuntimeError("live snapshot changed since runtime acquisition")
            launches = connection.execute(
                "SELECT COUNT(*) FROM authority_production_phase9_runtime_launches l JOIN "
                "authority_production_phase9_runtime_attempts a ON a.attempt_id=l.attempt_id "
                "WHERE a.runtime_id=? AND a.role IN ('execution','math','paper')", (runtime_id,)
            ).fetchone()[0]
    controls = {
        "entry_gate.json": entry,
        "packet.json": {"schema": "authority-phase9-packet-evidence-v1", "required_claims": packet["required_claims"],
                        "present_claims": [claim["claim_id"] for claim in packet["claims"]],
                        "packet_path": "payload/packet.bin", "packet_sha256": canonical_sha256(packet), "dispatch_count": launches},
        "verdict.json": {"schema": "authority-phase9-verdict-evidence-v1", "roles": layers,
                         "effective_verdict": _effective([value["effective"] for value in layers.values()]),
                         # This is the forensic evaluation protocol exit field;
                         # raw role and component exits remain in runtime records.
                         "exit_code": 0},
        "snapshot.json": {"schema": "authority-phase9-snapshot-evidence-v1", "coordinate": coordinate,
                          "sections": [{"section": section, "coordinate": coordinate, "read_failed": False,
                                        "read_status": "AVAILABLE"} for section in ("artifacts", "workflow")]},
    }
    if validate_only:
        return controls["verdict.json"]
    write_bytes(evidence_root / "payload/packet.bin", canonical_bytes(packet))
    for name, body in controls.items():
        write_bytes(evidence_root / name, canonical_bytes(body))
    return controls["verdict.json"]
