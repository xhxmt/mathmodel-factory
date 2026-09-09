"""Read-only Phase 9 delivery fence for actual delivery side-effect APIs.

Phase 9 is a forensic-replay phase.  Its only supported run generation has no
delivery capability, so this module deliberately has no Phase 9 allow-path.
Phase 10 must introduce its own, separately audited authorization contract
instead of teaching a Phase 9 receipt how to authorize delivery.

The generic audit and pre-Phase9 delivery paths predate Authority Phase 9 and
remain supported.  :func:`require_delivery_side_effect_authority` first makes
a read-only classification: it applies this permanent fence only when the
caller supplied a Phase9 coordinate or the project has a current
``FORENSIC_REPLAY`` generation.  It never fabricates an allow-valued Phase9
fence for legacy callers.
"""

from __future__ import annotations

from contextlib import contextmanager, ExitStack
from dataclasses import dataclass
from pathlib import Path
import sqlite3
from typing import Iterator

from .phase9_authority_lease import (
    AuthorityStateLeaseError,
    authority_state_commit_lease,
    isolated_authority_snapshot_ro,
)


PHASE9_RUN_MODE = "FORENSIC_REPLAY"
PHASE9_MODELING_CONTRACT = "LEGACY_NOT_APPLICABLE"
PHASE9_DELIVERY_CAPABILITY = "DISABLED"
PHASE9_SIDE_EFFECTS = frozenset(
    {"acceptance", "release", "submission", "delivery", "completion", "archive"}
)
_PHASE9_SCHEMA_OBJECT_PREFIXES = (
    "authority_production_run_generation",
    "authority_production_phase9_",
)
_PHASE9_MIGRATION_IDS = frozenset(
    {
        "A2_0015_PHASE9_RUN_GENERATION",
        "A2_0016_PHASE9_FORENSIC_REPLAY",
        "A2_0017_PHASE9_AUDIT_HARDENING",
        "A2_0018_PHASE9_P0_RUNNER_ATTESTATION",
        "A2_0019_PHASE9_REPLAY_EVIDENCE_ATTESTATION",
    }
)


class Phase9DeliveryFenceError(ValueError):
    """The current Authority coordinate cannot authorize a delivery effect."""


@dataclass(frozen=True)
class Phase9DeliveryFence:
    project_id: str
    workflow_id: str
    run_generation: str
    replay_id: str
    replay_mode: str
    terminal_receipt_sha256: str
    run_mode: str
    modeling_consultation_contract: str
    delivery_capability: str


@dataclass(frozen=True)
class _CurrentPhase9Generation:
    project_id: str
    workflow_id: str
    run_generation: str


def _side_effect_name(operation: str) -> str:
    if operation not in PHASE9_SIDE_EFFECTS:
        raise Phase9DeliveryFenceError(
            f"unsupported Phase9 side-effect operation: {operation}"
        )
    return operation


def _is_phase9_schema_object(name: str) -> bool:
    return name.startswith(_PHASE9_SCHEMA_OBJECT_PREFIXES)


def _phase9_migration_marker_present(
    connection: sqlite3.Connection, table_names: set[str]
) -> bool:
    """Recognize Phase9 even when its primary control tables were removed."""

    if "authority_production_schema_state" in table_names:
        state = connection.execute(
            "SELECT production_schema_version, last_completed_migration "
            "FROM authority_production_schema_state WHERE singleton=1"
        ).fetchone()
        if state is not None:
            version = state["production_schema_version"]
            if type(version) is not int:
                raise Phase9DeliveryFenceError(
                    "Phase9 delivery classification found malformed production "
                    "schema state"
                )
            if (
                version >= 3
                or state["last_completed_migration"] in _PHASE9_MIGRATION_IDS
            ):
                return True
    if "authority_production_migrations" in table_names:
        migration_ids = {
            str(row[0])
            for row in connection.execute(
                "SELECT migration_id FROM authority_production_migrations"
            ).fetchall()
        }
        if migration_ids & _PHASE9_MIGRATION_IDS:
            return True
    return False


