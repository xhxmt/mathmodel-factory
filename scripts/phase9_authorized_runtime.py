#!/usr/bin/env python3
"""Default-off Phase9-A dispatch planning, execution and receipt collection."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from factory_core.canonical import canonical_bytes
from factory_core.phase9_config import load_phase9_settings
from factory_core.phase9_forensic_replay import Phase9ForensicReplayService, read_phase9_forensic_replay_request, _strict_json, _regular_file_bytes
from factory_core.phase9_runtime_authority import Phase9RuntimeAuthority
from factory_core.phase9_runtime_coordinator import plan_runtime, execute_runtime, write_finalizer_controls


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("plan", "execute", "collect", "export", "finalize"))
    parser.add_argument("--project", type=Path)
    parser.add_argument("--records", type=Path)
    parser.add_argument("--request", type=Path)
    parser.add_argument("--entry", type=Path)
    parser.add_argument("--target", type=Path)
    parser.add_argument("--grant", type=Path)
    parser.add_argument("--runtime-id")
    parser.add_argument("--model", default="gpt-6-astra")
    parser.add_argument("--effort", default="medium")
    parser.add_argument("--timeout-seconds", type=int, default=900)
    parser.add_argument("--total-timeout-seconds", type=int, default=3600)
    args = parser.parse_args(argv)
    try:
        settings = load_phase9_settings()
        if not settings.enabled:
            print('{"status":"BLOCKED","reason":"PHASE9_DISABLED","model_dispatch_count":0,"delivery_capability":"DISABLED"}')
            return 2
        service = Phase9ForensicReplayService(
            settings.authority_database, expected_source_fence_sha256=settings.authority_source_fence_sha256,
            source_repository=settings.source_repository, evidence_root=settings.evidence_root,
            official_input_root=settings.official_input_root, execution_context_receipt_path=settings.execution_context_receipt_path,
        )
        authority = Phase9RuntimeAuthority(service)
        if args.command == "collect":
            if not args.runtime_id:
                parser.error("collect requires --runtime-id")
            result = authority.collect(args.runtime_id)
        else:
            if args.request is None or args.records is None:
                parser.error("this command requires --request and --records")
            request = read_phase9_forensic_replay_request(args.request)

            def read(path, label):
                if path is None:
                    parser.error(f"{label} path is required")
                return _strict_json(_regular_file_bytes(path, maximum=4 * 1024 * 1024, label=label), label)

            if args.command == "finalize":
                from factory_core.phase9_replay_evidence import produce_formal_phase9_replay_evidence, _write_new
                produced = produce_formal_phase9_replay_evidence(
                    database=authority.database, expected_source_fence_sha256=service.expected_source_fence_sha256,
                    source_repository=settings.source_repository, evidence_root=settings.evidence_root,
                    official_input_root=settings.official_input_root, execution_context_receipt_path=settings.execution_context_receipt_path,
                    python_executable=sys.executable, request=request, execution_root=args.records,
                )
                _write_new(args.records / "produced_request.json", canonical_bytes(produced.as_dict()))
                result = {"forensic_terminal": service.execute(produced).as_dict(),
                          "independent_formal_review": "PENDING", "formal_phase9_completed": False,
                          "delivery_capability": "DISABLED"}
            elif args.command == "plan":
                result = plan_runtime(source=settings.source_repository, project=args.project or authority.project_root,
                                      records=args.records, request=request, model=args.model, effort=args.effort,
                                      timeout_seconds=args.timeout_seconds, total_timeout_seconds=args.total_timeout_seconds)
            elif args.command == "execute":
                grant = read(args.grant, "grant")
                metadata = args.grant.stat()
                if metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077:
                    raise ValueError("dispatch grant must be owned by the controlled account and private (0600)")
                result = execute_runtime(source=settings.source_repository, project=args.project or authority.project_root,
                                         records=args.records, request=request, entry=read(args.entry, "entry"),
                                         target=read(args.target, "target"), grant=grant, authority=authority)
            else:
                if not args.runtime_id:
                    parser.error("export requires --runtime-id")
                selected = {}
                for path in sorted((args.records / "calls").glob("*/dispatch_intent.json")):
                    intent = read(path, "dispatch intent")
                    if intent["runtime_id"] != args.runtime_id:
                        raise ValueError("records include a different runtime")
                    old = selected.get(intent["role"])
                    if old is None or intent["role_attempt"] > old[0]:
                        output = path.parent / "output.raw"
                        if not output.exists() or output.stat().st_size == 0:
                            output = path.parent / "final_response.raw"
                        selected[intent["role"]] = (intent["role_attempt"], output)
                outputs = {role: _regular_file_bytes(value[1], maximum=64 * 1024 * 1024, label=role) for role, value in selected.items()}
                result = authority.export_runtime_receipts(args.runtime_id, request, read(args.entry, "entry"), outputs, settings.evidence_root)
                result["verdict"] = write_finalizer_controls(
                    authority=authority, runtime_id=args.runtime_id, request=request, entry=read(args.entry, "entry"),
                    project=args.project or authority.project_root, evidence_root=settings.evidence_root, outputs=outputs,
                )
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        if args.command == "execute":
            return 0 if result["runtime"]["status"] == "COMPLETED" and result["component_result"]["returncode"] == 0 else 2
        return 0
    except Exception as exc:
        print(json.dumps({"status": "BLOCKED", "error_type": type(exc).__name__, "detail": str(exc),
                          "formal_phase9_completed": False, "delivery_capability": "DISABLED"}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
