"""Operator entry points for explicitly authorized technical continuation."""
import argparse
import json
from pathlib import Path

from .engine import FactoryEngine
from .steps import build_native_registry
from .projections import write_compatibility_projections, runtime_payload
from .technical_continuation import authorize, execute


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["authorize-gate2", "continue-gate2"])
    parser.add_argument("project", type=Path)
    parser.add_argument("--expected-revision", type=int, required=True)
    parser.add_argument("--reason", default="")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    engine = FactoryEngine(args.project, registry=build_native_registry(root),
                           projector=write_compatibility_projections)
    if args.operation == "authorize-gate2":
        state = authorize(engine, expected_revision=args.expected_revision, reason=args.reason)
    else:
        state = execute(engine, expected_revision=args.expected_revision)
    print(json.dumps(runtime_payload(state), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
