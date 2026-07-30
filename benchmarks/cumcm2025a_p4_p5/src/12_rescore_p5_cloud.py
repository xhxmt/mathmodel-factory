#!/usr/bin/env python3
"""Rescore a Cloud Run P5 candidate in the audited local numerical stack."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path


HERE = Path(__file__).resolve().parent


def load_repaired():
    path = HERE / "10_repaired_p4_p5.py"
    spec = importlib.util.spec_from_file_location("m3_repaired_rescore", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    repaired = load_repaired()
    instance = repaired.m3_data.build_instance("full_template", 36)
    payload = json.loads(args.input.read_text(encoding="utf-8"))
    for label in ("best_total", "best_fairness"):
        records = repaired.records_from_decision_items(payload[label]["decision"])
        repaired.verify_shared_flight(records)
        local_rescore = {}
        for dt in (0.05, 0.02):
            evaluation = repaired.m3_full.evaluate_records(
                instance,
                repaired.global_rescore_records(records),
                list(repaired.MISSILE_IDS),
                dt,
            )
            local_rescore[f"dt_{dt:.2f}"] = evaluation
        payload[label]["local_rescore"] = local_rescore
        payload[label]["constraint_check"] = {
            "shared_flight": True,
            "minimum_drop_gap": min(
                right - left
                for uav_id in repaired.UAV_IDS
                for left, right in zip(
                    sorted(
                        record.plan.drop_time
                        for record in records
                        if record.uav_id == uav_id
                    ),
                    sorted(
                        record.plan.drop_time
                        for record in records
                        if record.uav_id == uav_id
                    )[1:],
                )
            ),
        }
    payload["local_rescore_environment"] = {
        "numpy": repaired.np.__version__,
        "scipy": repaired.m3_full.scipy.__version__,
        "target_points": len(instance["target_points"]),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                label: payload[label]["local_rescore"]["dt_0.02"]["T_i"]
                for label in ("best_total", "best_fairness")
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