def _nonempty_phase9_runtime_tables(
    connection: sqlite3.Connection,
    table_names: set[str],
    *,
    exclude: set[str],
) -> tuple[str, ...]:
    """List Phase9 tables whose rows disprove an unused READY foundation."""

    nonempty: list[str] = []
    for name in sorted(table_names):
        if name in exclude or not _is_phase9_schema_object(name):
            continue
        quoted = '"' + name.replace('"', '""') + '"'
        if (
            connection.execute(f"SELECT 1 FROM {quoted} LIMIT 1").fetchone()
            is not None
        ):
            nonempty.append(name)
    return tuple(nonempty)


def _current_phase9_generation(
    project: str | Path,
) -> _CurrentPhase9Generation | None:
    """Classify a project without creating or upgrading its state database."""

    # Keep the production Authority implementation outside the default active
    # CLI import graph.  It is loaded only when a real fence classification is
    # requested, preserving the Phase1-8/default-off module boundary.
    from .authority_production_schema import (
        AuthorityProductionSchemaError,
        verify_production_installation,
    )

    root = Path(project).resolve()
    database_candidate = root / ".factory" / "state.db"
    try:
        with authority_state_commit_lease(root):
            try:
                database_candidate.lstat()
            except FileNotFoundError:
                return None
            with isolated_authority_snapshot_ro(database_candidate) as connection:
                connection.execute("BEGIN")
                schema_objects = {
                    (str(row[0]), str(row[1]))
                    for row in connection.execute(
                        "SELECT type, name FROM sqlite_master "
                        "WHERE name NOT LIKE 'sqlite_%'"
                    ).fetchall()
                }
                table_names = {
                    name
                    for object_type, name in schema_objects
                    if object_type == "table"
                }
                required = {
                    "authority_production_run_generations",
                    "authority_production_run_generation_current",
                }
                tables = table_names & required
                if not tables:
                    phase9_objects = {
                        name
                        for _object_type, name in schema_objects
                        if _is_phase9_schema_object(name)
                    }
                    if phase9_objects or _phase9_migration_marker_present(
                        connection, table_names
                    ):
                        raise Phase9DeliveryFenceError(
                            "Phase9 delivery classification found Phase9 markers "
                            "without run-generation controls"
                        )
                    connection.commit()
                    return None
                if tables != required:
                    raise Phase9DeliveryFenceError(
                        "Phase9 delivery classification found incomplete "
                        "run-generation controls"
                    )
                generation_rows = connection.execute(
                    """
                    SELECT project_id, workflow_id, run_generation, run_mode
                    FROM authority_production_run_generations
                    ORDER BY workflow_id, run_generation
                    """
                ).fetchall()
                current_rows = connection.execute(
                    """
                    SELECT c.workflow_id AS current_workflow_id,
                           c.run_generation AS current_run_generation,
                           g.project_id, g.workflow_id, g.run_generation, g.run_mode
                    FROM authority_production_run_generation_current c
                    LEFT JOIN authority_production_run_generations g
                      ON g.workflow_id=c.workflow_id
                     AND g.run_generation=c.run_generation
                    ORDER BY c.workflow_id, c.run_generation
                    """,
                ).fetchall()
                dangling = [
                    row
                    for row in current_rows
                    if row["project_id"] is None
                    or row["workflow_id"] is None
                    or row["run_generation"] is None
                    or row["run_mode"] is None
                ]
                if dangling:
                    raise Phase9DeliveryFenceError(
                        "Phase9 delivery classification found a current generation "
                        "without its immutable generation record"
                    )
                if any(row["run_mode"] != PHASE9_RUN_MODE for row in current_rows):
                    raise Phase9DeliveryFenceError(
                        "Phase9 delivery classification found an invalid current "
                        "run-generation mode"
                    )
                if any(row["run_mode"] != PHASE9_RUN_MODE for row in generation_rows):
                    raise Phase9DeliveryFenceError(
                        "Phase9 delivery classification found an invalid immutable "
                        "run-generation mode"
                    )
                if len(current_rows) > 1:
                    raise Phase9DeliveryFenceError(
                        "Phase9 delivery classification found multiple current "
                        "FORENSIC_REPLAY generations"
                    )
                if generation_rows and not current_rows:
                    raise Phase9DeliveryFenceError(
                        "Phase9 delivery classification found immutable run-generation "
                        "history without a current generation pointer"
                    )
                if not generation_rows and not current_rows:
                    # These table names are Phase9 production controls, not a
                    # legacy schema marker.  Before treating an empty control
                    # set as a READY pre-Phase9 foundation, prove that the
                    # complete immutable production installation is present.
                    # Otherwise an attacker could create only two empty tables
                    # and make a malformed Authority database look legacy.
                    verify_production_installation(connection, require_ready=True)
                    residue = _nonempty_phase9_runtime_tables(
                        connection, table_names, exclude=required
                    )
                    if residue:
                        raise Phase9DeliveryFenceError(
                            "Phase9 delivery classification found Phase9 history "
                            "without run-generation control rows: "
                            + ", ".join(residue)
                        )
                connection.commit()
                if not current_rows:
                    return None
                row = current_rows[0]
                return _CurrentPhase9Generation(
                    project_id=str(row["project_id"]),
                    workflow_id=str(row["workflow_id"]),
                    run_generation=str(row["run_generation"]),
                )
    except (
        sqlite3.Error,
        AuthorityProductionSchemaError,
        AuthorityStateLeaseError,
        ValueError,
    ) as exc:
        if isinstance(exc, Phase9DeliveryFenceError):
            raise
        raise Phase9DeliveryFenceError(
            f"Phase9 delivery scope cannot be classified safely: {exc}"
        ) from exc


