#!/usr/bin/env python3
"""Execute a project's declarative quality contract."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.quality_contract import ContractResult, evaluate_contract, load_contract


def render_text(result: ContractResult, contract_path: Path) -> str:
    lines = ["=== Project Quality Contract ===", f"Contract: {contract_path}", ""]
    for evidence in result.evidence_results:
        state = "PASS" if evidence.returncode == 0 else "FAIL"
        lines.append(
            f"[{state}] {evidence.claim_id} {evidence.evidence_type}: "
            f"{' '.join(evidence.argv)}"
        )
    for warning in result.warnings:
        lines.append(f"[WARN] {warning.item_id} {warning.code}: {warning.message}")
    for failure in result.failures:
        lines.append(f"[FAIL] {failure.item_id} {failure.code}: {failure.message}")
    lines.extend(
        [
            "",
            f"QUALITY_CONTRACT_FAILURES={len(result.failures)}",
            f"QUALITY_CONTRACT_WARNINGS={len(result.warnings)}",
            f"VERDICT: {'PASS' if result.passed else 'FAIL'}",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("project_dir", type=Path)
    parser.add_argument("--factory-root", type=Path)
    parser.add_argument("--json-out", type=Path)
    parser.add_argument("--text-out", type=Path)
    args = parser.parse_args()

    project = args.project_dir.resolve()
    contract_path = project / "quality_contract.json"
    if not contract_path.is_file():
        text = "VERDICT: SKIP (quality_contract.json missing)\n"
        if args.text_out:
            args.text_out.write_text(text, encoding="utf-8")
        print(text, end="")
        return 2

    try:
        contract = load_contract(contract_path)
        result = evaluate_contract(
            contract,
            project,
            factory_root=args.factory_root,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        result = ContractResult(passed=False)
        from scripts.quality_contract import Finding

        result.failures.append(Finding("INVALID_CONTRACT", str(exc)))

    text = render_text(result, contract_path)
    if args.text_out:
        args.text_out.parent.mkdir(parents=True, exist_ok=True)
        args.text_out.write_text(text, encoding="utf-8")
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(
            json.dumps(result.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    print(text, end="")
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
