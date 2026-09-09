"""Durable envelope values for the default-off authority-schema shadow.

The current scheduler does not import this module.  These values define only
the bytes accepted by the Phase-2 persistence boundary; they do not authorize
dispatch, delivery, or a workflow transition.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass
from enum import Enum
import hashlib
import json
import re


EVENT_ENVELOPE_SCHEMA = "authority-event-envelope-v1"
RECEIPT_ENVELOPE_SCHEMA = "authority-receipt-envelope-v1"
OUTBOX_MESSAGE_SCHEMA = "authority-outbox-message-v1"

_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,191}\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


class AuthorityEnvelopeValidationError(ValueError):
    """Raised when a durable authority envelope is malformed."""


@dataclass(frozen=True)
class EnvelopeFieldV1:
    key: str
    value: str | int | bool | None


@dataclass(frozen=True)
class EventEnvelopeV1:
    schema_version: str
    event_id: str
    project_id: str
    workflow_id: str
    revision: int
    event_type: str
    command_id: str
    project_generation: str
    run_generation: str
    runtime_generation: str
    scheduler_generation: str
    contract_pin_set_sha256: str
    fields: tuple[EnvelopeFieldV1, ...]


@dataclass(frozen=True)
class ReceiptEnvelopeV1:
    schema_version: str
    receipt_id: str
    project_id: str
    workflow_id: str
    revision: int
    command_id: str
    event_id: str
    outcome: str
    contract_pin_set_sha256: str
    fields: tuple[EnvelopeFieldV1, ...]


@dataclass(frozen=True)
class OutboxMessageV1:
    schema_version: str
    message_id: str
    workflow_id: str
    revision: int
    event_id: str
    topic: str
    fields: tuple[EnvelopeFieldV1, ...]


def _text(value: object, path: str, *, identifier: bool = False, sha: bool = False) -> str:
    if type(value) is not str or not value:
        raise AuthorityEnvelopeValidationError(f"{path} must be a non-empty plain string")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise AuthorityEnvelopeValidationError(f"{path} must contain valid UTF-8") from exc
    if identifier and _IDENTIFIER_RE.fullmatch(value) is None:
        raise AuthorityEnvelopeValidationError(f"{path} must be a bounded authority identifier")
    if sha and _SHA256_RE.fullmatch(value) is None:
        raise AuthorityEnvelopeValidationError(f"{path} must be lowercase SHA-256")
    return value


def _revision(value: object, path: str) -> int:
    if type(value) is not int or value < 1:
        raise AuthorityEnvelopeValidationError(f"{path} must be a positive plain integer")
    return value


def _fields(value: object, path: str) -> tuple[EnvelopeFieldV1, ...]:
    if type(value) is not tuple:
        raise AuthorityEnvelopeValidationError(f"{path} must be an immutable tuple")
    keys: list[str] = []
    for index, item in enumerate(value):
        item_path = f"{path}[{index}]"
        if type(item) is not EnvelopeFieldV1:
            raise AuthorityEnvelopeValidationError(f"{item_path} has an unsupported runtime type")
        keys.append(_text(item.key, f"{item_path}.key", identifier=True))
        if type(item.value) not in {str, int, bool, type(None)}:
            raise AuthorityEnvelopeValidationError(f"{item_path}.value has an unsupported runtime type")
        if type(item.value) is str:
            _text(item.value, f"{item_path}.value")
    if keys != sorted(keys) or len(keys) != len(set(keys)):
        raise AuthorityEnvelopeValidationError(f"{path} must be uniquely sorted by key")
    return value


def validate_event_envelope(value: EventEnvelopeV1) -> EventEnvelopeV1:
    if type(value) is not EventEnvelopeV1:
        raise AuthorityEnvelopeValidationError("event envelope has an unsupported runtime type")
    if value.schema_version != EVENT_ENVELOPE_SCHEMA:
        raise AuthorityEnvelopeValidationError("event envelope schema is unsupported")
    _text(value.event_id, "event.event_id", identifier=True)
    _text(value.project_id, "event.project_id", identifier=True)
    _text(value.workflow_id, "event.workflow_id", identifier=True)
    _revision(value.revision, "event.revision")
    _text(value.event_type, "event.event_type", identifier=True)
    _text(value.command_id, "event.command_id", identifier=True)
    _text(value.project_generation, "event.project_generation", identifier=True)
    _text(value.run_generation, "event.run_generation", identifier=True)
    _text(value.runtime_generation, "event.runtime_generation", identifier=True)
    _text(value.scheduler_generation, "event.scheduler_generation", identifier=True)
    _text(value.contract_pin_set_sha256, "event.contract_pin_set_sha256", sha=True)
    _fields(value.fields, "event.fields")
    return value


def validate_receipt_envelope(value: ReceiptEnvelopeV1) -> ReceiptEnvelopeV1:
    if type(value) is not ReceiptEnvelopeV1:
        raise AuthorityEnvelopeValidationError("receipt envelope has an unsupported runtime type")
    if value.schema_version != RECEIPT_ENVELOPE_SCHEMA:
        raise AuthorityEnvelopeValidationError("receipt envelope schema is unsupported")
    _text(value.receipt_id, "receipt.receipt_id", identifier=True)
    _text(value.project_id, "receipt.project_id", identifier=True)
    _text(value.workflow_id, "receipt.workflow_id", identifier=True)
    _revision(value.revision, "receipt.revision")
    _text(value.command_id, "receipt.command_id", identifier=True)
    _text(value.event_id, "receipt.event_id", identifier=True)
    _text(value.outcome, "receipt.outcome", identifier=True)
    _text(value.contract_pin_set_sha256, "receipt.contract_pin_set_sha256", sha=True)
    _fields(value.fields, "receipt.fields")
    return value


def validate_outbox_message(value: OutboxMessageV1) -> OutboxMessageV1:
    if type(value) is not OutboxMessageV1:
        raise AuthorityEnvelopeValidationError("outbox message has an unsupported runtime type")
    if value.schema_version != OUTBOX_MESSAGE_SCHEMA:
        raise AuthorityEnvelopeValidationError("outbox message schema is unsupported")
    _text(value.message_id, "outbox.message_id", identifier=True)
    _text(value.workflow_id, "outbox.workflow_id", identifier=True)
    _revision(value.revision, "outbox.revision")
    _text(value.event_id, "outbox.event_id", identifier=True)
    _text(value.topic, "outbox.topic", identifier=True)
    _fields(value.fields, "outbox.fields")
    return value


def _stable_value(value: object) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: _stable_value(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, Enum):
        return _stable_value(value.value)
    if type(value) is tuple:
        return [_stable_value(item) for item in value]
    if type(value) in {str, int, bool, type(None)}:
        return value
    raise AuthorityEnvelopeValidationError("authority value has no stable JSON projection")


def stable_authority_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            _stable_value(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8", errors="strict")
    except (TypeError, UnicodeEncodeError, ValueError) as exc:
        raise AuthorityEnvelopeValidationError(
            "authority value cannot be serialized as stable JSON"
        ) from exc


def stable_authority_sha256(value: object) -> str:
    return hashlib.sha256(stable_authority_bytes(value)).hexdigest()


def _bytes(value: object, validator) -> bytes:
    return stable_authority_bytes(validator(value))


def _sha256(value: object, validator) -> str:
    return stable_authority_sha256(validator(value))


def event_envelope_bytes(value: EventEnvelopeV1) -> bytes:
    return _bytes(value, validate_event_envelope)


def event_envelope_sha256(value: EventEnvelopeV1) -> str:
    return _sha256(value, validate_event_envelope)


def receipt_envelope_bytes(value: ReceiptEnvelopeV1) -> bytes:
    return _bytes(value, validate_receipt_envelope)


def receipt_envelope_sha256(value: ReceiptEnvelopeV1) -> str:
    return _sha256(value, validate_receipt_envelope)


def outbox_message_bytes(value: OutboxMessageV1) -> bytes:
    return _bytes(value, validate_outbox_message)


def outbox_message_sha256(value: OutboxMessageV1) -> str:
    return _sha256(value, validate_outbox_message)
