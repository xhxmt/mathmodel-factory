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
