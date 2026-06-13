# 免评委硬指标层 (`hard_metrics.py`) 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 构建一个纯离线测量工具，把悬空引用、符号覆盖、数值一致性、假设保护、代码足迹等程序可判信号聚合成一张跨项目 markdown 体检表。

**Architecture:** 给已有的 `verify_symbols.py` / `verify_numbers.py` 各抽一个返回 dict 的 `collect_*_metrics()` 函数（`main()` 改为调用它后按原格式 print，CLI 字节不变）；新增 `scripts/hard_metrics.py` 实现 3 个新解析器（引用 / 假设 / 代码足迹）+ 产物校验 + 聚合 + markdown 渲染 + CLI。

**Tech Stack:** Python 3.13、pytest 9.0.3、标准库（re/os/glob/subprocess/zipfile）、`pdfinfo`（缺则降级）。

**关键约束：** `complete/` / `ongoing/` 是 gitignored 运行数据，单元测试一律用 `tests/fixtures/` 下提交进仓库的小样本。真实 6 个 complete 项目只在最后一步（Task 9）做基线跑，属人工验证、非 pytest。

**导入约定：** `hard_metrics.py` 与两个 verify 脚本同在 `scripts/`，CLI 以 `python3 scripts/hard_metrics.py` 运行时 `sys.path[0]` 即 `scripts/`，故 `from verify_symbols import ...` 可用。测试通过 `tests/conftest.py` 把 `scripts/` 注入 `sys.path`。

---

### Task 0: 测试脚手架与 fixture

**Files:**
- Create: `tests/conftest.py`
- Create: `tests/fixtures/mini_proj/mini_paper.tex`
- Create: `tests/fixtures/mini_proj/references.bib`
- Create: `tests/fixtures/mini_proj/symbol_table.md`
- Create: `tests/fixtures/mini_proj/assumption_ledger.md`
- Create: `tests/fixtures/mini_proj/logs/solve.log`
- Create: `tests/fixtures/mini_proj/models/m1/01_a.py`
- Create: `tests/fixtures/mini_proj/models/m1/02_b.py`

- [ ] **Step 1: 写 conftest.py 注入 scripts/ 到 sys.path**

```python
# tests/conftest.py
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO_ROOT, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "mini_proj")
```

- [ ] **Step 2: 写 mini_paper.tex（已知引用 / 占位符 / 符号）**

引用键集合 = {a2004, b2003, c}（其中 c 在 bib 中不存在 → 1 个悬空）；含 1 个 `ABSTRACT_PLACEHOLDER`；正文用到符号 `\alpha`、`\beta`（`\beta` 不在符号表 → 1 个 undefined）；正文数字 `3.14`、`999.9`（`3.14` 在 log 中、`999.9` 不在 → 1 个 unmatched）。

```latex
\documentclass{article}
\begin{document}
ABSTRACT_PLACEHOLDER

\section{符号表}
\begin{tabular}{ll}
$\alpha$ & rate \\
\end{tabular}

\section{正文}
我们采用 SPRT 方法 \cite{a2004,b2003} 与鲁棒优化 \cite[p.~3]{c}。
参数 $\alpha = 3.14$，残差 $\beta = 999.9$。
\bibliographystyle{plain}
\end{document}
```

- [ ] **Step 3: 写 references.bib（a2004/b2003 被引，d 未被引，无 c）**

```bibtex
@article{a2004, title={A}, year={2004}}
@book{b2003, title={B}, year={2003}}
@article{d, title={D never cited}, year={2010}}
```

期望：cited={a2004,b2003,c}；bib={a2004,b2003,d}；dangling={c}→1；uncited={d}→1。

- [ ] **Step 4: 写 symbol_table.md（只定义 alpha）**

```markdown
# 符号表
| 符号 | 含义 |
|---|---|
| $\alpha$ | 速率 |
```

- [ ] **Step 5: 写 assumption_ledger.md（3 行：PROTECTED+CRITICAL / PROTECTED / 无标签）**

```markdown
# 假设登记簿
| id | 陈述 | 来源 | 若违反的影响 | 状态 | 标签 |
|---|---|---|---|---|---|
| A1 | 假设一 | spec | 影响一 | CONFIRMED | PROTECTED, CRITICAL |
| A2 | 假设二 | spec | 影响二 | INHERITED | PROTECTED |
| A3 | 假设三 | spec | 影响三 | OPEN | — |

> A1 注释行，不应被计入。
```

