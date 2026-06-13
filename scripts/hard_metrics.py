#!/usr/bin/env python3
"""免评委硬指标层：聚合程序可判信号成跨项目体检表。

Usage:
    python3 scripts/hard_metrics.py <project_dir> <base_name>   # 单项目明细
    python3 scripts/hard_metrics.py --batch <dir> [--json]      # 跨项目对比表
"""
import os
import re
import sys
import glob
import json
import zipfile
import subprocess

from verify_symbols import collect_symbol_metrics
from verify_numbers import collect_number_metrics

_CITE_RE = re.compile(r'\\(?:cite|citep|citet|citealp|citeyear)\*?(?:\[[^\]]*\])*\{([^}]*)\}')
_BIB_KEY_RE = re.compile(r'^@(?P<type>[a-zA-Z]+)\s*\{\s*(?P<key>[^,\s]+)\s*,', re.MULTILINE)


def _read(path):
    with open(path, 'r', encoding='utf-8', errors='replace') as f:
        return f.read()


def collect_citation_metrics(tex_path, bib_path):
    """正文 \\cite 键 vs .bib 条目键，报悬空 / 未引用 / 占位符残留。"""
    if not os.path.exists(tex_path):
        return None
    tex = _read(tex_path)
    cited = []
    for grp in _CITE_RE.findall(tex):
        for key in grp.split(','):
            key = key.strip()
            if key:
                cited.append(key)
    cited_set = set(cited)
    bib_keys = set()
    if os.path.exists(bib_path):
        bib_keys = {m.group('key') for m in _BIB_KEY_RE.finditer(_read(bib_path))}
    dangling = sorted(cited_set - bib_keys)
    uncited = sorted(bib_keys - cited_set)
    return {
        "cited_keys": len(cited_set),
        "bib_entries": len(bib_keys),
        "dangling_cites": len(dangling),
        "dangling_cite_keys": dangling,
        "uncited_entries": len(uncited),
        "abstract_placeholder_residue": tex.count("ABSTRACT_PLACEHOLDER"),
    }


def collect_assumption_metrics(ledger_path):
    """统计 assumption_ledger.md 表格行的总数 / PROTECTED / CRITICAL。"""
    if not os.path.exists(ledger_path):
        return None
    total = protected = critical = 0
    for line in _read(ledger_path).splitlines():
        s = line.strip()
        if not s.startswith('|'):
            continue
        cells = [c.strip() for c in s.strip('|').split('|')]
        if len(cells) < 6:
            continue
        first = cells[0].lower()
        if first in ('id', '') or set(cells[0]) <= set('-: '):
            continue
        total += 1
        tags = cells[-1].upper()
        if 'PROTECTED' in tags:
            protected += 1
        if 'CRITICAL' in tags:
            critical += 1
    return {"assumptions_total": total, "protected": protected, "critical": critical}


def collect_code_metrics(project_dir):
    """统计 models/ 下 *.py 的文件数 / 总行数 / 均值（碎片化度）。"""
    py_files = glob.glob(os.path.join(project_dir, "models", "**", "*.py"), recursive=True)
    total_lines = 0
    for p in py_files:
        try:
            with open(p, 'r', encoding='utf-8', errors='replace') as f:
                total_lines += sum(1 for _ in f)
        except OSError:
            continue
    n = len(py_files)
    return {
        "code_files": n,
        "code_lines": total_lines,
        "code_mean_lines": round(total_lines / n, 1) if n else None,
    }


def _pdf_pages(pdf_path):
    try:
        out = subprocess.run(["pdfinfo", pdf_path], capture_output=True, text=True, timeout=20)
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    for line in out.stdout.splitlines():
        if line.startswith("Pages:"):
            try:
                return int(line.split(":", 1)[1].strip())
            except ValueError:
                return None
    return None


def collect_artifact_metrics(project_dir, base_name):
    """检查编译产物 PDF 与提交包 zip 是否存在/有效；附 PDF 页数（仅 reference）。"""
    pdf_candidates = [
        os.path.join(project_dir, f"{base_name}_paper.pdf"),
        os.path.join(project_dir, "papers", f"{base_name}_paper.pdf"),
    ]
    pdf_path = next((p for p in pdf_candidates if os.path.exists(p) and os.path.getsize(p) > 0), None)
    zip_path = os.path.join(project_dir, f"{base_name}_submission.zip")
    zip_ok = False
    if os.path.exists(zip_path):
        try:
            with zipfile.ZipFile(zip_path) as z:
                zip_ok = z.testzip() is None and len(z.namelist()) > 0
        except zipfile.BadZipFile:
            zip_ok = False
    return {
        "pdf_ok": pdf_path is not None,
        "zip_ok": zip_ok,
        "pdf_pages": _pdf_pages(pdf_path) if pdf_path else None,
    }


