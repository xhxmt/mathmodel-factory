from __future__ import annotations

import os
import re
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


# Provider failures recognised in a candidate's log tail, most explanatory
# first.  A status code counts only right after HTTP/status/code/error, so a
# stray "402" (a line number, a token count) does not.  The phrases that also
# occur in ordinary prose or echoed code ("rate limit", "overloaded", "high
# demand") count only on a line that reports a failure (_FAILURE_CONTEXT);
# the balance and credential phrases are unambiguous on their own.
_STATUS = r"(?:http|status|code|error)\W{0,3}"
_PROVIDER_SIGNALS: tuple[tuple[str, re.Pattern[str], bool], ...] = (
    (
        "balance or quota exhausted",
        re.compile(
            r"insufficient[ _]balance|insufficient_quota|exceeded your current quota"
            r"|credit balance is too low|payment required|" + _STATUS + r"402\b",
            re.IGNORECASE,
        ),
        False,
    ),
    (
        "credential rejected",
        re.compile(
            r"invalid[ _]api[ _]key|incorrect api key|authentication[ _](?:failed|error)|"
            + _STATUS + r"40[13]\b",
            re.IGNORECASE,
        ),
        False,
    ),
    (
        "rate limited",
        re.compile(r"rate[ _]limit|too many requests|" + _STATUS + r"429\b", re.IGNORECASE),
        True,
    ),
    (
        "provider overloaded",
        re.compile(r"high demand|overloaded|service unavailable|" + _STATUS + r"50[23]\b", re.IGNORECASE),
        True,
    ),
)
_FAILURE_CONTEXT = re.compile(
    # "too many requests" is the HTTP 429 reason phrase: a failure report by itself.
    r"\b(?:error|failed|failure|exceeded|retry(?:ing)?|reconnect(?:ing)?|too many requests"
    r"|http\s*\d{3}|status\W{0,3}\d{3})\b",
    re.IGNORECASE,
)
_LOG_TAIL_BYTES = 65_536


def _provider_signal(project_dir: Path, log: object) -> str:
    if not log:
        return ""
    path = Path(str(log))
    if not path.is_absolute():
        path = project_dir / path
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            handle.seek(max(0, handle.tell() - _LOG_TAIL_BYTES))
            tail = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
    lines = tail.splitlines()
    for label, pattern, needs_context in _PROVIDER_SIGNALS:
        for line in lines:
            if pattern.search(line) and (not needs_context or _FAILURE_CONTEXT.search(line)):
                return label
    return ""


@dataclass(frozen=True)
class CandidateAttempt:
    model_id: str
    result: ExecutionResult
    note: str = ""
    provider_signal: str = ""

    @property
    def rank(self) -> int:
        """Lower explains the step's failure better.

        A provider refusing the account is the actionable cause; a fallback
        whose executable is missing, or a candidate skipped by quarantine,
        explains nothing about why the run could not proceed.
        """

        if self.provider_signal:
            return [label for label, _, _ in _PROVIDER_SIGNALS].index(self.provider_signal)
        error_class = self.result.error_class
        if error_class == "TRANSIENT_ARTIFACT_MISSING":
            return 10  # the model ran and produced nothing usable
        if error_class == "PERMANENT_MODEL_UNSUPPORTED":
            return 11
        if error_class in {"TRANSIENT_MODEL_BACKEND", "TRANSIENT_TIMEOUT"} or not error_class.startswith("PERMANENT"):
            return 12
        if self.result.metadata.get("missing_key_env"):
            return 20
        if error_class == "PERMANENT_BACKEND_UNAVAILABLE":
            return 21  # launch error: executable missing
        if self.note.startswith("skipped"):
            return 30
        return 25

    def public(self) -> dict[str, object]:
        metadata = self.result.metadata
        record: dict[str, object] = {
            "model_id": self.model_id,
            "backend": metadata.get("backend", ""),
            "error_class": self.result.error_class,
            "returncode": self.result.returncode,
        }
        for key in ("log", "duration_seconds", "reason", "launch_error", "message"):
            if metadata.get(key) not in (None, ""):
                record[key] = metadata[key]
        if self.provider_signal:
            record["provider_signal"] = self.provider_signal
        if self.note:
            record["note"] = self.note
        return record


def _aggregate_failure(attempts: list[CandidateAttempt], last: ExecutionResult) -> ExecutionResult:
    """The step's failure: the most explanatory candidate, with every attempt."""

    if not attempts:
        return last
    primary = min(enumerate(attempts), key=lambda item: (item[1].rank, item[0]))[1]
    metadata = {
        **primary.result.metadata,
        "candidate_attempts": [attempt.public() for attempt in attempts],
        "primary_candidate": primary.model_id,
    }
    if primary.provider_signal:
        metadata["provider_signal"] = primary.provider_signal
        if not str(metadata.get("reason") or "").strip():
            metadata["reason"] = f"provider: {primary.provider_signal}"
    return ExecutionResult(
        returncode=primary.result.returncode,
        error_class=primary.result.error_class,
        metadata=metadata,
    )


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
        # Every candidate that did not succeed, in order.  The step's failure is
        # reported from the most explanatory one, not just the last fallback.
        attempts: list[CandidateAttempt] = []
        for model_id in candidates:
            if model_id in self._quarantined_models:
                last = ExecutionResult.failed(
                    "PERMANENT_MODEL_CONFIG",
                    returncode=2,
                    model_id=model_id,
                    quarantine=self._quarantined_models[model_id],
                )
                attempts.append(CandidateAttempt(model_id, last, "skipped: quarantined earlier in this worker"))
                continue
            entry = self._entry(model_id)
            if entry is None:
                last = ExecutionResult.failed(
                    "PERMANENT_MODEL_CONFIG", returncode=2, model_id=model_id
                )
                attempts.append(CandidateAttempt(model_id, last, "not in the model registry"))
                continue
            backend_name = entry["backend"]
            if os.getenv("CODEX_ONLY", "0").lower() in {"1", "true", "yes", "on"} and backend_name != "codex":
                attempts.append(
                    CandidateAttempt(
                        model_id,
                        ExecutionResult.failed("PERMANENT_MODEL_CONFIG", returncode=2,
                                               model_id=model_id, backend=backend_name),
                        "skipped: CODEX_ONLY allows only the codex backend",
                    )
                )
                continue
            try:
                backend = self.backends.get(backend_name)
            except KeyError:
                last = ExecutionResult.failed(
                    "PERMANENT_MODEL_CONFIG", returncode=2, model_id=model_id
                )
                attempts.append(CandidateAttempt(model_id, last, f"backend {backend_name!r} is not registered"))
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
                attempts.append(CandidateAttempt(model_id, last, f"{key_env} is not set"))
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
            attempts.append(
                CandidateAttempt(
                    model_id,
                    last,
                    # A process that exited 0 was not refused by its provider,
                    # whatever warnings its log carries.
                    provider_signal=(
                        _provider_signal(request.project_dir, last.metadata.get("log"))
                        if backend_result.returncode != 0
                        else ""
                    ),
                )
            )
        return _aggregate_failure(attempts, last)

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