期望：total=3、protected=2、critical=1。

- [ ] **Step 6: 写 logs/solve.log（含 3.14，不含 999.9）**

```
result alpha = 3.14 converged
iterations = 42
```

- [ ] **Step 7: 写两个 models 下的 py 文件（已知行数）**

`models/m1/01_a.py`（3 行）：
```python
# a
x = 1
print(x)
```

`models/m1/02_b.py`（5 行）：
```python
# b
def f():
    return 2
y = f()
print(y)
```

期望：code_files=2、code_lines=8、code_mean_lines=4.0。

- [ ] **Step 8: Commit**

```bash
git add tests/conftest.py tests/fixtures/mini_proj
git commit -m "test: add mini fixture project for hard-metrics tests"
```

---

### Task 1: 重构 `verify_symbols.py` → `collect_symbol_metrics`

**Files:**
- Modify: `scripts/verify_symbols.py:263-322` (main 区段)
- Test: `tests/test_verify_symbols_refactor.py`

- [ ] **Step 1: 先存金标准 CLI 输出（特征化测试）**

Run:
```bash
mkdir -p tests/golden
python3 scripts/verify_symbols.py tests/fixtures/mini_proj mini > tests/golden/verify_symbols_mini.txt 2>&1 || true
```
说明：这是重构前的真实输出，作为"行为不变"的基准。

- [ ] **Step 2: 写测试（金标准回归 + collect_* dict 断言）**

```python
# tests/test_verify_symbols_refactor.py
import os, subprocess, sys
from conftest import FIXTURE, REPO_ROOT, SCRIPTS

GOLDEN = os.path.join(os.path.dirname(__file__), "golden", "verify_symbols_mini.txt")

def test_cli_output_byte_identical():
    out = subprocess.run(
        [sys.executable, os.path.join(SCRIPTS, "verify_symbols.py"), FIXTURE, "mini"],
        capture_output=True, text=True,
    )
    combined = out.stdout + out.stderr
    with open(GOLDEN) as f:
        assert combined == f.read()

def test_collect_symbol_metrics_dict():
    from verify_symbols import collect_symbol_metrics
    m = collect_symbol_metrics(FIXTURE, "mini")
    assert m["symbols_undefined"] == 1   # \beta used, not in table
    assert m["symbols_used"] >= 2
    assert "use_before_def" in m
```

- [ ] **Step 3: 运行测试，确认 collect 测试 FAIL**

Run: `python3 -m pytest tests/test_verify_symbols_refactor.py -v`
Expected: `test_cli_output_byte_identical` 视情况 PASS，`test_collect_symbol_metrics_dict` FAIL（`ImportError: cannot import name 'collect_symbol_metrics'`）。

- [ ] **Step 4: 重构 —— 抽出 collect 函数，main 调用它**

把 `main()` 中"计算 defined/used/undefined/use_before_def"的逻辑搬进新函数；`main()` 改为调用它再按**原样**打印。替换 `scripts/verify_symbols.py` 中 `def main():` 之前插入：

```python
def collect_symbol_metrics(project_dir, base_name):
    """返回符号覆盖指标 dict；不打印。文件缺失时各项尽量返回，paper 缺失返回 None。"""
    paper_path = os.path.join(project_dir, f'{base_name}_paper.tex')
    table_path = os.path.join(project_dir, 'symbol_table.md')
    if not os.path.exists(paper_path):
        return None
    defined = extract_defined_symbols(table_path)
    used, first_use = extract_used_symbols(paper_path)
    table_line = find_symbol_table_line(paper_path)
    undefined = sorted(used - defined)
    use_before_def = []
    if table_line > 0:
        use_before_def = sorted(
            s for s in (used & defined)
            if first_use.get(s, 10**9) < table_line - 5
        )
    return {
        "symbols_defined": len(defined),
        "symbols_used": len(used),
        "symbols_undefined": len(undefined),
        "use_before_def": len(use_before_def),
        "_undefined_list": undefined,
        "_use_before_def_list": use_before_def,
        "_first_use": first_use,
        "_table_line": table_line,
        "_table_path": table_path,
        "_paper_path": paper_path,
    }
```

然后把 `main()` 改为先取 `m = collect_symbol_metrics(...)`（保留 `--paper/--table` 分支的现有 paper/table 推断与 not-found 退出逻辑），用 `m["_*"]` 还原**逐字节相同**的打印与 `sys.exit` 行为。注意：`--paper/--table` 分支不经过 `collect_*`，保持原样直接计算即可，避免改变其行为。

