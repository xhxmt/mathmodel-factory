#!/usr/bin/env python3
"""Extract a compact, content-free run-history fixture from a project state DB.

The fixture feeds ``scripts/replay_run_history.py``: it keeps what a workflow
*decided* (events, classified change sets, solver job owners) and drops what a
run *produced* (paper text, logs, prompts, hashes of the evidence chain).

The database is opened read-only and immutable, so a live or historical
project is never modified, migrated or checkpointed by this script.

Usage:
    python3 scripts/extract_run_history.py --out tests/fixtures/run_history \\
        <project_dir> [<project_dir> ...]
"""

from __future__ import annotations

import argparse
import contextlib
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any, Iterator

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

FIXTURE_SCHEMA = "factory-run-history-fixture-v1"

# Payload keys that describe a decision.  Everything else (hash chains, prompt
# receipts, evidence digests) is evidence about a run, not a decision, and is
# deliberately dropped.
_KEPT_PAYLOAD_KEYS = (
    "error_class",
    "reason",
    "resume_after_step",
    "semantic_owner_stage",
    "dirty_owner_stages",
    "dirty_flags",
    "audit_status",
    "audit_profile",
    "failed_check",
    "backend",
    "model_id",
    "returncode",
    "duration_seconds",
    "delay_seconds",
    "missing_artifacts",
    "job_id",
    "decision",
)
# Event types that only bind evidence (receipts, prompt inputs) and never carry
# a workflow decision.  Revisions keep their original numbers, so gaps are
# expected in the fixture.
_DROPPED_EVENT_TYPES = frozenset(
    {"SOLVER_JOB_RECEIPT_SUBMITTED", "SOLVER_JOB_RECEIPT_COMPLETED", "PROMPT_INPUT_BOUND"}
)
_MAX_TEXT = 300
_HOME = str(Path.home())
# Home directories of any other account (another operator, root on a server).
# The whole first component is replaced, whatever its characters (accents,
# non-ASCII names); it ends at a path separator, whitespace or a quote.
_OTHER_HOMES = re.compile(r"(?:/home|/Users)/[^/\s\"'`<>]+|/root(?=/|\b)")
# Credential-shaped tokens.  The fixture is committed, so anything that looks
# like a key is replaced even if it is probably harmless.
_SECRET_PATTERNS = (
    re.compile(r"sk[-_][A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)(api[_-]?key|token|secret|password)\s*[=:]\s*\S+"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{8,}"),
)


def sanitize_text(value: Any) -> str:
    text = str(value)
    text = text.replace(_HOME, "~")
    text = _OTHER_HOMES.sub("~", text)
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("<redacted>", text)
    if len(text) > _MAX_TEXT:
        text = text[:_MAX_TEXT] + "…"
    return text


def _short_fingerprint(value: Any) -> str:
    """Keep equality and MISSING-ness of a fingerprint, not the hash itself."""

    text = str(value)
    return "MISSING" if text == "MISSING" else text[:12]


def _clean(value: Any) -> Any:
    if isinstance(value, str):
        return sanitize_text(value)
    if isinstance(value, list):
        return [_clean(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _clean(item) for key, item in value.items()}
    return value


def _stage_of(payload: dict[str, Any], workflow: dict[str, Any]) -> int | None:
    for candidate in (
        payload.get("stage"),
        workflow.get("stage_id"),
        workflow.get("result_stage_id"),
    ):
        if candidate is not None:
            try:
                return int(candidate)
            except (TypeError, ValueError):
                continue
    return None


@contextlib.contextmanager
def _open_readonly(database: Path) -> Iterator[sqlite3.Connection]:
    """Read a private main/WAL copy of a project database.

    ``immutable=1`` would make SQLite ignore a live ``-wal`` sidecar and read
    stale or incomplete state.  The engine's own snapshot reader copies the
    main file and WAL into a private directory under the project's commit
    lease, so committed WAL frames are seen and nothing beside the source
    database is created or modified.
    """

    from factory_core.state_lease import isolated_state_snapshot_ro, state_commit_lease

    if not database.is_file():
        raise FileNotFoundError(f"no state database at {database}")
    with state_commit_lease(database.parent.parent):
        with isolated_state_snapshot_ro(database) as connection:
            connection.row_factory = None
            yield connection


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in connection.execute("select name from sqlite_master where type='table'")
    }


def extract(project_dir: Path) -> dict[str, Any]:
    with _open_readonly(project_dir / ".factory" / "state.db") as connection:
        tables = _tables(connection)
        columns = [row[1] for row in connection.execute("pragma table_info(project_state)")]
        state = dict(zip(columns, connection.execute("select * from project_state").fetchone()))

        events: list[dict[str, Any]] = []
        for revision, event_type, created_at, step, attempt, payload_json in connection.execute(
            "select revision, type, created_at, step, attempt, payload_json "
            "from events order by revision"
        ):
            if event_type in _DROPPED_EVENT_TYPES:
                continue
            payload = json.loads(payload_json or "{}")
            workflow = payload.pop("_workflow", {}) or {}
            record: dict[str, Any] = {
                "rev": int(revision),
                "type": str(event_type),
                "ts": float(created_at),
            }
            if step not in (None, ""):
                record["step"] = int(step)
            if attempt not in (None, ""):
                record["attempt"] = int(attempt)
            stage = _stage_of(payload, workflow)
            if stage is not None:
                record["stage"] = stage
            if payload.get("subtask") is not None:
                record["subtask"] = str(payload["subtask"])
            if workflow.get("canonical_type"):
                record["canonical_type"] = str(workflow["canonical_type"])
            for key in _KEPT_PAYLOAD_KEYS:
                if payload.get(key) is not None:
                    record[key] = _clean(payload[key])
            events.append(record)

        change_sets: dict[int, list[dict[str, Any]]] = {}
        # The classifier identity each change set was recorded under; replay
        # fidelity can only be asserted where it equals the current one.
        classifiers: dict[int, set[str]] = {}
        if "dirty_causes" in tables:
            for cause_revision, owner_stage, flag, artifact, baseline, current, contract in connection.execute(
                "select cause_revision, owner_stage, flag, cause_artifact, "
                "baseline_fingerprint, current_fingerprint, classifier_contract_sha256 "
                "from dirty_causes order by cause_revision, cause_artifact, flag"
            ):
                classifiers.setdefault(int(cause_revision), set()).add(_short_fingerprint(contract))
                change_sets.setdefault(int(cause_revision), []).append(
                    {
                        "owner_stage": int(owner_stage),
                        "flag": str(flag),
                        "artifact": sanitize_text(artifact),
                        "baseline": _short_fingerprint(baseline),
                        "current": _short_fingerprint(current),
                    }
                )

        solver_owners: dict[str, int] = {}
        if "solver_jobs" in tables:
            for job_id, owner_stage in connection.execute(
                "select job_id, owner_stage from solver_jobs where owner_stage is not null"
            ):
                solver_owners[str(job_id)] = int(owner_stage)

    return {
        "schema": FIXTURE_SCHEMA,
        "run": project_dir.name,
        "final_state": {
            "status": state.get("status"),
            "revision": state.get("revision"),
            "last_completed_step": state.get("last_completed_step"),
        },
        "events": events,
        "change_sets": [
            {
                "rev": revision,
                "classifier": sorted(classifiers[revision]),
                "changes": rows,
            }
            for revision, rows in sorted(change_sets.items())
        ],
        "solver_job_owners": dict(sorted(solver_owners.items())),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", required=True, type=Path, help="fixture directory")
    parser.add_argument("projects", nargs="+", type=Path, help="project directories")
    args = parser.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    for project in args.projects:
        fixture = extract(project)
        target = args.out / f"{fixture['run']}.json"
        target.write_text(
            json.dumps(fixture, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(
            f"{fixture['run']}: {len(fixture['events'])} events, "
            f"{len(fixture['change_sets'])} change sets -> {target}",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