def legacy_delivery_projection_allowed(project: str | Path) -> bool:
    """Return whether legacy delivery artifacts may be shown as current.

    This is a read-only compatibility classifier, never delivery authority.
    A current Phase9 generation, a project binding mismatch, or an Authority
    database that cannot be classified safely all suppress legacy projections.
    """

    try:
        return _current_phase9_generation(project) is None
    except Phase9DeliveryFenceError:
        return False


def require_delivery_side_effect_authority(
    project: str | Path,
    *,
    operation: str,
    workflow_id: str | None = None,
    run_generation: str | None = None,
) -> None:
    """Allow a legacy side effect or apply the permanent Phase9 refusal.

    Absence of both an explicit Phase9 coordinate and a current Phase9
    ``FORENSIC_REPLAY`` generation is the only non-Phase9 allow branch.  A
    malformed or unreadable database is never reclassified as legacy.
    """

    side_effect = _side_effect_name(operation)
    current = _current_phase9_generation(project)
    coordinate_supplied = workflow_id is not None or run_generation is not None
    if current is None and not coordinate_supplied:
        return
    if current is not None and current.project_id != Path(project).resolve().name:
        raise Phase9DeliveryFenceError(
            f"Phase9 {side_effect} project binding does not match the project directory"
        )
    if (
        not isinstance(workflow_id, str)
        or not workflow_id.strip()
        or not isinstance(run_generation, str)
        or not run_generation.strip()
    ):
        raise Phase9DeliveryFenceError(
            f"Phase9 {side_effect} requires explicit workflow_id and run_generation"
        )
    if current is None:
        raise Phase9DeliveryFenceError(
            f"Phase9 {side_effect} coordinate does not match a current "
            "FORENSIC_REPLAY generation"
        )
    if (
        current.workflow_id != workflow_id
        or current.run_generation != run_generation
    ):
        raise Phase9DeliveryFenceError(
            f"Phase9 {side_effect} coordinate does not match the current "
            "FORENSIC_REPLAY generation"
        )
    require_phase9_delivery_authority(
        project,
        workflow_id=workflow_id,
        run_generation=run_generation,
        operation=side_effect,
    )


