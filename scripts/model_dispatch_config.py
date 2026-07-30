#!/usr/bin/env python3
"""Read model registry and per-step model assignment config."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

US = "\x1f"


def read_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def normalize_assignment(value: Any) -> tuple[str, str] | None:
    if isinstance(value, str) and value.strip():
        return value.strip(), ""
    if isinstance(value, dict):
        primary = str(value.get("primary") or "").strip()
        fallback = str(value.get("fallback") or "").strip()
        if primary:
            return primary, fallback
    return None


def get_step_model_ids(config_file: Path, base: str, step: str | int) -> tuple[str, str] | None:
    if not config_file.is_file():
        return None
    try:
        cfg = read_json(config_file)
    except Exception:
        return None
    key = f"step_{step}"

    def get(scope: str) -> tuple[str, str] | None:
        scoped = cfg.get(scope) if isinstance(cfg, dict) else None
        if not isinstance(scoped, dict):
            return None
        return normalize_assignment(scoped.get(key))

    return get(base) or get("_default")


def get_model_entry(registry_file: Path, model_id: str) -> dict[str, str] | None:
    if not model_id or not registry_file.is_file():
        return None
    try:
        registry = read_json(registry_file)
    except Exception:
        return None
    models = registry.get("models", []) if isinstance(registry, dict) else registry
    if not isinstance(models, list):
        return None
    for model in models:
        if not isinstance(model, dict) or model.get("id") != model_id:
            continue
        if model.get("enabled") is False:
            return None
        return {
            "backend": str(model.get("backend") or ""),
            "model": str(model.get("model") or ""),
            "effort": str(model.get("effort") or ""),
            "base_url": str(model.get("base_url") or ""),
            "key_env": str(model.get("key_env") or ""),
        }
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("step-ids")
    p.add_argument("config_file")
    p.add_argument("base")
    p.add_argument("step")

    p = sub.add_parser("entry")
    p.add_argument("registry_file")
    p.add_argument("model_id")

    args = parser.parse_args()
    if args.command == "step-ids":
        ids = get_step_model_ids(Path(args.config_file), args.base, args.step)
        if not ids:
            return 1
        print(f"{ids[0]}|{ids[1]}")
        return 0
    if args.command == "entry":
        entry = get_model_entry(Path(args.registry_file), args.model_id)
        if not entry:
            return 1
        print(US.join([entry["backend"], entry["model"], entry["effort"], entry["base_url"], entry["key_env"]]))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
