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
