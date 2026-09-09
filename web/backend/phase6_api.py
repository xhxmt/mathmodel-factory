"""Authenticated, ACL-first read adapter for the Phase 6 shadow snapshot UI.

This module deliberately has no import-time dependency on the Phase 6 store.
The store adapter is imported only after authentication, project ACL checking,
and the backend feature gate have all succeeded.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
import importlib
import json
import logging
from pathlib import Path
import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status

from .access_control import require_project_access
from .auth import get_current_user
from .config import Settings
from .schemas import UserInfo


_LOG = logging.getLogger(__name__)
_WEB_SCHEMA = "phase6-project-snapshot-web-v1"
_SNAPSHOT_ID = re.compile(r"^[0-9a-f]{64}$")
_EXPECTED_REVISION = re.compile(r"^(?:0|[1-9][0-9]*)$")
_MAX_REVISION = 2**63 - 1
_VIEW_STATES = frozenset(
    {
        "loading",
        "ready",
        "empty",
        "legacy_unavailable",
        "auth_error",
        "api_error",
        "unknown",
    }
)
_PUBLIC_ERROR_CODES = frozenset(
    {
        "PHASE6_SNAPSHOT_LOADING",
        "PHASE6_SNAPSHOT_EMPTY",
        "LEGACY_SNAPSHOT_UNAVAILABLE",
        "PHASE6_SNAPSHOT_AUTH_ERROR",
        "PHASE6_SNAPSHOT_UNAVAILABLE",
        "PHASE6_CONFIGURATION_INVALID",
        "PHASE6_SNAPSHOT_STALE",
        "PHASE6_SNAPSHOT_NOT_FOUND",
        "PHASE6_SOURCE_INELIGIBLE",
        "PHASE6_SNAPSHOT_EXPIRED",
        "PHASE6_GRANT_EXPIRED",
        "PHASE6_GRANT_REVOKED",
        "PHASE6_SNAPSHOT_TAMPERED",
        "PHASE6_SNAPSHOT_INCONSISTENT",
    }
)
_STATE_REASON_CODES = {
    "loading": "PHASE6_SNAPSHOT_LOADING",
    "empty": "PHASE6_SNAPSHOT_EMPTY",
    "legacy_unavailable": "LEGACY_SNAPSHOT_UNAVAILABLE",
    "auth_error": "PHASE6_SNAPSHOT_AUTH_ERROR",
    "api_error": "PHASE6_SNAPSHOT_UNAVAILABLE",
    "unknown": "PHASE6_SNAPSHOT_INCONSISTENT",
}


SnapshotLoader = Callable[..., Mapping[str, Any] | Any]


def _lazy_snapshot_loader() -> SnapshotLoader:
    module = importlib.import_module("factory_core.phase6_snapshot_grants")
    loader = getattr(module, "load_project_snapshot_for_web", None)
    if not callable(loader):
        raise RuntimeError("Phase 6 Web snapshot adapter is unavailable")
    return loader


def _mapping_payload(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    as_dict = getattr(value, "as_dict", None)
    if callable(as_dict):
        result = as_dict()
        if isinstance(result, Mapping):
            return dict(result)
    raise TypeError("Phase 6 snapshot adapter returned an invalid payload")


def _safe_reason_code(value: Any, fallback: str) -> str:
    if isinstance(value, str) and value in _PUBLIC_ERROR_CODES:
        return value
    return fallback


def _parse_expected_revision(value: object) -> int | None:
    """Parse the optional revision only after project access is authorized.

    Keeping the route parameter as an unconstrained string prevents FastAPI's
    request validation from disclosing a distinct 422 response before the
    project ACL has run.  Direct endpoint tests may still supply an integer,
    so both wire strings and already-parsed integers use the same strict
    signed-64-bit contract.
    """

    if value is None:
        return None
    if type(value) is int:
        revision = value
    elif type(value) is str and _EXPECTED_REVISION.fullmatch(value) is not None:
        try:
            revision = int(value, 10)
        except ValueError:  # pragma: no cover - guarded by the ASCII expression
            revision = -1
    else:
        revision = -1
    if revision < 0 or revision > _MAX_REVISION:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": "PHASE6_EXPECTED_REVISION_INVALID"},
        )
    return revision


def _normalized_payload(
    value: Any,
    expected_revision: int | None,
    expected_project_id: str,
) -> dict[str, Any]:
    payload = _mapping_payload(value)
    if payload.get("schema_version") != _WEB_SCHEMA:
        raise ValueError("Phase 6 snapshot adapter returned an unknown schema")
    if payload.get("project_id") != expected_project_id:
        raise ValueError("Phase 6 snapshot adapter returned a different project")
    state = payload.get("state")
    if state not in _VIEW_STATES:
        raise ValueError("Phase 6 snapshot adapter returned an invalid state")

    # The Web endpoint never exposes authority-like records.  Every Phase 6
    # adapter result must state the three safety bits rather than relying on a
    # permissive default.
    for field in ("authoritative", "authority_transferred", "dispatch_performed"):
        if payload.get(field) is not False:
            raise ValueError("Phase 6 snapshot adapter returned unsafe authority flags")

    if state != "ready":
        return {
            "schema_version": _WEB_SCHEMA,
            "state": state,
            "project_id": expected_project_id,
            "reason_code": _safe_reason_code(
                payload.get("reason_code"), _STATE_REASON_CODES[state]
            ),
            "authoritative": False,
            "authority_transferred": False,
            "dispatch_performed": False,
        }

    snapshot_id = payload.get("snapshot_id")
    revision = payload.get("revision")
    coordinate = payload.get("coordinate")
    if not isinstance(snapshot_id, str) or _SNAPSHOT_ID.fullmatch(snapshot_id) is None:
        raise ValueError("Phase 6 snapshot adapter returned an invalid snapshot ID")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
        raise ValueError("Phase 6 snapshot adapter returned an invalid revision")
    workflow_id = payload.get("workflow_id")
    authority_coordinate = payload.get("authority_coordinate")
    source_binding_sha256 = payload.get("source_binding_sha256")
    if not isinstance(workflow_id, str) or not workflow_id:
        raise ValueError("Phase 6 snapshot adapter returned an invalid workflow")
    if not isinstance(authority_coordinate, Mapping):
        raise ValueError("Phase 6 snapshot adapter omitted its authority coordinate")
    authority_revision = authority_coordinate.get("current_revision")
    if (
        authority_coordinate.get("workflow_id") != workflow_id
        or authority_coordinate.get("project_id") != expected_project_id
        or not isinstance(authority_revision, int)
        or isinstance(authority_revision, bool)
        or authority_revision != revision
    ):
        raise ValueError("Phase 6 snapshot adapter returned a mixed authority coordinate")
    if (
        not isinstance(source_binding_sha256, str)
        or _SNAPSHOT_ID.fullmatch(source_binding_sha256) is None
    ):
        raise ValueError("Phase 6 snapshot adapter returned an invalid source binding")
    if coordinate is not None and coordinate != {
        "snapshot_id": snapshot_id,
        "revision": revision,
    }:
        raise ValueError("Phase 6 snapshot adapter returned a mixed coordinate")
    if expected_revision is not None and revision != expected_revision:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "PHASE6_SNAPSHOT_STALE",
                "server_revision": revision,
            },
        )

    sections = payload.get("sections", [])
    actions = payload.get("actions", [])
    if not isinstance(sections, list) or not isinstance(actions, list):
        raise ValueError("Phase 6 snapshot adapter returned invalid collections")
    page_coordinate = {"snapshot_id": snapshot_id, "revision": revision}
    section_keys: set[str] = set()
    for section in sections:
        if not isinstance(section, Mapping):
            raise ValueError("Phase 6 snapshot adapter returned an invalid section")
        key = section.get("key")
        if not isinstance(key, str) or not key.strip() or key.strip() in section_keys:
            raise ValueError("Phase 6 snapshot adapter returned an invalid section key")
        section_keys.add(key.strip())
        has_nested = "coordinate" in section
        has_flat = "snapshot_id" in section or "revision" in section
        if has_nested and section.get("coordinate") != page_coordinate:
            raise ValueError("Phase 6 snapshot adapter returned a mixed section coordinate")
        if has_flat and (
            section.get("snapshot_id") != snapshot_id
            or section.get("revision") != revision
        ):
            raise ValueError("Phase 6 snapshot adapter returned a mixed section coordinate")
        if "data" not in section:
            raise ValueError("Phase 6 snapshot adapter returned section data without a value")
    action_ids: set[str] = set()
    for action in actions:
        if not isinstance(action, Mapping):
            raise ValueError("Phase 6 snapshot adapter returned an invalid action")
        action_id = action.get("id")
        if not isinstance(action_id, str) or not action_id.strip():
            raise ValueError("Phase 6 snapshot adapter returned an invalid action ID")
        canonical_action_id = action_id.strip()
        if canonical_action_id in action_ids:
            raise ValueError("Phase 6 snapshot adapter returned a duplicate action ID")
        action_ids.add(canonical_action_id)
        has_nested = "coordinate" in action
        has_flat = "snapshot_id" in action or "revision" in action
        if has_nested and action.get("coordinate") != page_coordinate:
            raise ValueError("Phase 6 snapshot adapter returned a mixed action coordinate")
        if has_flat and (
            action.get("snapshot_id") != snapshot_id
            or action.get("revision") != revision
        ):
            raise ValueError("Phase 6 snapshot adapter returned a mixed action coordinate")
    # Clone through strict JSON so response encoding cannot execute object
    # hooks or fail after this validation boundary. NaN/Infinity are rejected.
    sections = json.loads(json.dumps(sections, ensure_ascii=False, allow_nan=False))
    actions = json.loads(json.dumps(actions, ensure_ascii=False, allow_nan=False))
    for action in actions:
        action["coordinate"] = page_coordinate
        action["snapshot_id"] = snapshot_id
        action["revision"] = revision
    return {
        "schema_version": _WEB_SCHEMA,
        "state": "ready",
        "project_id": expected_project_id,
        "reason_code": None,
        "snapshot_id": snapshot_id,
        "revision": revision,
        "server_revision": revision,
        "coordinate": page_coordinate,
        "sections": sections,
        "actions": actions,
        "authoritative": False,
        "authority_transferred": False,
        "dispatch_performed": False,
    }


def _coded_store_error(exc: Exception) -> HTTPException:
    code = getattr(exc, "code", None)
    error_name = type(exc).__name__
    if not isinstance(code, str):
        code = {
            "Phase6SnapshotStale": "PHASE6_SNAPSHOT_STALE",
            "Phase6SnapshotNotFound": "PHASE6_SNAPSHOT_NOT_FOUND",
            "Phase6SnapshotConflict": "PHASE6_SNAPSHOT_INCONSISTENT",
            "Phase6StoreError": "PHASE6_SNAPSHOT_INCONSISTENT",
        }.get(error_name)
    if code == "PHASE6_SNAPSHOT_STALE":
        server_revision = getattr(exc, "server_revision", None)
        detail: dict[str, Any] = {"code": code}
        if isinstance(server_revision, int) and not isinstance(server_revision, bool):
            detail["server_revision"] = server_revision
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)
    if code in {"PHASE6_SNAPSHOT_NOT_FOUND", "PHASE6_SOURCE_INELIGIBLE"}:
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=code)
    if code in {
        "PHASE6_SNAPSHOT_EXPIRED",
        "PHASE6_GRANT_EXPIRED",
        "PHASE6_GRANT_REVOKED",
    }:
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=code)
    if code in {"PHASE6_SNAPSHOT_TAMPERED", "PHASE6_SNAPSHOT_INCONSISTENT"}:
        return HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=code)
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="PHASE6_SNAPSHOT_UNAVAILABLE",
    )


def create_phase6_router(
    settings: Settings,
    *,
    snapshot_loader: SnapshotLoader | None = None,
) -> APIRouter:
    router = APIRouter()
    current_user_dependency = get_current_user(settings)

    @router.get("/api/projects/{base_name}/phase6-snapshot")
    def get_phase6_project_snapshot(
        base_name: str,
        expected_revision: str | None = Query(default=None),
        current_user: UserInfo = Depends(current_user_dependency),
    ):
        # Keep this order explicit and testable: auth is resolved by FastAPI's
        # dependency first, then project ACL, then the gate, and only then path
        # resolution/import/store access.
        require_project_access(settings, current_user, base_name)
        parsed_expected_revision = _parse_expected_revision(expected_revision)
        if not settings.phase6_snapshot_enabled:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="PHASE6_SNAPSHOT_DISABLED",
            )

        db_path: Path = settings.resolved_phase6_snapshot_db_file
        if not db_path.is_absolute():
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="PHASE6_CONFIGURATION_INVALID",
            )
        try:
            loader = snapshot_loader or _lazy_snapshot_loader()
            value = loader(
                db_path=db_path,
                project_id=base_name,
                expected_revision=parsed_expected_revision,
            )
        except Exception as exc:
            _LOG.error(
                "Phase 6 snapshot read failed for an authorized project "
                "(error_type=%s)",
                type(exc).__name__,
            )
            raise _coded_store_error(exc) from exc
        try:
            return _normalized_payload(value, parsed_expected_revision, base_name)
        except HTTPException:
            # Only normalization itself constructs a public stale response.
            raise
        except Exception as exc:
            _LOG.error(
                "Phase 6 snapshot response validation failed "
                "(error_type=%s)",
                type(exc).__name__,
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="PHASE6_SNAPSHOT_UNAVAILABLE",
            ) from exc

    return router


__all__ = ["create_phase6_router"]
