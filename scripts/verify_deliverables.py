#!/usr/bin/env python3
"""verify_deliverables.py — 题目交付物硬门禁（Gate 1 / Step 16 delivery gate）。

检查三类机械可验证的交付完整性问题（对应 CUMCM 2025A 实跑丢分项 B1/B2）：

  1. 附件存在性：`problem/deliverables.json` 声明的 result*.xlsx 等附件必须存在且非空。
  2. 策略表存在性：题目要求"给出投放策略/决策方案"的子问题，论文必须有一张
     包含声明字段（如 航向角/速度/投放点/起爆点）的表格。
  3. Excel↔结果一致性：附件 xlsx 的数值单元格必须可追溯到
     `results/canonical_results.json` / `results/*/values.json`（同一真相源），
     且 canonical 中的 headline 数字（total/objective/总/时长 类 key）必须出现在
     至少一个 xlsx 中 —— 防止"论文 4.90s vs Excel 6.358s"式的双源分叉。

契约文件 `problem/deliverables.json`（Step 0 生成）示例：

  {
    "schema_version": 1,
    "attachments": [
      {"file": "result1.xlsx", "problem": "问题3", "description": "单机三弹投放策略"}
    ],
    "strategy_tables": [
      {"problem": "问题2", "description": "投放策略明细表",
       "fields": ["航向角", "速度", "投放时刻", "起爆时刻", "有效遮蔽时长"]}
    ]
  }

Usage:
    python3 verify_deliverables.py <project_dir> <base_name> [--strict]

Exit code: 0 = PASS（或无契约文件时 SKIP，除非 --strict）；1 = FAIL。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

TOL_REL = 0.005          # 相对容差 0.5%（与 Gate 1 数字核对口径一致）
XLSX_UNMATCHED_MAX = 0.15  # xlsx 数值单元格允许的最大不可追溯比例（派生列如占比%）
HEADLINE_KEY_RE = re.compile(
    r"(total|objective|optimal|profit|时长|总|合计|最优|answer)", re.I
)
# 跳过明显非结果类数字：年份、行号等
SKIP_INT_BELOW = 10


def read_json(path: Path):
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="utf-8", errors="replace")


def numeric_leaves(data, prefix="") -> list[tuple[str, float]]:
    out: list[tuple[str, float]] = []
    if isinstance(data, bool):
        return out
    if isinstance(data, (int, float)):
        out.append((prefix, float(data)))
        return out
    if isinstance(data, dict):
        for k, v in data.items():
            out.extend(numeric_leaves(v, f"{prefix}.{k}" if prefix else str(k)))
    elif isinstance(data, list):
        for i, v in enumerate(data):
            out.extend(numeric_leaves(v, f"{prefix}[{i}]"))
    return out


def collect_truth_numbers(project: Path) -> list[tuple[str, float]]:
    """真相源：canonical_results.json 优先，其次所有 results/**/values.json。"""
    truth: list[tuple[str, float]] = []
    canonical = project / "results" / "canonical_results.json"
    if canonical.is_file():
        try:
            truth.extend(
                (f"canonical:{p}", v) for p, v in numeric_leaves(read_json(canonical))
            )
        except Exception:
            pass
    results = project / "results"
    if results.is_dir():
        for vf in sorted(results.rglob("values.json")):
            try:
                rel = vf.relative_to(project)
                truth.extend((f"{rel}:{p}", v) for p, v in numeric_leaves(read_json(vf)))
            except Exception:
                continue
        # 单文件 result json（如 m1_p1_result.json）也计入真相源
        for rf in sorted(results.glob("*.json")):
            if rf.name == "canonical_results.json":
                continue
            try:
                rel = rf.relative_to(project)
                truth.extend((f"{rel}:{p}", v) for p, v in numeric_leaves(read_json(rf)))
            except Exception:
                continue
    return truth


def decimals_of(x: float) -> int:
    s = f"{x}"
    if "." in s and "e" not in s and "E" not in s:
        return len(s.split(".")[1])
    return 0


def value_matches(x: float, truth_values: list[float]) -> bool:
    """x 与任一真相值在容差内匹配（容差 = 显示精度舍入 ∪ 相对 0.5%）。"""
    round_tol = 0.5 * 10 ** (-decimals_of(x))
    for t in truth_values:
        if abs(x - t) <= max(round_tol, TOL_REL * abs(t)):
            return True
    return False


def xlsx_numeric_cells(path: Path) -> list[float]:
    import openpyxl

    cells: list[float] = []
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    for ws in wb.worksheets:
        for row in ws.iter_rows(values_only=True):
            for v in row:
                if isinstance(v, bool):
                    continue
                if isinstance(v, int) and abs(v) < SKIP_INT_BELOW:
                    continue
                if isinstance(v, (int, float)):
                    cells.append(float(v))
    wb.close()
    return cells


def extract_tabular_blocks(tex: str) -> list[str]:
    blocks = []
    for env in ("tabular", "tabularx", "longtable", "array"):
        for m in re.finditer(
            rf"\\begin\{{{env}\}}.*?\\end\{{{env}\}}", tex, re.S
        ):
            blocks.append(m.group(0))
    return blocks


def normalize_field(field: str) -> str:
    """去掉单位括号和空白：'航向角(°)' -> '航向角'。"""
    return re.sub(r"[（(].*?[)）]", "", field).strip()


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    strict = "--strict" in sys.argv
    if len(args) < 2:
        print(__doc__)
        return 2
    project = Path(args[0]).resolve()
    base = args[1]

    contract_path = project / "problem" / "deliverables.json"
    if not contract_path.is_file():
        print("DELIVERABLES_CONTRACT=MISSING")
        print("VERDICT: SKIP (no problem/deliverables.json — legacy project)")
        return 1 if strict else 0
    try:
        contract = read_json(contract_path)
    except Exception as exc:
        print(f"DELIVERABLES_CONTRACT=PARSE_ERROR {exc}")
        print("VERDICT: FAIL")
        return 1
    print("DELIVERABLES_CONTRACT=OK")

    failures: list[str] = []
    warnings: list[str] = []

    # ── 1. 附件存在性 ────────────────────────────────────────────
    attachments = contract.get("attachments") or []
    missing_files = []
    xlsx_paths: list[Path] = []
    for att in attachments:
        name = att.get("file", "") if isinstance(att, dict) else str(att)
        if not name:
            continue
        candidates = [project / name, project / "results" / name]
        found = next((c for c in candidates if c.is_file() and c.stat().st_size > 0), None)
        if found is None:
            missing_files.append(name)
        elif found.suffix.lower() in (".xlsx", ".xls"):
            xlsx_paths.append(found)
    print(f"ATTACHMENTS_REQUIRED={len(attachments)}")
    print(f"ATTACHMENTS_MISSING={len(missing_files)}" + (f" [{', '.join(missing_files)}]" if missing_files else ""))
    if missing_files:
        failures.append(f"缺少题目要求的附件: {', '.join(missing_files)}")

    # ── 2. 策略表存在性 ──────────────────────────────────────────
    tex_path = project / f"{base}_paper.tex"
    tables_required = contract.get("strategy_tables") or []
    tables_missing = []
    if tables_required:
        if not tex_path.is_file():
            failures.append(f"论文 {tex_path.name} 不存在，无法核对策略表")
            tables_missing = [t.get("problem", "?") for t in tables_required]
        else:
            tex = read_text(tex_path)
            blocks = extract_tabular_blocks(tex)
            for spec in tables_required:
                fields = [normalize_field(f) for f in (spec.get("fields") or []) if normalize_field(f)]
                if not fields:
                    continue
                need = max(1, int(len(fields) * 0.7 + 0.5))  # ≥70% 字段共现于同一张表
                ok = any(
                    sum(1 for f in fields if f in blk) >= need for blk in blocks
                )
                if not ok:
                    tables_missing.append(
                        f"{spec.get('problem', '?')}({spec.get('description', '策略表')})"
                    )
    print(f"TABLES_REQUIRED={len(tables_required)}")
    print(f"TABLES_MISSING={len(tables_missing)}" + (f" [{'; '.join(tables_missing)}]" if tables_missing else ""))
    if tables_missing:
        failures.append(f"论文缺少题目要求的策略/结果明细表: {'; '.join(tables_missing)}")

    # ── 3. Excel ↔ 结果真相源一致性 ─────────────────────────────
    truth = collect_truth_numbers(project)
    truth_values = [v for _, v in truth]
    total_cells = 0
    unmatched_cells: list[float] = []
    if xlsx_paths and truth_values:
        for xp in xlsx_paths:
            try:
                cells = xlsx_numeric_cells(xp)
            except Exception as exc:
                failures.append(f"{xp.name} 无法读取: {exc}")
                continue
            total_cells += len(cells)
            unmatched_cells.extend(c for c in cells if not value_matches(c, truth_values))
        ratio = (len(unmatched_cells) / total_cells) if total_cells else 0.0
        print(f"XLSX_NUMBERS={total_cells}")
        sample = ", ".join(f"{c:g}" for c in unmatched_cells[:8])
        print(f"XLSX_UNMATCHED={len(unmatched_cells)} ratio={ratio:.3f}" + (f" sample=[{sample}]" if unmatched_cells else ""))
        if ratio > XLSX_UNMATCHED_MAX:
            failures.append(
                f"xlsx 数值不可追溯比例 {ratio:.1%} > {XLSX_UNMATCHED_MAX:.0%} —— "
                "附件很可能不是从 canonical 结果派生的（双源分叉）"
            )
        elif unmatched_cells:
            warnings.append(f"{len(unmatched_cells)} 个 xlsx 数值未在 results/ 中找到（可能是派生列，需人工确认）")
    elif xlsx_paths and not truth_values:
        failures.append("存在 xlsx 附件但 results/ 无任何数值真相源")

    # canonical headline 数字必须出现在至少一个 xlsx 中
    headline = [
        (p, v) for p, v in truth
        if p.startswith("canonical:") and HEADLINE_KEY_RE.search(p) and abs(v) > 1e-9
    ]
    headline_missing = []
    if headline and xlsx_paths:
        all_cells: list[float] = []
        for xp in xlsx_paths:
            try:
                all_cells.extend(xlsx_numeric_cells(xp))
            except Exception:
                continue
        for p, v in headline:
            round_tol = 0.5 * 10 ** (-max(decimals_of(v) - 1, 0))
            if not any(abs(c - v) <= max(round_tol, TOL_REL * abs(v)) for c in all_cells):
                headline_missing.append(f"{p}={v:g}")
        print(f"HEADLINE_KEYS={len(headline)}")
        print(f"HEADLINE_MISSING={len(headline_missing)}" + (f" [{'; '.join(headline_missing[:6])}]" if headline_missing else ""))
        if headline_missing:
            failures.append(
                f"canonical headline 数字未出现在任何附件 xlsx 中: {'; '.join(headline_missing[:6])}"
            )

    # ── verdict ─────────────────────────────────────────────────
    for w in warnings:
        print(f"WARNING: {w}")
    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        print("VERDICT: FAIL")
        return 1
    print("VERDICT: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
