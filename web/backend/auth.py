import secrets
import time
from typing import Any


class WsTicketStore:
    def __init__(self, ttl_seconds: int):
        self.ttl_seconds = ttl_seconds
        self._tickets: dict[str, tuple[float, dict[str, Any]]] = {}

    def issue(self, payload: dict[str, Any]) -> str:
        self._prune_expired()
        ticket = secrets.token_urlsafe(32)
        self._tickets[ticket] = (time.monotonic() + self.ttl_seconds, dict(payload))
        return ticket

    def consume(self, ticket: str) -> dict[str, Any] | None:
        self._prune_expired()
        entry = self._tickets.pop(ticket, None)
        if entry is None:
            return None

        expires_at, payload = entry
        if expires_at <= time.monotonic():
            return None

        return payload

    def _prune_expired(self) -> None:
        now = time.monotonic()
        expired = [
            ticket for ticket, (expires_at, _payload) in self._tickets.items()
            if expires_at <= now
        ]
        for ticket in expired:
            self._tickets.pop(ticket, None)
