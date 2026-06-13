# 设计：免评委硬指标层 (`hard_metrics.py`)

> 日期: 2026-06-13
> 状态: 已批准，待写实现计划
> 背景: 消融实验的 P0 工作项。详见 `evaluation/EXPERIMENTS_STATUS.md` 的后续优先级。

## 动机

消融研究当前的测量层不可信：DeepSeek 评委分不开细粒度消融（分数被锚定），
只有 Gemini 能分，且 n=1、单题。除 `no_methodlib` 外的三个消融差异都落在单一
LLM 评委的噪声带里。**在测量层可信之前，再跑更多消融只是给不可证伪的结论加小数点。**

唯一已被验证、不依赖评委的硬判别信号是 `no_methodlib` 的悬空引用。本工作把这类
**程序可判、不依赖 LLM** 的信号系统化，做成一张跨项目可比的"体检表"，让后续消融
从"靠一个会锚定的评委"升级为"客观指标 + 评委交叉验证"。

## 范围与非目标

- **定位：纯离线测量工具。** 只读项目产物、输出报表。**不** 修改 `run_paper.sh`、
  不碰 agent 契约（`STEPS.md` / prompts）、不阻断流水线。把"硬门禁"留给后续 P2。
- 非目标：自动判定 PROTECTED 假设是"创新性 vs 技术性"（主观，留给人）；PDF 页数
  作为质量分（已知页数被代码附录主导，见 memory `paper-page-count-not-quality-signal`）。
- 主输出：跨项目 markdown 对比表。`--json` 为可选附带开关，默认关。

## 指标集（全部程序可判）

| 指标键 | 含义 | 来源 | 解析方式 |
|---|---|---|---|
| `dangling_cites` | 正文引用了但 `.bib` 无条目的 key 数 | `.tex` ∩∁ `.bib` | 新写 |
| `dangling_cite_keys` | 上述具体 key 列表（明细用） | 同上 | 新写 |
| `uncited_entries` | `.bib` 有但正文从未引用的条目数 | 同上反向 | 新写 |
| `abstract_placeholder_residue` | `.tex` 中残留的 `ABSTRACT_PLACEHOLDER` 次数 | `.tex` 文本扫描 | 新写 |
| `symbols_used` / `symbols_undefined` / `use_before_def` | 符号覆盖 | `verify_symbols.py` | 轻重构复用 |
| `symbol_coverage` | `1 - undefined/used`（used=0 时记 `null`） | 派生 | 新写（在聚合器里算） |
| `numbers_matched` / `numbers_unmatched` | 正文数字有无 log/table 出处 | `verify_numbers.py` | 轻重构复用 |
| `assumptions_total` / `protected` / `critical` | 假设登记簿计数 | `assumption_ledger.md` 表格 | 新写 |
| `code_files` / `code_lines` / `code_mean_lines` | `models/` 下 `*.py` 足迹（碎片化度） | 文件系统 | 新写 |
| `pdf_ok` / `zip_ok` | 编译产物 / 提交包存在且非空/有效 | 文件系统 + zip 校验 | 复用 `evaluate_modeling_project.zip_ok` |
| `pdf_pages` | PDF 页数 — **仅 reference 列，非质量分** | `pdfinfo` 若可用 | 新写，缺工具记 `null` |

### 关键解析约束（实现时必须遵守）

- **引用 key 解析**：一个 `\cite` 可含逗号分隔的多个 key，且可带可选参数，例如
  `\cite[见][第3章]{a2004,b2003}`。解析须：匹配 `\cite`、`\citep`、`\citet`、
  `\citealp`、`\citeyear`（及带 `*` 变体），剥离所有 `[...]` 可选参数，再按 `,`
  拆分、`strip()` 每个 key。当前论文用 natbib 风格 + `\bibliographystyle{plain}`。
- **bib key 解析**：`^@<type>{<key>,`，忽略大小写与前导空白；跳过 `@comment`/`@string`。
- **假设表解析**：`assumption_ledger.md` 的 markdown 表，列含 `状态` 与 `标签`；
  `标签` 列含 `PROTECTED` / `CRITICAL`（可同格共存，逗号分隔）。只统计表格数据行
  （以 `|` 开头、且非表头/分隔行）。计数对 `标签` 列做子串匹配。
