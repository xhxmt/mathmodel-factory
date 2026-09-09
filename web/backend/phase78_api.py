"""ACL-first Web adapter for the non-authoritative Phase 7+8 shadow flow.

This module imports no Phase 7+8 core at module load.  Even when the process
gate is enabled, authentication and project ACL checks run before environment
path parsing, database/CAS access, or the lazy service import.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
import importlib
import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from starlette.concurrency import run_in_threadpool

from .access_control import require_project_access
from .auth import get_current_user
from .config import Settings
from .schemas import UserInfo


_LOG = logging.getLogger(__name__)
_MAX_REQUEST_BYTES = 8 * 1024 * 1024

Adapter = Callable[..., Mapping[str, Any] | Any]


def _lazy_adapter(name: str) -> Adapter:
    module = importlib.import_module("factory_core.phase78_service")
    value = getattr(module, name, None)
    if not callable(value):
        raise RuntimeError("Phase 7+8 service adapter is unavailable")
    return value


def _duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


async def _request_payload(request: Request) -> dict[str, object]:
    raw = await request.body()
    if not raw or len(raw) > _MAX_REQUEST_BYTES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": "PHASE78_REQUEST_INVALID"},
        )
    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"), object_pairs_hook=_duplicates
        )
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": "PHASE78_REQUEST_INVALID"},
        ) from exc
    if type(value) is not dict:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": "PHASE78_REQUEST_INVALID"},
        )
    return value


def _enabled_settings(web_settings: Settings):
    if not web_settings.phase78_shadow_enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="PHASE78_SHADOW_DISABLED",
        )
    try:
        config = importlib.import_module("factory_core.phase78_config")
        return config.load_phase78_settings()
    except Exception as exc:
        _LOG.error(
            "Phase 7+8 configuration rejected after ACL (error_type=%s)",
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="PHASE78_CONFIGURATION_INVALID",
        ) from exc


def _safe_payload(value: Any, base_name: str) -> dict[str, object]:
    if isinstance(value, Mapping):
        payload = dict(value)
    else:
        as_dict = getattr(value, "as_dict", None)
        payload = as_dict() if callable(as_dict) else None
    if type(payload) is not dict:
        raise ValueError("Phase 7+8 adapter returned a non-object")
    if payload.get("project_id") not in {None, base_name}:
        raise ValueError("Phase 7+8 adapter returned another project")
    for field in (
        "authoritative",
        "authority_transferred",
        "dispatch_performed",
        "provider_call_performed",
        "outbox_dispatch_performed",
    ):
        if field in payload and payload[field] is not False:
            raise ValueError("Phase 7+8 adapter returned an unsafe flag")
    # Clone once through strict JSON to ensure the response cannot execute an
    # object hook or defer a NaN/Infinity failure until after headers.
    return json.loads(json.dumps(payload, ensure_ascii=False, allow_nan=False))


def _public_error(exc: Exception) -> HTTPException:
    code = getattr(exc, "code", "PHASE78_SHADOW_UNAVAILABLE")
    if code in {
        "PHASE78_IDEMPOTENCY_CONFLICT",
        "PHASE7_GROUNDING_IDEMPOTENCY_CONFLICT",
        "PHASE7_GROUNDING_SOURCE_STALE",
        "PHASE8_IDEMPOTENCY_CONFLICT",
        "PHASE8_SOURCE_STALE",
        "PHASE78_CURRENT_HEAD_MISMATCH",
        "PHASE78_REQUEST_CANCELLED",
        "PHASE78_OUTCOME_UNCERTAIN",
        "PHASE78_WORK_TERMINAL",
    }:
        if code == "PHASE78_REQUEST_CANCELLED":
            reason = getattr(exc, "reason", None)
            if reason in {"user_cancel", "shutdown", "superseded"}:
                return HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={"code": code, "reason": reason},
                )
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=code)
    if code in {
        "PHASE78_WORK_NOT_FOUND",
        "PHASE7_GROUNDING_NOT_FOUND",
        "PHASE8_NOT_FOUND",
    }:
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=code)
    if code == "PHASE78_DEADLINE_EXCEEDED":
        return HTTPException(status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail=code)
    if code in {
        "PHASE78_REQUEST_INVALID",
        "PHASE7_GROUNDING_CONTRACT_INVALID",
        "PHASE8_CONTRACT_INVALID",
    }:
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=code
        )
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="PHASE78_SHADOW_UNAVAILABLE",
    )


def create_phase78_router(
    settings: Settings,
    *,
    submitter: Adapter | None = None,
    worker_runner: Adapter | None = None,
    status_loader: Adapter | None = None,
    request_canceller: Adapter | None = None,
    approval_revoker: Adapter | None = None,
) -> APIRouter:
    router = APIRouter()
    current_user_dependency = get_current_user(settings)

    @router.post("/api/projects/{base_name}/phase78-shadow/requests")
    async def submit(
        base_name: str,
        request: Request,
        current_user: UserInfo = Depends(current_user_dependency),
    ):
        require_project_access(settings, current_user, base_name)
        phase_settings = _enabled_settings(settings)
        payload = await _request_payload(request)
        try:
            value = await run_in_threadpool(
                submitter or _lazy_adapter("submit_phase78_request"),
                settings=phase_settings,
                project_id=base_name,
                actor_id=current_user.username,
                payload=payload,
            )
            return _safe_payload(value, base_name)
        except HTTPException:
            raise
        except Exception as exc:
            _LOG.error(
                "Phase 7+8 submit failed after ACL (error_type=%s)",
                type(exc).__name__,
            )
            raise _public_error(exc) from exc

    @router.post("/api/projects/{base_name}/phase78-shadow/run-one")
    async def run_one(
        base_name: str,
        request: Request,
        current_user: UserInfo = Depends(current_user_dependency),
    ):
        require_project_access(settings, current_user, base_name)
        phase_settings = _enabled_settings(settings)
        payload = await _request_payload(request)
        try:
            value = await run_in_threadpool(
                worker_runner or _lazy_adapter("run_phase78_worker_once"),
                settings=phase_settings,
                project_id=base_name,
                actor_id=current_user.username,
                payload=payload,
            )
            return _safe_payload(value, base_name)
        except HTTPException:
            raise
        except Exception as exc:
            _LOG.error(
                "Phase 7+8 local worker failed after ACL (error_type=%s)",
                type(exc).__name__,
            )
            raise _public_error(exc) from exc

    @router.get("/api/projects/{base_name}/phase78-shadow/{idempotency_key}")
    def read_status(
        base_name: str,
        idempotency_key: str,
        current_user: UserInfo = Depends(current_user_dependency),
    ):
        require_project_access(settings, current_user, base_name)
        phase_settings = _enabled_settings(settings)
        try:
            value = (status_loader or _lazy_adapter("load_phase78_status"))(
                settings=phase_settings,
                project_id=base_name,
                actor_id=current_user.username,
                idempotency_key=idempotency_key,
            )
            return _safe_payload(value, base_name)
        except HTTPException:
            raise
        except Exception as exc:
            _LOG.error(
                "Phase 7+8 status read failed after ACL (error_type=%s)",
                type(exc).__name__,
            )
            raise _public_error(exc) from exc

    @router.post(
        "/api/projects/{base_name}/phase78-shadow/{idempotency_key}/cancel"
    )
    async def cancel_request(
        base_name: str,
        idempotency_key: str,
        request: Request,
        current_user: UserInfo = Depends(current_user_dependency),
    ):
        require_project_access(settings, current_user, base_name)
        phase_settings = _enabled_settings(settings)
        payload = await _request_payload(request)
        if payload.get("idempotency_key") != idempotency_key:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={"code": "PHASE78_REQUEST_INVALID"},
            )
        try:
            value = await run_in_threadpool(
                request_canceller or _lazy_adapter("cancel_phase78_request"),
                settings=phase_settings,
                project_id=base_name,
                actor_id=current_user.username,
                payload=payload,
            )
            return _safe_payload(value, base_name)
        except HTTPException:
            raise
        except Exception as exc:
            _LOG.error(
                "Phase 7+8 request cancellation failed after ACL (error_type=%s)",
                type(exc).__name__,
            )
            raise _public_error(exc) from exc

    @router.post(
        "/api/projects/{base_name}/phase78-shadow/approvals/{approval_id}/revoke"
    )
    async def revoke(
        base_name: str,
        approval_id: str,
        request: Request,
        current_user: UserInfo = Depends(current_user_dependency),
    ):
        require_project_access(settings, current_user, base_name)
        phase_settings = _enabled_settings(settings)
        payload = await _request_payload(request)
        try:
            value = await run_in_threadpool(
                approval_revoker or _lazy_adapter("revoke_phase78_approval"),
                settings=phase_settings,
                project_id=base_name,
                actor_id=current_user.username,
                approval_id=approval_id,
                payload=payload,
            )
            return _safe_payload(value, base_name)
        except HTTPException:
            raise
        except Exception as exc:
            _LOG.error(
                "Phase 7+8 approval revoke failed after ACL (error_type=%s)",
                type(exc).__name__,
            )
            raise _public_error(exc) from exc

    return router


__all__ = ["create_phase78_router"]
