from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from ...domain import ExecutionResult
from ...registry import ModelBackendRegistry
from scripts.model_dispatch_config import get_model_entry, get_step_model_ids

from .backends import ApiAgentBackend, ModelRequest


@dataclass(frozen=True)
class ModelPolicy:
    primary: str
    fallback: str = ""


_BUILTINS = {
    "codex": {"backend": "codex", "model": "", "effort": "xhigh", "base_url": "", "key_env": ""},
    "claude": {"backend": "claude", "model": "", "effort": "max", "base_url": "", "key_env": ""},
    "agy": {"backend": "agy", "model": "gemini-3.1-pro-preview", "effort": "", "base_url": "", "key_env": "GEMINI_API_KEY"},
    "dsh-deepseek": {"backend": "dsh", "model": "deepseek-flash", "effort": "", "base_url": "", "key_env": "DEEPSEEK_API_KEY"},
    "deepseek-chat": {"backend": "openai", "model": "deepseek-chat", "effort": "", "base_url": "https://api.deepseek.com", "key_env": "DEEPSEEK_API_KEY"},
}


class ModelDispatcher:
    def __init__(
        self,
        factory_root: str | Path,
        backends: ModelBackendRegistry,
        *,
        artifact_valid: Callable[[int, Path], bool] | None = None,
    ) -> None:
        self.root = Path(factory_root).resolve()
        self.backends = backends
        self.artifact_valid = artifact_valid
        self._quarantined_models: dict[str, dict[str, str]] = {}

    def policy_for(self, project_id: str, step_key: str | int, defaults: tuple[str, ...]) -> ModelPolicy:
        """Resolve the primary/fallback pair for one step.

        An operator assignment in ``web/model_config.json`` wins.  When there is
        none, the caller-provided catalog defaults become the policy.  Note that
        ``execute`` still appends ``defaults`` to the candidate chain even when an
        assignment exists; that escalation is a pinned product contract (see
        ``tests/test_native_orchestration.py::test_configured_models_fall_back_to_catalog_defaults``)
        and must not be tightened without an explicit decision.
        """
        assigned = get_step_model_ids(self.root / "web" / "model_config.json", project_id, step_key)
        if assigned:
            return ModelPolicy(*assigned)
        return ModelPolicy(defaults[0], defaults[1] if len(defaults) > 1 else "")

    def execute(
        self,
        request: ModelRequest,
        *,
        step_key: str | int,
        defaults: tuple[str, ...],
    ) -> ExecutionResult:
        if step_key == 2:
            from ...joint_modeling import policy

            if policy(request.project_dir)["enabled"]:
                from ...joint_modeling_executor import JointClaudeBackend

                # Explicit opt-in pins both proposals and critics. Never enter
                # the normal registry fallback loop for this modeling mode.
                return JointClaudeBackend(self.root).execute(request)
        policy = self.policy_for(request.project_dir.name, step_key, defaults)
        candidates: list[str] = []
        # Deliberate escalation: an explicit assignment does NOT stop the chain,
        # catalog defaults remain candidates after primary and fallback.  This is
        # the pinned contract described on ``policy_for``.
        for model_id in (policy.primary, policy.fallback, *defaults):
            if model_id and model_id not in candidates:
                candidates.append(model_id)
        last = ExecutionResult.failed("PERMANENT_MODEL_CONFIG", returncode=2)
        for model_id in candidates:
            if model_id in self._quarantined_models:
                last = ExecutionResult.failed(
                    "PERMANENT_MODEL_CONFIG",
                    returncode=2,
                    model_id=model_id,
                    quarantine=self._quarantined_models[model_id],
                )
                continue
            entry = self._entry(model_id)
            if entry is None:
                last = ExecutionResult.failed(
                    "PERMANENT_MODEL_CONFIG", returncode=2, model_id=model_id
                )
                continue
            backend_name = entry["backend"]
            if os.getenv("CODEX_ONLY", "0").lower() in {"1", "true", "yes", "on"} and backend_name != "codex":
                continue
            try:
                backend = self.backends.get(backend_name)
            except KeyError:
                last = ExecutionResult.failed(
                    "PERMANENT_MODEL_CONFIG", returncode=2, model_id=model_id
                )
                continue
            key_env = entry.get("key_env", "")
            if (
                key_env
                and isinstance(backend, ApiAgentBackend)
                and not (request.env.get(key_env) or os.getenv(key_env))
            ):
                self._quarantined_models[model_id] = {
                    "error_class": "PERMANENT_MODEL_CONFIG",
                    "reason": "required credential environment variable is unavailable",
                }
                last = ExecutionResult.failed(
                    "PERMANENT_MODEL_CONFIG",
                    returncode=2,
                    model_id=model_id,
                    backend=backend_name,
                    missing_key_env=key_env,
                )
                continue
            configured = replace(
                request,
                model=entry["model"],
                effort=entry["effort"],
                base_url=entry["base_url"],
                key_env=entry["key_env"],
                env={**request.env, "FACTORY_API_BACKEND": backend_name},
            )
            backend_result = backend.execute(configured)
            metadata = {
                **backend_result.metadata,
                "model_id": model_id,
                "backend": backend_name,
                "model": entry["model"],
            }
            last = ExecutionResult(
                returncode=backend_result.returncode,
                error_class=backend_result.error_class,
                metadata=metadata,
            )
            if last.error_class in {
                "PERMANENT_BACKEND_UNAVAILABLE",
                "PERMANENT_MODEL_CONFIG",
                "PERMANENT_MODEL_UNSUPPORTED",
            }:
                # ``PERMANENT_OUTPUT_CONTRACT`` is deliberately absent: every
                # producer of it (adapters/models/backends.py) reports a property
                # of the *current request* -- a missing output_file, or output
                # paths outside the project -- not an unusable model.  Quarantining
                # on it let one malformed request skip the model for every later
                # request sharing this dispatcher instance.
                self._quarantined_models[model_id] = {
                    "error_class": last.error_class,
                    "reason": str(last.metadata.get("reason") or "candidate failed native preflight"),
                }
            if last.returncode == 0:
                if self.artifact_valid is None or self.artifact_valid(
                    request.step_id, request.project_dir
                ):
                    return ExecutionResult.succeeded(**metadata)
                last = ExecutionResult.failed(
                    "TRANSIENT_ARTIFACT_MISSING",
                    returncode=1,
                    **metadata,
                )
        return last

    def execute_model_id(self, model_id: str, request: ModelRequest) -> ExecutionResult:
        entry = self._entry(model_id)
        if entry is None:
            return ExecutionResult.failed("PERMANENT_MODEL_CONFIG", returncode=2)
        backend = self.backends.get(entry["backend"])
        return backend.execute(
            replace(
                request,
                model=entry["model"],
                effort=entry["effort"],
                base_url=entry["base_url"],
                key_env=entry["key_env"],
                env={**request.env, "FACTORY_API_BACKEND": entry["backend"]},
            )
        )

    def _entry(self, model_id: str) -> dict[str, str] | None:
        if model_id in _BUILTINS:
            return dict(_BUILTINS[model_id])
        return get_model_entry(self.root / "web" / "model_registry.json", model_id)
