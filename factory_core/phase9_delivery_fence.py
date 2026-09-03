"""Read-only Phase 9 delivery fence shared by every delivery side-effect API.

Phase 9 is a forensic-replay phase.  Its only supported run generation has no
delivery capability, so this module deliberately has no Phase 9 allow-path.
Phase 10 must introduce its own, separately audited authorization contract
instead of teaching a Phase 9 receipt how to authorize delivery.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sqlite3

from .authority_production_schema import (
    AuthorityProductionSchemaError,
    authority_database_path,
    connect_authority_ro,
    verify_production_installation,
)


PHASE9_RUN_MODE = "FORENSIC_REPLAY"
PHASE9_MODELING_CONTRACT = "LEGACY_NOT_APPLICABLE"
PHASE9_DELIVERY_CAPABILITY = "DISABLED"


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


def collect_phase9_delivery_fence(
    project: str | Path,
    *,
    workflow_id: str | None = None,
    run_generation: str | None = None,
) -> Phase9DeliveryFence:
    """Read one exact current Phase 9 coordinate without creating a database."""

    root = Path(project).resolve()
    connection: sqlite3.Connection | None = None
    try:
        database = authority_database_path(root / ".factory" / "state.db")
        connection = connect_authority_ro(database)
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
    except (sqlite3.Error, AuthorityProductionSchemaError, ValueError) as exc:
        if connection is not None:
            connection.rollback()
        if isinstance(exc, Phase9DeliveryFenceError):
            raise
        raise Phase9DeliveryFenceError(
            f"Phase9 delivery Authority cannot be verified: {exc}"
        ) from exc
    finally:
        if connection is not None:
            connection.close()


def require_phase9_delivery_authority(
    project: str | Path,
    *,
    workflow_id: str | None = None,
    run_generation: str | None = None,
) -> Phase9DeliveryFence:
    """Fail closed for every Phase 9 release/acceptance/submission attempt."""

    if (
        not isinstance(workflow_id, str)
        or not workflow_id.strip()
        or not isinstance(run_generation, str)
        or not run_generation.strip()
    ):
        raise Phase9DeliveryFenceError(
            "delivery requires explicit workflow_id and run_generation"
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
            "current Phase9 terminal does not bind the requested delivery coordinate"
        )
    if (
        fence.run_mode != PHASE9_RUN_MODE
        or fence.modeling_consultation_contract != PHASE9_MODELING_CONTRACT
        or fence.delivery_capability != PHASE9_DELIVERY_CAPABILITY
        or fence.replay_mode not in {"TECHNICAL", "ABLATE_NO_JUDGE"}
    ):
        raise Phase9DeliveryFenceError(
            "current generation is not a valid Phase9 forensic coordinate"
        )
    raise Phase9DeliveryFenceError(
        "Phase9 forensic, technical, and ablation generations are delivery DISABLED"
    )