- [ ] **Step 5: 运行测试，确认全部 PASS**

Run: `python3 -m pytest tests/test_verify_symbols_refactor.py -v`
Expected: 2 passed。若金标准测试失败，说明打印被改动，回到 Step 4 对齐。

- [ ] **Step 6: Commit**

```bash
git add scripts/verify_symbols.py tests/test_verify_symbols_refactor.py tests/golden/verify_symbols_mini.txt
git commit -m "refactor: extract collect_symbol_metrics, CLI output unchanged"
```

---

### Task 2: 重构 `verify_numbers.py` → `collect_number_metrics`

**Files:**
- Modify: `scripts/verify_numbers.py:159-218` (main 区段)
- Test: `tests/test_verify_numbers_refactor.py`

- [ ] **Step 1: 存金标准 CLI 输出**

Run:
```bash
python3 scripts/verify_numbers.py tests/fixtures/mini_proj mini > tests/golden/verify_numbers_mini.txt 2>&1 || true
```

- [ ] **Step 2: 写测试**

```python
# tests/test_verify_numbers_refactor.py
import os, subprocess, sys
from conftest import FIXTURE, SCRIPTS

GOLDEN = os.path.join(os.path.dirname(__file__), "golden", "verify_numbers_mini.txt")

def test_cli_output_byte_identical():
    out = subprocess.run(
        [sys.executable, os.path.join(SCRIPTS, "verify_numbers.py"), FIXTURE, "mini"],
        capture_output=True, text=True,
    )
    with open(GOLDEN) as f:
        assert out.stdout + out.stderr == f.read()

def test_collect_number_metrics_dict():
    from verify_numbers import collect_number_metrics
    m = collect_number_metrics(FIXTURE, "mini")
    assert m["numbers_unmatched"] >= 1   # 999.9 not in log
    assert m["numbers_matched"] >= 1     # 3.14 in log
```

- [ ] **Step 3: 运行，确认 collect 测试 FAIL**

Run: `python3 -m pytest tests/test_verify_numbers_refactor.py -v`
Expected: `test_collect_number_metrics_dict` FAIL（ImportError）。

- [ ] **Step 4: 重构 —— 抽出 collect 函数，main 调用它**

在 `def main():` 之前插入：

```python
def collect_number_metrics(project_dir, base_name):
    """返回数值一致性指标 dict；不打印。paper 缺失返回 None。"""
    tex_path = os.path.join(project_dir, f'{base_name}_paper.tex')
    if not os.path.exists(tex_path):
        return None
    log_dir = os.path.join(project_dir, 'logs')
    tables_dir = os.path.join(project_dir, 'tables')
    paper_numbers = extract_tex_numbers(tex_path)
    reference_numbers = extract_log_numbers(log_dir) | extract_table_numbers(tables_dir)
    matched, unmatched = [], []
    for entry in paper_numbers:
        (matched if number_matches(entry['value'], reference_numbers) else unmatched).append(entry)
    return {
        "numbers_matched": len(matched),
        "numbers_unmatched": len(unmatched),
        "_matched": matched,
        "_unmatched": unmatched,
        "_paper_numbers": paper_numbers,
        "_reference_count": len(reference_numbers),
    }
```

把 `main()` 改为调用它，并用返回值还原**逐字节相同**的打印与 `sys.exit(0 if not unmatched else 1)`。

- [ ] **Step 5: 运行测试，确认全部 PASS**

Run: `python3 -m pytest tests/test_verify_numbers_refactor.py -v`
Expected: 2 passed。

- [ ] **Step 6: Commit**

```bash
git add scripts/verify_numbers.py tests/test_verify_numbers_refactor.py tests/golden/verify_numbers_mini.txt
git commit -m "refactor: extract collect_number_metrics, CLI output unchanged"
```

---

### Task 3: 引用完整性解析器 `collect_citation_metrics`

**Files:**
- Create: `scripts/hard_metrics.py`
- Test: `tests/test_citation_metrics.py`

- [ ] **Step 1: 写测试**

