from __future__ import annotations

import json
from pathlib import Path
import sys
import time

from factory_core.authority_production_writer import AuthorityProductionWriter
from tests.support.authority_production import bundle


def main() -> int:
    database = Path(sys.argv[1])
    source_fence = sys.argv[2]
    suffix = sys.argv[3]
    barrier = Path(sys.argv[4])
    Path(f"{barrier}.{suffix}.ready").write_text("ready\n", encoding="utf-8")
    deadline = time.monotonic() + 10
    while not Path(f"{barrier}.go").exists():
        if time.monotonic() >= deadline:
            print(json.dumps({"status": "Timeout"}))
            return 2
        time.sleep(0.01)
    writer = AuthorityProductionWriter(
        database,
        writer_id="writer-a",
        writer_epoch=1,
        expected_source_fence_sha256=source_fence,
    )
    command, event, receipt, outbox = bundle(requested_revision=1, suffix=suffix)
    try:
        result = writer.persist_command_bundle(
            workflow_id="legacy_current",
            idempotency_key=f"process-key-{suffix}",
            command=command,
            event=event,
            receipt=receipt,
            outbox=outbox,
            occurred_at=1350,
        )
    except Exception as exc:
        print(json.dumps({"status": type(exc).__name__}, sort_keys=True))
        return 0
    print(
        json.dumps(
            {"status": "committed", "revision": result.revision},
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