@contextmanager
def delivery_side_effect_commit_lease(
    project: str | Path,
    *,
    operation: str,
    workflow_id: str | None = None,
    run_generation: str | None = None,
) -> Iterator[None]:
    """Serialize a final side-effect commit with Phase9 Authority writers.

    Preliminary calls to :func:`require_delivery_side_effect_authority` remain
    useful for rejecting before staging work.  This context manager is the
    required final boundary: participating Phase9 generation/replay writers
    take the same database-inode lease for their write transaction, so a
    side-effect classification cannot become stale before its commit.
    """

    root = Path(project).resolve()
    try:
        with authority_state_commit_lease(root):
            require_delivery_side_effect_authority(
                root,
                operation=operation,
                workflow_id=workflow_id,
                run_generation=run_generation,
            )
            yield
    except AuthorityStateLeaseError as exc:
        raise Phase9DeliveryFenceError(
            f"Phase9 {operation} Authority commit lease failed closed: {exc}"
        ) from exc


def collect_phase9_delivery_fence(
    project: str | Path,
    *,
    workflow_id: str | None = None,
    run_generation: str | None = None,
) -> Phase9DeliveryFence:
    """Read one exact current Phase 9 coordinate without creating a database."""

    from .authority_production_schema import (
        AuthorityProductionSchemaError,
        verify_production_installation,
    )

    root = Path(project).resolve()
    connection: sqlite3.Connection | None = None
    contexts = ExitStack()
    try:
        contexts.enter_context(authority_state_commit_lease(root))
        connection = contexts.enter_context(
            isolated_authority_snapshot_ro(root / ".factory" / "state.db")
        )
        connection.execute("BEGIN")
        verify_production_installation(connection, require_ready=True)
        parameters: list[object] = [root.name]
        predicates = ["w.project_id=?"]
        if workflow_id is not None:
            predicates.append("w.workflow_id=?")
            parameters.append(workflow_id)
        if run_generation is not None:
            predicates.append("g.run_generation=?")
            parameters.append(run_generation)
        rows = connection.execute(
            f"""
            SELECT w.project_id, w.workflow_id,
                   g.run_generation, g.run_mode,
                   g.modeling_consultation_contract, g.delivery_capability,
                   p.replay_id, r.replay_mode,
                   r.delivery_capability AS replay_delivery_capability,
                   t.receipt_sha256 AS terminal_receipt_sha256
            FROM authority_workflows w
            JOIN authority_production_run_generation_current c
              ON c.workflow_id=w.workflow_id
             AND c.run_generation=w.run_generation
            JOIN authority_production_run_generations g
              ON g.workflow_id=w.workflow_id
             AND g.run_generation=c.run_generation
            JOIN authority_production_phase9_replay_current p
              ON p.workflow_id=w.workflow_id
             AND p.run_generation=g.run_generation
             AND p.state='COMPLETED'
            JOIN authority_production_phase9_replays r
              ON r.replay_id=p.replay_id
             AND r.workflow_id=p.workflow_id
             AND r.run_generation=p.run_generation
            JOIN authority_production_phase9_terminal_receipts t
              ON t.workflow_id=w.workflow_id
             AND t.run_generation=g.run_generation
             AND t.replay_id=p.replay_id
             AND t.receipt_sha256=p.terminal_receipt_sha256
             AND t.final_event_sha256=p.final_event_sha256
            WHERE {' AND '.join(predicates)}
            """,
            tuple(parameters),
        ).fetchall()
        if len(rows) != 1:
            raise Phase9DeliveryFenceError(
                "delivery requires one explicitly bound current Phase9 terminal"
            )
        row = rows[0]
        # A row-level join only proves that several foreign-key coordinates
        # agree.  It does not prove that the terminal is the result of the
        # complete, typed Phase9 replay graph.  Reconstruct that graph in this
        # same read transaction before exposing even a read-only release.  In
        # particular, a hash-consistent but semantically forged terminal must
        # never become a delivery authority.
        try:
            from .phase9_forensic_replay import (
                Phase9ForensicReplayError,
                validate_current_phase9_completed_replay_in_transaction,
            )

            validated = validate_current_phase9_completed_replay_in_transaction(
                connection,
                workflow_id=str(row["workflow_id"]),
                expected_run_generation=str(row["run_generation"]),
                expected_terminal_receipt_sha256=str(
                    row["terminal_receipt_sha256"]
                ),
            )
        except Phase9ForensicReplayError as exc:
            raise Phase9DeliveryFenceError(
                f"current Phase9 terminal graph is invalid: {exc}"
            ) from exc
        result = Phase9DeliveryFence(
            project_id=str(row["project_id"]),
            workflow_id=str(row["workflow_id"]),
            run_generation=str(row["run_generation"]),
            replay_id=str(row["replay_id"]),
            replay_mode=str(row["replay_mode"]),
            terminal_receipt_sha256=str(row["terminal_receipt_sha256"]),
            run_mode=str(row["run_mode"]),
            modeling_consultation_contract=str(
                row["modeling_consultation_contract"]
            ),
            delivery_capability=str(row["delivery_capability"]),
        )
        if row["replay_delivery_capability"] != result.delivery_capability:
            raise Phase9DeliveryFenceError(
                "current replay and generation delivery capabilities differ"
            )
        if (
            validated.get("workflow_id") != result.workflow_id
            or validated.get("run_generation") != result.run_generation
            or validated.get("replay_id") != result.replay_id
            or validated.get("terminal_receipt_sha256")
            != result.terminal_receipt_sha256
        ):
            raise Phase9DeliveryFenceError(
                "reconstructed Phase9 terminal coordinate differs"
            )
        connection.commit()
        return result
    except (
        sqlite3.Error,
        AuthorityProductionSchemaError,
        AuthorityStateLeaseError,
        ValueError,
    ) as exc:
        if connection is not None:
            connection.rollback()
        if isinstance(exc, Phase9DeliveryFenceError):
            raise
        raise Phase9DeliveryFenceError(
            f"Phase9 delivery Authority cannot be verified: {exc}"
        ) from exc
    finally:
        contexts.close()