```python
# tests/test_citation_metrics.py
import os
from conftest import FIXTURE

def test_citation_metrics():
    from hard_metrics import collect_citation_metrics
    tex = os.path.join(FIXTURE, "mini_paper.tex")
    bib = os.path.join(FIXTURE, "references.bib")
    m = collect_citation_metrics(tex, bib)
    assert m["dangling_cites"] == 1
    assert m["dangling_cite_keys"] == ["c"]
    assert m["uncited_entries"] == 1
    assert m["abstract_placeholder_residue"] == 1
```

- [ ] **Step 2: 运行，确认 FAIL**

Run: `python3 -m pytest tests/test_citation_metrics.py -v`
Expected: FAIL（ModuleNotFoundError: hard_metrics 或 ImportError）。

- [ ] **Step 3: 创建 hard_metrics.py 并实现引用解析器**

```python
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
```

- [ ] **Step 4: 运行，确认 PASS**

Run: `python3 -m pytest tests/test_citation_metrics.py -v`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add scripts/hard_metrics.py tests/test_citation_metrics.py
git commit -m "feat: add collect_citation_metrics (dangling-cite detector)"
```

---

### Task 4: 假设保护解析器 `collect_assumption_metrics`

**Files:**
- Modify: `scripts/hard_metrics.py`
- Test: `tests/test_assumption_metrics.py`

- [ ] **Step 1: 写测试**

```python
# tests/test_assumption_metrics.py
import os
from conftest import FIXTURE

def test_assumption_metrics():
    from hard_metrics import collect_assumption_metrics
    m = collect_assumption_metrics(os.path.join(FIXTURE, "assumption_ledger.md"))
    assert m["assumptions_total"] == 3
    assert m["protected"] == 2
    assert m["critical"] == 1

def test_assumption_metrics_missing():
    from hard_metrics import collect_assumption_metrics
    assert collect_assumption_metrics("/no/such/ledger.md") is None
```

- [ ] **Step 2: 运行，确认 FAIL**

Run: `python3 -m pytest tests/test_assumption_metrics.py -v`
Expected: FAIL（ImportError）。

- [ ] **Step 3: 实现 —— 追加到 hard_metrics.py**

只数"表格数据行"：以 `|` 开头、含至少 5 个 `|`、且第一格不是表头(`id`)、不是 `---` 分隔行。标签子串匹配。

```python
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
```

- [ ] **Step 4: 运行，确认 PASS**

Run: `python3 -m pytest tests/test_assumption_metrics.py -v`
Expected: 2 passed。

- [ ] **Step 5: Commit**

```bash
git add scripts/hard_metrics.py tests/test_assumption_metrics.py
git commit -m "feat: add collect_assumption_metrics (PROTECTED/CRITICAL counts)"
```

---

### Task 5: 代码足迹解析器 `collect_code_metrics`

**Files:**
- Modify: `scripts/hard_metrics.py`
- Test: `tests/test_code_metrics.py`

- [ ] **Step 1: 写测试**

```python
# tests/test_code_metrics.py
from conftest import FIXTURE

def test_code_metrics():
    from hard_metrics import collect_code_metrics
    m = collect_code_metrics(FIXTURE)
    assert m["code_files"] == 2
    assert m["code_lines"] == 8
    assert m["code_mean_lines"] == 4.0

def test_code_metrics_empty(tmp_path):
    from hard_metrics import collect_code_metrics
    m = collect_code_metrics(str(tmp_path))
    assert m["code_files"] == 0
    assert m["code_mean_lines"] is None
```

- [ ] **Step 2: 运行，确认 FAIL**

Run: `python3 -m pytest tests/test_code_metrics.py -v`
Expected: FAIL（ImportError）。

- [ ] **Step 3: 实现 —— 追加到 hard_metrics.py**

```python
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
```

- [ ] **Step 4: 运行，确认 PASS**

Run: `python3 -m pytest tests/test_code_metrics.py -v`
Expected: 2 passed。

- [ ] **Step 5: Commit**

```bash
git add scripts/hard_metrics.py tests/test_code_metrics.py
git commit -m "feat: add collect_code_metrics (models/ python footprint)"
```

---

### Task 6: 产物校验 `collect_artifact_metrics`

**Files:**
- Modify: `scripts/hard_metrics.py`
- Test: `tests/test_artifact_metrics.py`

- [ ] **Step 1: 写测试（用 tmp_path 造一个最小 zip + 假 pdf）**

```python
# tests/test_artifact_metrics.py
import os, zipfile

