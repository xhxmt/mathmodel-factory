"""Explicit operator-only preparation for one Phase 7+8 shadow request.

This module is deliberately absent from the general CLI, Web router and
service exports.  A trusted local operator invokes it explicitly to validate
the current Phase-3/6 source, persist the exact Phase-7/PDF/Phase-8 reference
facts, and mint the durable local approval preflight that an ordinary pipeline
request may only reference by hash.  The later worker replays the same P7 and
reference keys; this command never enqueues work or performs dispatch.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import time
from typing import Mapping

from .phase78_config import (
    Phase78ConfigurationError,
    Phase78Settings,
    load_phase78_settings,
)


PHASE78_OPERATOR_PREFLIGHT_RESULT_SCHEMA = (
    "phase78-operator-trusted-preflight-result-v1"
)
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@-]{0,255}\Z")
_MAX_REQUEST_BYTES = 8 * 1024 * 1024


class Phase78OperatorError(RuntimeError):
    code = "PHASE78_OPERATOR_UNTRUSTED"


class Phase78OperatorDisabled(Phase78OperatorError):
    code = "PHASE78_SHADOW_DISABLED"


class Phase78OperatorRequestError(Phase78OperatorError):
    code = "PHASE78_REQUEST_INVALID"


def _identifier(value: object, field: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise Phase78OperatorRequestError(f"{field} must be a canonical identifier")
    return value


def _duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise Phase78OperatorRequestError(
                "operator request contains duplicate JSON keys"
            )
        result[key] = value
    return result


def _payload(path_value: str) -> dict[str, object]:
    path = Path(path_value)
    information = path.lstat()
    if (
        not stat.S_ISREG(information.st_mode)
        or information.st_nlink != 1
        or not 0 < information.st_size <= _MAX_REQUEST_BYTES
    ):
        raise Phase78OperatorRequestError(
            "operator request must be one bounded regular file"
        )
    raw = path.read_bytes()
    if len(raw) != information.st_size:
        raise Phase78OperatorRequestError("operator request changed while reading")
    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"), object_pairs_hook=_duplicates
        )
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise Phase78OperatorRequestError(
            "operator request is not strict JSON"
        ) from exc
    if type(value) is not dict:
        raise Phase78OperatorRequestError("operator request must be a JSON object")
    return value


def prepare_phase78_trusted_preflight(
    settings: Phase78Settings,
    project_id: str,
    operator_id: str,
    payload: Mapping[str, object],
    *,
    deadline: object | None = None,
) -> dict[str, object]:
    """Persist a trusted local preflight without exposing approval minting."""

    if not isinstance(settings, Phase78Settings) or settings.enabled is not True:
        raise Phase78OperatorDisabled("Phase 7+8 shadow pipeline is disabled")
    project = _identifier(project_id, "project_id")
    operator = _identifier(operator_id, "operator_id")
    trusted_operator = os.environ.get("PHASE78_TRUSTED_OPERATOR_ID", "")
    trusted_generation = os.environ.get(
        "PHASE78_TRUSTED_OPERATOR_GENERATION", ""
    )
    if (
        _IDENTIFIER.fullmatch(trusted_operator) is None
        or _IDENTIFIER.fullmatch(trusted_generation) is None
    ):
        raise Phase78ConfigurationError(
            "PHASE78_TRUSTED_OPERATOR_ID and "
            "PHASE78_TRUSTED_OPERATOR_GENERATION are required for operator preflight"
        )
    if operator != trusted_operator:
        raise Phase78OperatorError(
            "operator identity differs from protected local configuration"
        )

    # All heavy/runtime modules remain behind both enablement and protected
    # operator identity.  Importing this module while disabled is resource-free.
    service_module = importlib.import_module("factory_core.phase78_service")
    deadline_module = importlib.import_module("factory_core.phase78_deadline")
    current_module = importlib.import_module("factory_core.phase78_current")
    phase7_module = importlib.import_module("factory_core.phase7_grounding_runtime")
    phase8_module = importlib.import_module(
        "factory_core.phase8_evidence_egress_runtime"
    )
    materializer_module = importlib.import_module(
        "factory_core.reference_materializer"
    )
    request = service_module.parse_phase78_request(payload)
    if (
        request.approval["issuer_id"] != operator
        or request.approval["issuer_generation"] != trusted_generation
    ):
        raise Phase78OperatorError(
            "approval issuer identity differs from protected local configuration"
        )
    if request.approval["issuer_id"] == request.approval["subject_id"]:
        raise Phase78OperatorError(
            "trusted local approval cannot be issued by its own subject"
        )
    total = (
        deadline
        if deadline is not None
        else deadline_module.TotalDeadline(settings.deadline_ms)
    )
    verifier = current_module.Phase78CurrentHeadVerifier(settings)
    facts = verifier.verify(
        phase3_artifact_state=request.phase3_artifact_state,
        phase3_artifact_occurrence=request.phase3_artifact_occurrence,
        phase6_access_proof=request.phase6_access_proof,
        deadline=total,
    )
    if facts.coordinate.project_id != project:
        raise service_module.Phase78ProjectMismatch(
            "operator request belongs to another project"
        )
    if (
        request.approval["subject_id"] != facts.access_proof.grant.subject_id
        or request.approval["subject_generation"]
        != facts.access_proof.grant.subject_generation
    ):
        raise service_module.Phase78ProjectMismatch(
            "trusted operator/subject differs from the exact approval source"
        )
    trusted_now = int(time.time())
    if not (
        request.approval["logical_issued_at"]
        <= request.approval["not_before"]
        <= trusted_now
        < request.approval["expires_at"]
    ):
        raise Phase78OperatorError(
            "operator approval interval is not currently active"
        )
    upstream_current = verifier.current_callback(deadline=total)
    upstream_fence = verifier.fence(
        phase3_artifact_state=request.phase3_artifact_state,
        phase3_artifact_occurrence=request.phase3_artifact_occurrence,
        phase6_access_proof=request.phase6_access_proof,
        deadline=total,
    )
    p7_store = phase7_module.Phase7GroundingStore(
        settings.required_path("phase7_database")
    )
    p7_result = p7_store.record_grounding_bundle(
        idempotency_key=f"{request.idempotency_key}:phase7",
        phase3_artifact_state=request.phase3_artifact_state,
        phase3_artifact_occurrence=request.phase3_artifact_occurrence,
        phase6_access_proof=request.phase6_access_proof,
        role_output_bytes=request.role_outputs,
        manifest_bytes=request.manifests,
        context_bytes=request.contexts,
        current_head_verifier=upstream_current,
        deadline=total,
        fence_hook=upstream_fence,
    )
    p7_bundle = p7_store.load_bundle(
        f"{request.idempotency_key}:phase7",
        deadline=total,
        fence_hook=upstream_fence,
    )
    package = materializer_module.materialize_reference_pdf(
        reference_id=request.reference["reference_id"],
        project_root=settings.required_path("project_root"),
        pdf_path=Path(request.reference["pdf_path"]),
        phase3_artifact_occurrence=request.phase3_artifact_occurrence,
        bibliographic_metadata=request.reference["bibliographic_metadata"],
        external_share_classification=request.reference[
            "external_share_classification"
        ],
        cas_root=settings.required_path("cas_root"),
        scratch_root=settings.required_path("scratch_root"),
        config=materializer_module.ReferenceMaterializerConfig(),
        deadline=total,
    )

    def p7_current(scope_key: str, commit_sha256: str) -> bool:
        loaded = p7_store.load_current_bundle(
            scope_key,
            current_head_verifier=upstream_current,
            deadline=total,
        )
        return loaded["result"]["commit_sha256"] == commit_sha256

    p8_store = phase8_module.Phase8EvidenceEgressStore(
        settings.required_path("phase8_database"),
        settings.required_path("cas_root"),
        enabled=True,
    )
    binding_key = f"{request.idempotency_key}:phase8-binding"
    operator_generation = {
        "request_idempotency_key": request.idempotency_key,
        "operator_id": operator,
        "operator_generation": trusted_generation,
    }

    def publication_is_current(publication: Mapping[str, object]) -> bool:
        if (
            publication.get("publication_kind") != "operator-generation"
            or publication.get("generation") != operator_generation
        ):
            return False
        total.check("operator publication generation")
        upstream_fence("operator publication upstream")
        return p7_current(
            str(publication.get("phase7_scope_key")),
            str(publication.get("phase7_commit_sha256")),
        )

    binding_publication = phase8_module.build_phase8_publication_identity(
        publication_kind="operator-generation",
        publication_key=binding_key,
        generation=operator_generation,
        phase7_scope_key=p7_result.scope_key,
        phase7_commit_sha256=p7_result.commit_sha256,
    )
    try:
        binding = p8_store.record_reference_binding(
            idempotency_key=binding_key,
            logical_id=request.reference["logical_id"],
            phase3_artifact_state=request.phase3_artifact_state,
            phase3_artifact_occurrence=request.phase3_artifact_occurrence,
            phase6_access_proof=request.phase6_access_proof,
            phase7_result=p7_bundle["result"],
            phase7_receipt=p7_bundle["receipt"],
            phase7_effective_verdict=p7_bundle["effective_verdict"],
            reference_package_blob=package.package_blob,
            reference_receipt_blob=package.receipt_blob,
            phase7_current_head_verifier=p7_current,
            publication_identity=binding_publication,
            publication_head_verifier=publication_is_current,
            expected_current_binding_sha256=request.reference[
                "expected_current_binding_sha256"
            ],
            deadline=total,
        )
    except deadline_module.Phase78OutcomeUncertain as exc:
        raise deadline_module.Phase78OutcomeUncertain(
            request.idempotency_key
        ) from exc
    approval = request.approval
    try:
        preflight = p8_store.register_trusted_approval_preflight(
            idempotency_key=f"{request.idempotency_key}:trusted-preflight",
            preflight_id=f"{approval['approval_id']}-trusted-preflight",
            approval_id=approval["approval_id"],
            binding_sha256=binding.binding_sha256,
            issuer_id=approval["issuer_id"],
            issuer_generation=approval["issuer_generation"],
            subject_id=approval["subject_id"],
            subject_generation=approval["subject_generation"],
            logical_issued_at=approval["logical_issued_at"],
            not_before=approval["not_before"],
            expires_at=approval["expires_at"],
            decision_evaluated_at=trusted_now,
            data_egress_request=approval["data_egress_request"],
            phase7_current_head_verifier=p7_current,
            publication_head_verifier=publication_is_current,
            successor_of=approval["successor_of"],
            expected_predecessor_event_sha256=approval[
                "expected_predecessor_event_sha256"
            ],
            deadline=total,
        )
        total.check("operator preflight terminal")
        upstream_fence("operator preflight terminal upstream")
        p8_store.load_current_reference_binding(
            binding.scope_key,
            phase7_current_head_verifier=p7_current,
            publication_head_verifier=publication_is_current,
            expected_binding_sha256=binding.binding_sha256,
            deadline=total,
        )
        total.check("operator preflight response")
    except deadline_module.Phase78OutcomeUncertain as exc:
        raise deadline_module.Phase78OutcomeUncertain(
            request.idempotency_key
        ) from exc
    except deadline_module.Phase78DeadlineError as exc:
        # Reference/preflight history may already be durable.  The operator
        # must query or replay this exact root request key.
        raise deadline_module.Phase78OutcomeUncertain(
            request.idempotency_key
        ) from exc
    return {
        "schema_version": PHASE78_OPERATOR_PREFLIGHT_RESULT_SCHEMA,
        "project_id": project,
        "idempotency_key": request.idempotency_key,
        "trusted_preflight_sha256": preflight.preflight["preflight_sha256"],
        "binding_sha256": binding.binding_sha256,
        "phase7_commit_sha256": p7_result.commit_sha256,
        "phase7_replayed": p7_result.replayed,
        "binding_replayed": binding.replayed,
        "preflight_replayed": preflight.replayed,
        "authoritative": False,
        "authority_transferred": False,
        "dispatch_performed": False,
        "provider_call_performed": False,
        "outbox_dispatch_performed": False,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m factory_core.phase78_operator"
    )
    parser.add_argument("--operator", required=True)
    sub = parser.add_subparsers(dest="operator_command", required=True)
    prepare = sub.add_parser("prepare")
    prepare.add_argument("project_id")
    prepare.add_argument("request_json")
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(sys.argv[1:] if argv is None else argv)
        settings = load_phase78_settings()
        if settings.enabled is not True:
            raise Phase78OperatorDisabled("Phase 7+8 shadow pipeline is disabled")
        result = prepare_phase78_trusted_preflight(
            settings,
            args.project_id,
            args.operator,
            _payload(args.request_json),
        )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except SystemExit:
        raise
    except Exception as exc:
        print(
            json.dumps(
                {"code": getattr(exc, "code", "PHASE78_SHADOW_UNAVAILABLE")},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "PHASE78_OPERATOR_PREFLIGHT_RESULT_SCHEMA",
    "Phase78OperatorDisabled",
    "Phase78OperatorError",
    "Phase78OperatorRequestError",
    "prepare_phase78_trusted_preflight",
]
