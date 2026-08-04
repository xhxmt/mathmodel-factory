#!/usr/bin/env python3
"""Regenerate canonical-derived artifacts in isolation and diff the results."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import subprocess
import sys
import tempfile
from datetime import date, datetime, time
from pathlib import Path
from typing import Any


SCHEMA = "canonical-derived-artifacts-v1"
COMPARISONS = {"bytes", "json_canonical", "xlsx_cells"}


class DerivedArtifactError(ValueError):
    """Raised when a derived-artifact contract is structurally unsafe."""


def canonical_hash(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_relative(root: Path, value: Any, *, must_exist: bool) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise DerivedArtifactError("artifact path must be a nonempty relative string")
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts or "\\" in value:
        raise DerivedArtifactError(f"artifact path must stay inside its root: {value}")
    try:
        candidate = (root / relative).resolve(strict=must_exist)
        candidate.relative_to(root.resolve())
    except (OSError, RuntimeError, ValueError) as exc:
        raise DerivedArtifactError(f"artifact path escapes or is missing: {value}") from exc
    if must_exist and not candidate.is_file():
        raise DerivedArtifactError(f"artifact path is not a regular file: {value}")
    return candidate


def _json_value(path: Path) -> Any:
    def reject_constant(token: str) -> None:
        raise ValueError(f"non-finite JSON constant {token}")

    return json.loads(path.read_text(encoding="utf-8"), parse_constant=reject_constant)


def _xlsx_scalar(value: Any) -> Any:
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        raise DerivedArtifactError("xlsx contains a non-finite numeric cell")
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return repr(value)


def _xlsx_semantics(path: Path) -> dict[str, Any]:
    try:
        import openpyxl
    except ImportError as exc:
        raise DerivedArtifactError("openpyxl is required for xlsx_cells comparison") from exc
    workbook = openpyxl.load_workbook(path, data_only=False, read_only=False)
    try:
        sheets = []
        for sheet in workbook.worksheets:
            cells = []
            for row in sheet.iter_rows():
                for cell in row:
                    if cell.value is None and not cell.has_style:
                        continue
                    cells.append(
                        {
                            "coordinate": cell.coordinate,
                            "value": _xlsx_scalar(cell.value),
                            "data_type": cell.data_type,
                            "number_format": cell.number_format,
                        }
                    )
            sheets.append(
                {
                    "title": sheet.title,
                    "cells": cells,
                    "merged_ranges": sorted(str(value) for value in sheet.merged_cells.ranges),
                }
            )
        return {"sheets": sheets}
    finally:
        workbook.close()


def artifact_digest(path: str | Path, comparison: str) -> str:
    target = Path(path)
    if comparison == "bytes":
        return file_sha256(target)
    if comparison == "json_canonical":
        return canonical_hash(_json_value(target))
    if comparison == "xlsx_cells":
        return canonical_hash(_xlsx_semantics(target))
    raise DerivedArtifactError(f"unsupported artifact comparison: {comparison}")


def _valid_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def verify_manifest(
    project_dir: str | Path,
    manifest: dict[str, Any],
    *,
    timeout_seconds: int = 120,
) -> dict[str, Any]:
    project = Path(project_dir).resolve()
    failures: list[str] = []
    outputs_report: list[dict[str, Any]] = []
    if manifest.get("schema") != SCHEMA:
        raise DerivedArtifactError(f"derived manifest schema must be {SCHEMA}")
    canonical = _safe_relative(project, manifest.get("canonical_results"), must_exist=True)
    expected_canonical_hash = manifest.get("canonical_results_sha256")
    if not _valid_sha256(expected_canonical_hash) or file_sha256(canonical) != expected_canonical_hash:
        failures.append("CANONICAL_HASH_MISMATCH")
    generator_value = manifest.get("generator")
    if not isinstance(generator_value, dict):
        raise DerivedArtifactError("generator must be an object")
    generator = _safe_relative(project, generator_value.get("path"), must_exist=True)
    expected_generator_hash = generator_value.get("sha256")
    if not _valid_sha256(expected_generator_hash) or file_sha256(generator) != expected_generator_hash:
        failures.append("GENERATOR_HASH_MISMATCH")
    argv_template = generator_value.get("argv")
    if not isinstance(argv_template, list) or not argv_template or any(
        not isinstance(item, str) or not item for item in argv_template
    ):
        raise DerivedArtifactError("generator.argv must be a nonempty string array")
    expanded_generator_tokens = {
        str(generator),
        generator.relative_to(project).as_posix(),
    }
    if not any(token in expanded_generator_tokens for token in argv_template):
        raise DerivedArtifactError("generator.argv must invoke the hash-pinned generator path")
    raw_outputs = manifest.get("outputs")
    if not isinstance(raw_outputs, list) or not raw_outputs:
        raise DerivedArtifactError("outputs must be a nonempty array")
    outputs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw_outputs):
        if not isinstance(item, dict):
            raise DerivedArtifactError(f"outputs[{index}] must be an object")
        relative = item.get("path")
        actual = _safe_relative(project, relative, must_exist=True)
        relative_text = actual.relative_to(project).as_posix()
        if relative_text in seen:
            raise DerivedArtifactError(f"duplicate derived output: {relative_text}")
        seen.add(relative_text)
        comparison = item.get("comparison")
        if comparison not in COMPARISONS:
            raise DerivedArtifactError(f"outputs[{index}].comparison is invalid")
        expected = item.get("sha256")
        if not _valid_sha256(expected):
            raise DerivedArtifactError(f"outputs[{index}].sha256 must be lowercase SHA-256")
        actual_digest = artifact_digest(actual, comparison)
        if actual_digest != expected:
            failures.append("ACTUAL_DIGEST_MISMATCH")
        outputs.append(
            {
                "path": relative_text,
                "comparison": comparison,
                "declared_sha256": expected,
                "actual_sha256": actual_digest,
                "actual_matches_manifest": actual_digest == expected,
            }
        )

    generated_files: set[str] = set()
    undeclared: list[str] = []
    command: list[str] = []
    returncode: int | None = None
    stdout = ""
    stderr = ""
    if not failures:
        with tempfile.TemporaryDirectory(prefix="paper-factory-derived-") as temp_value:
            output_root = Path(temp_value).resolve()
            replacements = {
                "__PYTHON__": sys.executable,
                "__CANONICAL_RESULTS__": str(canonical),
                "__OUTPUT_DIR__": str(output_root),
                generator.relative_to(project).as_posix(): str(generator),
            }
            command = [replacements.get(token, token) for token in argv_template]
            environment = {
                **os.environ,
                "PYTHONHASHSEED": "0",
                "TZ": "UTC",
                "LC_ALL": "C.UTF-8",
            }
            try:
                completed = subprocess.run(
                    command,
                    cwd=project,
                    env=environment,
                    capture_output=True,
                    text=True,
                    timeout=timeout_seconds,
                    check=False,
                )
                returncode = completed.returncode
                stdout = completed.stdout
                stderr = completed.stderr
            except (OSError, subprocess.TimeoutExpired) as exc:
                returncode = 124
                stderr = str(exc)
            if returncode != 0:
                failures.append("GENERATOR_FAILED")
            generated_files = {
                path.relative_to(output_root).as_posix()
                for path in output_root.rglob("*")
                if path.is_file()
            }
            undeclared = sorted(generated_files - seen)
            if undeclared:
                failures.append("UNDECLARED_GENERATED_OUTPUT")
            missing = sorted(seen - generated_files)
            if missing:
                failures.append("MISSING_GENERATED_OUTPUT")
            for item in outputs:
                generated = output_root / item["path"]
                if not generated.is_file():
                    item["regenerated_sha256"] = None
                    item["matches_regenerated"] = False
                    continue
                digest = artifact_digest(generated, item["comparison"])
                item["regenerated_sha256"] = digest
                item["matches_regenerated"] = digest == item["actual_sha256"]
                if not item["matches_regenerated"]:
                    failures.append("REGENERATED_DIFF_MISMATCH")

    return {
        "schema": "canonical-derived-verification-v1",
        "manifest_sha256": canonical_hash(manifest),
        "canonical_results_sha256": file_sha256(canonical),
        "generator_sha256": file_sha256(generator),
        "generator_command": command,
        "generator_returncode": returncode,
        "generator_stdout": stdout,
        "generator_stderr": stderr,
        "outputs": outputs,
        "generated_outputs": sorted(generated_files),
        "undeclared_generated_outputs": undeclared,
        "failures": sorted(set(failures)),
        "passed": not failures,
        "claim_limit": "DETERMINISTIC_DERIVATION_FROM_PINNED_CANONICAL_RESULTS_ONLY",
    }


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DerivedArtifactError(f"invalid JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise DerivedArtifactError(f"JSON root must be an object: {path}")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project_dir", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args(argv)
    project = args.project_dir.resolve()
    try:
        manifest_path = args.manifest
        if manifest_path is None:
            contract_path = project / "quality_contract.json"
            if not contract_path.is_file():
                print("VERDICT: SKIP (no quality_contract.json)")
                return 0
            contract = _read_json(contract_path)
            derived = contract.get("derived_artifacts")
            if not isinstance(derived, dict):
                print("VERDICT: SKIP (quality contract has no derived-artifact declaration)")
                return 0
            manifest_path = _safe_relative(project, derived.get("manifest"), must_exist=True)
        elif not manifest_path.is_absolute():
            manifest_path = _safe_relative(project, str(manifest_path), must_exist=True)
        report = verify_manifest(project, _read_json(manifest_path.resolve()))
    except (DerivedArtifactError, OSError, ValueError) as exc:
        report = {
            "schema": "canonical-derived-verification-v1",
            "passed": False,
            "failures": ["INVALID_DERIVED_ARTIFACT_CONTRACT"],
            "error": str(exc),
        }
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    for failure in report.get("failures", []):
        print(f"FAIL: {failure}")
    print(f"VERDICT: {'PASS' if report.get('passed') else 'FAIL'}")
    return 0 if report.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
