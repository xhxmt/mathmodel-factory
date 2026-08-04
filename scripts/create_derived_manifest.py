#!/usr/bin/env python3
"""Create a hash-pinned manifest for deterministic canonical-result derivatives."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.verify_derived_artifacts import COMPARISONS, SCHEMA, artifact_digest, file_sha256


def _relative_file(project: Path, value: str | Path) -> tuple[Path, str]:
    raw = Path(value)
    candidate = raw if raw.is_absolute() else project / raw
    resolved = candidate.resolve(strict=True)
    try:
        relative = resolved.relative_to(project).as_posix()
    except ValueError as exc:
        raise ValueError(f"path must stay inside the project: {value}") from exc
    if not resolved.is_file():
        raise ValueError(f"path must be a regular file: {value}")
    return resolved, relative


def build_manifest(
    project_dir: str | Path,
    *,
    canonical_results: str | Path,
    generator: str | Path,
    outputs: list[tuple[str | Path, str]],
) -> dict:
    project = Path(project_dir).resolve()
    canonical, canonical_relative = _relative_file(project, canonical_results)
    generator_path, generator_relative = _relative_file(project, generator)
    if not outputs:
        raise ValueError("at least one derived output is required")
    records = []
    seen: set[str] = set()
    for raw_path, comparison in outputs:
        if comparison not in COMPARISONS:
            raise ValueError(f"unsupported comparison mode: {comparison}")
        output, relative = _relative_file(project, raw_path)
        if relative in seen:
            raise ValueError(f"duplicate derived output: {relative}")
        seen.add(relative)
        records.append(
            {
                "path": relative,
                "comparison": comparison,
                "sha256": artifact_digest(output, comparison),
            }
        )
    return {
        "schema": SCHEMA,
        "canonical_results": canonical_relative,
        "canonical_results_sha256": file_sha256(canonical),
        "generator": {
            "path": generator_relative,
            "sha256": file_sha256(generator_path),
            "argv": [
                "__PYTHON__",
                generator_relative,
                "--canonical",
                "__CANONICAL_RESULTS__",
                "--output-dir",
                "__OUTPUT_DIR__",
            ],
        },
        "outputs": records,
    }


def _output_spec(value: str) -> tuple[str, str]:
    path, separator, comparison = value.rpartition("=")
    if not separator or not path or comparison not in COMPARISONS:
        choices = ", ".join(sorted(COMPARISONS))
        raise argparse.ArgumentTypeError(
            f"output must be PATH=COMPARISON where COMPARISON is one of: {choices}"
        )
    return path, comparison


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project_dir", type=Path)
    parser.add_argument(
        "--canonical", default="results/canonical_results.json"
    )
    parser.add_argument("--generator", default="models/generate_derived.py")
    parser.add_argument("--output", action="append", type=_output_spec, required=True)
    parser.add_argument("--manifest", default="results/derived_artifacts.json")
    args = parser.parse_args(argv)
    project = args.project_dir.resolve()
    try:
        manifest = build_manifest(
            project,
            canonical_results=args.canonical,
            generator=args.generator,
            outputs=args.output,
        )
        target = Path(args.manifest)
        if not target.is_absolute():
            target = project / target
        resolved_target = target.resolve(strict=False)
        resolved_target.relative_to(project)
        resolved_target.parent.mkdir(parents=True, exist_ok=True)
        resolved_target.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except (OSError, ValueError) as exc:
        print(f"cannot create derived manifest: {exc}", file=sys.stderr)
        return 2
    print(resolved_target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