def require_phase9_delivery_authority(
    project: str | Path,
    *,
    workflow_id: str | None = None,
    run_generation: str | None = None,
    operation: str = "delivery",
) -> Phase9DeliveryFence:
    """Fail closed for every Phase 9 release/acceptance/submission attempt."""

    side_effect = _side_effect_name(operation)
    if (
        not isinstance(workflow_id, str)
        or not workflow_id.strip()
        or not isinstance(run_generation, str)
        or not run_generation.strip()
    ):
        raise Phase9DeliveryFenceError(
            f"Phase9 {side_effect} requires explicit workflow_id and run_generation"
        )
    current = _current_phase9_generation(project)
    if current is not None and current.project_id != Path(project).resolve().name:
        raise Phase9DeliveryFenceError(
            f"Phase9 {side_effect} project binding does not match the project directory"
        )
    if current is not None and (
        current.workflow_id != workflow_id
        or current.run_generation != run_generation
    ):
        raise Phase9DeliveryFenceError(
            f"Phase9 {side_effect} coordinate does not match the current "
            "FORENSIC_REPLAY generation"
        )
    fence = collect_phase9_delivery_fence(
        project,
        workflow_id=workflow_id,
        run_generation=run_generation,
    )
    if (
        fence.project_id != Path(project).resolve().name
        or fence.workflow_id != workflow_id
        or fence.run_generation != run_generation
    ):
        raise Phase9DeliveryFenceError(
            f"Phase9 {side_effect} coordinate does not match the current terminal"
        )
    if (
        fence.run_mode != PHASE9_RUN_MODE
        or fence.modeling_consultation_contract != PHASE9_MODELING_CONTRACT
        or fence.delivery_capability != PHASE9_DELIVERY_CAPABILITY
        or fence.replay_mode not in {"TECHNICAL", "ABLATE_NO_JUDGE"}
    ):
        raise Phase9DeliveryFenceError(
            f"Phase9 {side_effect} coordinate is not a valid forensic coordinate"
        )
    raise Phase9DeliveryFenceError(
        f"Phase9 {side_effect} is permanently disabled: delivery DISABLED for "
        "forensic, technical, and ablation generations"
    )