def _safe(fn, *args, label=""):
    try:
        return fn(*args) or {}
    except Exception as e:  # noqa: BLE001 — 批量跑必须容错
        print(f"[hard_metrics] WARN {label}: {e}", file=sys.stderr)
        return {}


def collect_all(project_dir, base_name):
    """聚合所有 collect_* 成一行扁平 dict（去明细键）+ 派生指标。"""
    row = {"project": base_name}
    cite = _safe(collect_citation_metrics,
                 os.path.join(project_dir, f"{base_name}_paper.tex"),
                 os.path.join(project_dir, "references.bib"), label="citation")
    assum = _safe(collect_assumption_metrics,
                  os.path.join(project_dir, "assumption_ledger.md"), label="assumption")
    code = _safe(collect_code_metrics, project_dir, label="code")
    art = _safe(collect_artifact_metrics, project_dir, base_name, label="artifact")
    sym = _safe(collect_symbol_metrics, project_dir, base_name, label="symbol")
    num = _safe(collect_number_metrics, project_dir, base_name, label="number")
    for d in (cite, assum, code, art, sym, num):
        for k, v in d.items():
            if not k.startswith("_") and k != "project":
                row[k] = v
    used = row.get("symbols_used")
    undef = row.get("symbols_undefined")
    row["symbol_coverage"] = round(1 - undef / used, 3) if used else None
    return row


_COLUMNS = [
    ("project", "项目"),
    ("dangling_cites", "悬空引用"),
    ("uncited_entries", "未引用"),
    ("abstract_placeholder_residue", "占位符残留"),
    ("symbols_undefined", "符号未定义"),
    ("symbol_coverage", "符号覆盖"),
    ("numbers_unmatched", "数字无源"),
    ("assumptions_total", "假设数"),
    ("protected", "PROTECTED"),
    ("critical", "CRITICAL"),
    ("code_files", "代码文件"),
    ("code_mean_lines", "均行/文件"),
    ("pdf_ok", "PDF"),
    ("zip_ok", "提交包"),
    ("pdf_pages", "页数*"),
]


def _fmt(v):
    if v is None:
        return "—"
    if v is True:
        return "✓"
    if v is False:
        return "✗"
    return str(v)


def render_markdown(rows):
    keys = [k for k, _ in _COLUMNS]
    heads = [f"{label} ({k})" for k, label in _COLUMNS]
    lines = ["| " + " | ".join(heads) + " |",
             "|" + "|".join(["---"] * len(keys)) + "|"]
    for r in rows:
        lines.append("| " + " | ".join(_fmt(r.get(k)) for k in keys) + " |")
    out = "\n".join(lines)
    return out + "\n\n> 页数* 仅作 reference，非质量分（页数受代码附录主导）。\n"


def _discover_projects(parent):
    found = []
    for name in sorted(os.listdir(parent)):
        pdir = os.path.join(parent, name)
        if os.path.isdir(pdir) and os.path.exists(os.path.join(pdir, f"{name}_paper.tex")):
            found.append((pdir, name))
        elif os.path.isdir(pdir):
            print(f"[hard_metrics] skip {name}: no {name}_paper.tex", file=sys.stderr)
    return found


def main():
    args = sys.argv[1:]
    as_json = "--json" in args
    args = [a for a in args if a != "--json"]
    if len(args) >= 2 and args[0] == "--batch":
        rows = [collect_all(pdir, base) for pdir, base in _discover_projects(args[1])]
    elif len(args) >= 2 and not args[0].startswith("--"):
        rows = [collect_all(args[0], args[1])]
    else:
        print("Usage: hard_metrics.py <project_dir> <base_name>", file=sys.stderr)
        print("   or: hard_metrics.py --batch <dir> [--json]", file=sys.stderr)
        sys.exit(2)
    if as_json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        print(render_markdown(rows))
    sys.exit(0)


if __name__ == "__main__":
    main()
