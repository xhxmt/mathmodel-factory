from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol


CONTINUE_AFTER_GATE2 = "continue_after_gate2"
DELIVER_SNAPSHOT = "deliver_snapshot"
OVERRIDE_SCOPES = frozenset({CONTINUE_AFTER_GATE2, DELIVER_SNAPSHOT})
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class DeliveryOverride:
    override_id: str
    base_name: str
    scope: str
    bound_snapshot_id: str | None
    source_verdict: str
    reason: str
    actor: str
    issued_at: int
    expires_at: int | None = None
    revoked_at: int | None = None
    consumed_at: int | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class OverrideProvider(Protocol):
    def active_override(
        self,
        base_name: str,
        scope: str,
        *,
        snapshot_id: str | None = None,
    ) -> DeliveryOverride | None: ...

    def consume(self, override_id: str) -> bool: ...

    def get_override(self, override_id: str) -> DeliveryOverride | None: ...


class NullOverrideProvider:
    def active_override(
        self,
        base_name: str,
        scope: str,
        *,
        snapshot_id: str | None = None,
    ) -> DeliveryOverride | None:
        del base_name, scope, snapshot_id
        return None

    def consume(self, override_id: str) -> bool:
        del override_id
        return False

    def get_override(self, override_id: str) -> DeliveryOverride | None:
        del override_id
        return None


class SQLiteOverrideProvider:
    """Read administrator-issued delivery overrides from the control-plane DB.

    Project files are deliberately not consulted. This is an operational
    governance boundary for the single-operator deployment; it is not a
    cryptographic boundary against another process running as the same Unix UID.
    """

    def __init__(self, db_file: str | Path):
        self.db_file = Path(db_file)

    def active_override(
        self,
        base_name: str,
        scope: str,
        *,
        snapshot_id: str | None = None,
    ) -> DeliveryOverride | None:
        if scope not in OVERRIDE_SCOPES or not self.db_file.is_file():
            return None
        if scope == DELIVER_SNAPSHOT and not self._valid_snapshot(snapshot_id):
            return None
        now = int(time.time())
        try:
            with self._connect() as conn:
                row = conn.execute(
                    """
                    SELECT * FROM delivery_overrides
                    WHERE base_name = ? AND scope = ?
                      AND revoked_at IS NULL AND consumed_at IS NULL
                      AND (expires_at IS NULL OR expires_at > ?)
                      AND (? != 'deliver_snapshot' OR bound_snapshot_id = ?)
                    ORDER BY issued_at DESC, override_id DESC
                    LIMIT 1
                    """,
                    (base_name, scope, now, scope, snapshot_id),
                ).fetchone()
        except sqlite3.Error:
            return None
        return self._from_row(row) if row is not None else None

    def consume(self, override_id: str) -> bool:
        if not self.db_file.is_file():
            return False
        now = int(time.time())
        try:
            with self._connect() as conn:
                cursor = conn.execute(
                    """
                    UPDATE delivery_overrides
                    SET consumed_at = ?
                    WHERE override_id = ? AND revoked_at IS NULL
                      AND consumed_at IS NULL
                      AND (expires_at IS NULL OR expires_at > ?)
                    """,
                    (now, override_id, now),
                )
                return cursor.rowcount == 1
        except sqlite3.Error:
            return False

    def get_override(self, override_id: str) -> DeliveryOverride | None:
        if not self.db_file.is_file():
            return None
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT * FROM delivery_overrides WHERE override_id = ?",
                    (override_id,),
                ).fetchone()
        except sqlite3.Error:
            return None
        return self._from_row(row) if row is not None else None

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_file)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _valid_snapshot(value: str | None) -> bool:
        return isinstance(value, str) and bool(SHA256_RE.fullmatch(value))

    @staticmethod
    def _from_row(row: sqlite3.Row) -> DeliveryOverride:
        return DeliveryOverride(
            override_id=str(row["override_id"]),
            base_name=str(row["base_name"]),
            scope=str(row["scope"]),
            bound_snapshot_id=row["bound_snapshot_id"],
            source_verdict=str(row["source_verdict"] or ""),
            reason=str(row["reason"]),
            actor=str(row["actor"]),
            issued_at=int(row["issued_at"]),
            expires_at=row["expires_at"],
            revoked_at=row["revoked_at"],
            consumed_at=row["consumed_at"],
        )


def default_override_provider(factory_root: str | Path) -> OverrideProvider:
    return SQLiteOverrideProvider(Path(factory_root).resolve() / "web" / "auth.db")