- **代码足迹**：仅统计 `<project>/models/**/*.py`（求解主体）。`code_mean_lines` =
  `code_lines / code_files`，`code_files=0` 时记 `null`。

## 架构（3 个文件）

```
scripts/verify_symbols.py   ← 抽出 collect_symbol_metrics(project_dir, base) -> dict；main() 改为调用它再 print
scripts/verify_numbers.py   ← 抽出 collect_number_metrics(project_dir, base) -> dict；main() 同上
scripts/hard_metrics.py     ← 新增：引用/假设/代码足迹解析器 + 聚合 + markdown 渲染 + CLI
```

**复用方式**：轻量重构——给两个已有脚本各抽一个返回 dict 的 `collect_*` 函数，
原 `main()` 改为"调用 collect → 按原格式 print"，**CLI 输出字节不变**（保证现有
调用方与流水线不受影响）。`hard_metrics.py` 直接 `import` 这两个函数拿结构化结果，
不走 subprocess + 正则。

### 单元边界

- `verify_symbols.collect_symbol_metrics(project_dir, base)` → 符号 dict。
- `verify_numbers.collect_number_metrics(project_dir, base)` → 数值 dict。
- `hard_metrics.collect_citation_metrics(tex_path, bib_path)` → 引用 dict。
- `hard_metrics.collect_assumption_metrics(ledger_path)` → 假设 dict。
- `hard_metrics.collect_code_metrics(project_dir)` → 代码足迹 dict。
- `hard_metrics.collect_artifact_metrics(project_dir, base)` → pdf/zip/pages dict。
- `hard_metrics.collect_all(project_dir, base)` → 合并上述全部 + 派生指标的扁平 dict。
- `hard_metrics.render_markdown(rows: list[dict]) -> str` → 跨项目对比表。

### CLI

```bash
# 单项目：打印该项目明细（人读）
python3 scripts/hard_metrics.py complete/test_cumcm2024b test_cumcm2024b

# 批量：扫描一个目录下所有子项目，输出跨项目 markdown 对比表
python3 scripts/hard_metrics.py --batch complete/

# 附带机器可读 JSON（默认关）
python3 scripts/hard_metrics.py --batch complete/ --json
```

- 批量模式：把 `<dir>/<base>/` 当作项目，`base` 取子目录名，paper 取
  `<dir>/<base>/<base>_paper.tex`。跳过没有 paper 的子目录并在 stderr 提示。
- `base` 与项目路径推断逻辑复用现有约定（与 `evaluate_modeling_project.py` 一致）。

## 错误处理

- 任一文件缺失（如无 `assumption_ledger.md` / 无 paper）：该项指标记 `null`，
  **不让整张表崩**；在该格显示 `—`。
- `pdfinfo` 等外部工具不存在：`pdf_pages` 记 `null`，不报错。
- 解析异常被该 `collect_*` 捕获并降级为 `null` + 一条 stderr 警告，保证批量跑完。

## 验证方式

- 单元：对 `test_cumcm2024b` 已知值断言——bib 有 12 个条目、12 个被引用 key、
  `dangling_cites == 0`（其 `citation_audit.md` 已人工确认无幻引）；`code_files == 15`；
  `assumptions_total == 8`、`protected == 4`（A1–A4 标 PROTECTED）。
- 回归：重构后 `verify_symbols.py` / `verify_numbers.py` 的 CLI 输出与重构前逐字节一致。
- 端到端：在现有 6 个 complete 项目上跑 `--batch complete/`，产出第一张基线体检表。
  这张表同时为 P1（A 题 66.3 vs B 题 91.6 缺口）提供客观对照。

## 预期产物

- 重构后的 `scripts/verify_symbols.py`、`scripts/verify_numbers.py`（行为不变）。
- 新 `scripts/hard_metrics.py`。
- 第一张基线体检表（落到 `evaluation/` 下，文件名实现时定）。