def test_artifact_metrics(tmp_path):
    from hard_metrics import collect_artifact_metrics
    base = "demo"
    (tmp_path / f"{base}_paper.pdf").write_bytes(b"%PDF-1.4 fake")
    zpath = tmp_path / f"{base}_submission.zip"
    with zipfile.ZipFile(zpath, "w") as z:
        z.writestr("a.txt", "hello")
    m = collect_artifact_metrics(str(tmp_path), base)
    assert m["pdf_ok"] is True
    assert m["zip_ok"] is True
    assert "pdf_pages" in m   # 可能为 None（pdfinfo 读不了假 pdf）

def test_artifact_metrics_missing(tmp_path):
    from hard_metrics import collect_artifact_metrics
    m = collect_artifact_metrics(str(tmp_path), "nope")
    assert m["pdf_ok"] is False
    assert m["zip_ok"] is False
    assert m["pdf_pages"] is None
```

- [ ] **Step 2: 运行，确认 FAIL**

Run: `python3 -m pytest tests/test_artifact_metrics.py -v`
Expected: FAIL（ImportError）。

- [ ] **Step 3: 实现 —— 追加到 hard_metrics.py**

PDF 在 `<project>/papers/` 或项目根下；提交包查 `<base>_submission.zip`。`zip_ok` 用 `testzip()` 校验完整性。`pdf_pages` 用 `pdfinfo`，失败返回 None。

```python
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
```

- [ ] **Step 4: 运行，确认 PASS**

Run: `python3 -m pytest tests/test_artifact_metrics.py -v`
Expected: 2 passed。

- [ ] **Step 5: Commit**

```bash
git add scripts/hard_metrics.py tests/test_artifact_metrics.py
git commit -m "feat: add collect_artifact_metrics (pdf/zip ok, pages ref)"
```

---

### Task 7: 聚合 `collect_all` + 派生指标

**Files:**
- Modify: `scripts/hard_metrics.py`
- Test: `tests/test_collect_all.py`

- [ ] **Step 1: 写测试**

```python
# tests/test_collect_all.py
from conftest import FIXTURE

def test_collect_all_flat():
    from hard_metrics import collect_all
    row = collect_all(FIXTURE, "mini")
    assert row["project"] == "mini"
    assert row["dangling_cites"] == 1
    assert row["assumptions_total"] == 3
    assert row["protected"] == 2
    assert row["code_files"] == 2
    # 派生：symbol_coverage = 1 - undefined/used，used>0
    assert row["symbol_coverage"] is not None
    assert 0.0 <= row["symbol_coverage"] <= 1.0
    # 缺失子模块不应让整行崩
    assert "numbers_unmatched" in row
```

- [ ] **Step 2: 运行，确认 FAIL**

Run: `python3 -m pytest tests/test_collect_all.py -v`
Expected: FAIL（ImportError）。

- [ ] **Step 3: 实现 —— 追加到 hard_metrics.py**

每个 `collect_*` 包在 try 里，异常或 None 降级为空 dict + stderr 警告，保证整行不崩。扁平合并并去掉 `_` 前缀的明细键（明细只在单项目模式用）。

```python
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
```

- [ ] **Step 4: 运行，确认 PASS**

Run: `python3 -m pytest tests/test_collect_all.py -v`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add scripts/hard_metrics.py tests/test_collect_all.py
git commit -m "feat: add collect_all aggregator + symbol_coverage derived metric"
```

---

### Task 8: markdown 渲染 + CLI（单项目 / --batch / --json）

**Files:**
- Modify: `scripts/hard_metrics.py`
- Test: `tests/test_render_and_cli.py`

- [ ] **Step 1: 写测试**

```python
# tests/test_render_and_cli.py
import os, subprocess, sys, json
from conftest import FIXTURE, SCRIPTS

HM = os.path.join(SCRIPTS, "hard_metrics.py")

def test_render_markdown():
    from hard_metrics import collect_all, render_markdown
    md = render_markdown([collect_all(FIXTURE, "mini")])
    assert md.startswith("|")
    assert "dangling_cites" in md
    assert "| mini |" in md

def test_cli_single():
    out = subprocess.run([sys.executable, HM, FIXTURE, "mini"], capture_output=True, text=True)
    assert out.returncode == 0
    assert "dangling_cites" in out.stdout

def test_cli_batch_json():
    parent = os.path.dirname(FIXTURE)  # tests/fixtures, contains mini_proj/
    out = subprocess.run([sys.executable, HM, "--batch", parent, "--json"],
                         capture_output=True, text=True)
    assert out.returncode == 0
    data = json.loads(out.stdout)
    assert any(r["project"] == "mini_proj" for r in data)
```

- [ ] **Step 2: 运行，确认 FAIL**

Run: `python3 -m pytest tests/test_render_and_cli.py -v`
Expected: FAIL（render_markdown 未定义 / CLI 无 main）。

- [ ] **Step 3: 实现 —— render_markdown + main，追加到 hard_metrics.py**

固定列序；`None` 显示 `—`；bool 显示 ✓/✗。批量模式把 `<dir>/<sub>/` 当项目，`base=sub`，需存在 `<base>_paper.tex` 才纳入，否则 stderr 跳过提示。

```python
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
    heads = [h for _, h in _COLUMNS]
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
```

- [ ] **Step 4: 运行，确认 PASS**

Run: `python3 -m pytest tests/test_render_and_cli.py -v`
Expected: 3 passed。

- [ ] **Step 5: 跑全套测试确认无回归**

Run: `python3 -m pytest tests/ -v`
Expected: 全部 passed。

- [ ] **Step 6: Commit**

```bash
git add scripts/hard_metrics.py tests/test_render_and_cli.py
git commit -m "feat: add render_markdown + hard_metrics CLI (single/batch/json)"
```

---

### Task 9: 在 6 个真实 complete 项目上跑基线（人工验证）

**Files:**
- Create: `evaluation/hard_metrics_baseline.md`

- [ ] **Step 1: 跑批量，落地基线表**

Run:
```bash
python3 scripts/hard_metrics.py --batch complete/ | tee evaluation/hard_metrics_baseline.md
```
Expected: 一张含 6 行（test_cumcm2024a/b + 4 个消融）的 markdown 表。

- [ ] **Step 2: 人工核对已知锚点**

对照 `test_cumcm2024b` 行：`dangling_cites` 应为 0（其 `citation_audit.md` 已确认无幻引）、`assumptions_total=8`、`protected=4`、`code_files=15`。对照 `no_methodlib` 行：`dangling_cites` 应 > 0（memory 记录的唯一硬判别信号）。若 `no_methodlib` 的悬空引用不 > 0，停下排查解析器或重新确认该信号来源，**不要**直接接受。

- [ ] **Step 3: 表头补一行生成时间与口径说明**

在 `evaluation/hard_metrics_baseline.md` 顶部手加：生成日期 2026-06-13、数据源 `complete/`、口径"程序可判、不依赖 LLM 评委"。

- [ ] **Step 4: Commit**

```bash
git add evaluation/hard_metrics_baseline.md
git commit -m "data: first judge-free hard-metrics baseline over 6 complete projects"
```

---

## Self-Review

**Spec 覆盖核对：**
- 悬空引用 / 未引用 / 占位符残留 → Task 3 ✓
- 符号覆盖（含 use-before-def、coverage 派生）→ Task 1 + Task 7 ✓
- 数值一致性 → Task 2 ✓
- PROTECTED/CRITICAL 假设计数 → Task 4 ✓
- 代码足迹（models/ 唯一范围）→ Task 5 ✓
- PDF/zip OK + 页数 reference 列 → Task 6 ✓
- 轻量重构、CLI 字节不变 → Task 1/2 金标准回归测试 ✓
- 跨项目 markdown 表 + 可选 --json → Task 8 ✓
- 错误降级不崩表 → Task 7 `_safe` + Task 8 discover 跳过 ✓
- 基线跑 + 对照 P1 → Task 9 ✓
- 不碰 run_paper.sh / agent 契约 → 全程无该类改动 ✓

**占位符扫描：** 无 TBD/TODO；每个代码步均含完整可跑代码。

**类型一致性：** `collect_symbol_metrics` / `collect_number_metrics` / `collect_citation_metrics` / `collect_assumption_metrics` / `collect_code_metrics` / `collect_artifact_metrics` / `collect_all` / `render_markdown` 命名在 Task 1–8 间一致；`collect_all` 引用的键（symbols_used/undefined、dangling_cites、assumptions_total、protected、code_files）均在前序 Task 的返回 dict 中定义。

**已知风险（实现时留意）：** 金标准回归依赖"重构后打印逻辑逐字节复刻"——若 `main()` 里有难以从 `collect_*` 还原的局部变量（如 `--paper/--table` 分支），保持该分支原样不经过 collect 即可（已在 Task 1 Step 4 注明）。
