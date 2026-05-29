# my_understanding.md — Paper Factory 架构理解笔记

> 写作时机：阶段 0.4，改造工作开始前。
> 目的：把对原仓库的理解显式化，便于用户校对，避免后续阶段返工。
> 范围：基于直接阅读 `launch_agents.sh`、`run_paper.sh`、`STEPS.md`、
> `analysis_guide.md`、`stata_submit.sh`、`stata_wrapper.sh`、
> `compile_paper.sh`、若干 prompt 样本（`step1a`、`step2_findings_agent`、
> `step3_decider`）。**未跑通 demo 项目**（缺 Stata、pdflatex，
> 详见"环境状况"段）。

---

## 1. 仓库布局（顶层）

```
paper_factory/
├── launch_agents.sh        ← 358 行；薄 CLI 层（new/resume/pause/run/attach/trace/status）
├── run_paper.sh            ← 2218 行；per-project 工作流驱动核心
├── stata_submit.sh         ← 150 行；本地 Stata 异步作业提交器
├── stata_wrapper.sh        ← 39 行；探测 stata-mp/se/bare 可执行
├── compile_paper.sh        ← 16 行；pdflatex + bibtex + pdflatex×2
├── trace_viewer.py         ← Codex/Claude 轨迹文件查看
├── STEPS.md                ← 168 行；16 步契约（agent-facing 文档）
├── analysis_guide.md       ← 180 行；Stata/figure/file-layout 规范
├── CLAUDE.md               ← 给 Claude Code 用的元说明（已存在，未追踪）
├── README.md               ← 用户面向 quick start
├── prompts/                ← 33 个 prompt 文件，命名 stepN_xxx.txt
├── scripts/
│   ├── verify_numbers.py        ← 论文数字 ↔ Stata 日志一致性核查
│   └── cleanup_project_artifacts.py  ← Step 16 清理可重建中间产物
└── resources/
    ├── style/paper.sty
    ├── style/model_papers_style.json
    ├── bib/bibliography.bst
    └── examples/Abstract_examples.md
```

运行时目录（gitignore，由 launcher 创建）：
`ongoing/`、`complete/`、`papers/`、`logs/`、`run_state/`。

---

## 2. 两层启动器架构

**Layer 1 — `launch_agents.sh`**：
- 处理 CLI 子命令：`new` / `resume` / `pause` / `run` / `attach` / `trace` / `status`
- 写 `run_state/process_registry`（每行 `base_name pid`）
- 写每项目的 `.runner.pid`、`.paused`、`.killed` 标记文件
- `new` 创建项目目录结构（含 style/bib/figures/tables/do/logs/data/raw,intermediate,final 等子目录），
  拷贝 `paper.sty`、`bibliography.bst`、`model_papers_style.json`、`analysis_guide.md`，
  生成最初的 `checkpoint.md`（含 `Last completed step: -1`）
- `submit_project` 通过 `nohup ... run_paper.sh ... &` 拉起 runner，pid 写入 `.runner.pid`

**Layer 2 — `run_paper.sh`**：
- 每项目独占的工作流驱动
- 启动时把自己**快照** 到 `logs/runner_snapshots/run_paper_<ts>_<pid>.sh`，
  通过 `RUN_PAPER_SNAPSHOT=1` 标志 `exec` 进快照（关键设计：编辑 `run_paper.sh`
  时已在跑的项目不受影响）
- 通过 mkdir 实现 `.runner.lock/` 互斥锁，含 `.runner.lock.info`（pid/jobid/host/started）
- 主循环 `while STEP < 16` 按当前 `STEP` 调度 `run_step_$((STEP+1))`，
  retry 最多 `MAX_RETRIES=5` 次，超过则 fatal 退出

---

## 3. File-state authoritative 设计（核心哲学）

**`infer_step()`**（`run_paper.sh:170-335`）**不信任 `checkpoint.md`**，
而是从磁盘文件存在性 + 最小行数 + 时间戳关系反推到达的步数：

| Step | 判断证据 |
|------|----------|
| 16 | `papers/{base}_paper.pdf` 存在 |
| 15 | `derobotification.md` 存在且 ≥5 行 且 `paper.tex` 不含 `ABSTRACT PLACEHOLDER` |
| 14 | `abstract_draft.md` ≥300 字符 且 `paper.tex` 无 placeholder |
| 13 | `table_formatting.md` 存在 |
| 12 | `citation_audit.md` 存在 |
| 11 | `final_review.md` ≥20 行 |
| 10 | `do/archive/pre_step10/` 目录 + `revision_summary.md` + 文件时间戳关系 |
| 9 | `review_comments.md` ≥20 行 |
| 8 | `code_review.md` ≥20 行 |
| 7 | `{base}_paper.tex` >200 行 + `\begin{document}` + `\end{document}` + 含 placeholder |
| 6 | `findings_brief.md` 包含 "methods audit" 字串 |
| 5 | `findings_brief.md` 包含 "data audit" 字串 |
| 4 | `argument_decision.md` >20 行 |
| 3 | `findings_decision.md` >20 行 + `findings_brief.md` >40 行 |
| 2 | 6 个 stream 都 validated（`findings_critique_<N>.md` 首行 `VERDICT: VALIDATED` + `findings_memo_<N>.md` ≥20 行 + 对应前缀 figure/table 存在）|
| 1 | `codex_research.md` + `data_wrangle.md` + `data_context.md` + `key_variables.md` + `descriptive_map.md` 都 ≥行数阈值 |
| 0 | `project_brief.md` 存在 |
| -1 | 啥都没有 |

**后果**：要让 runner 重做某步，**删该步的产物文件**，编辑 `checkpoint.md` 没用。
runner 启动时反过来用 `infer_step` 的结果重写 `checkpoint.md`。

**复审重置**：`.review_state.json`（`resume_step` + `requested_at_epoch`）让
`infer_step` 把比某时间戳更早的产物视为不存在——支持人工触发的"回到第 N 步"。

---

## 4. Step 调度的几种原语

`run_paper.sh` 给每个 step 暴露 `run_step_N()` 函数。底层只用四种原语：

- **`run_codex <prompt> <timeout> <hang_timeout>`**
  跑 Codex (`codex exec --model gpt-5.5 -c model_reasoning_effort=xhigh --dangerously-bypass-approvals-and-sandbox ...`)，
  通过监听 `~/.codex/sessions/.../*.jsonl` 轨迹文件的 size 是否增长做 hang 检测

- **`run_claude_worker <prompt> <timeout>`**
  跑 Claude CLI (`claude -p ... --dangerously-skip-permissions --effort max`)，
  无主动 hang 检测（依赖 `timeout`）

- **组合原语**
  - `run_claude_then_codex` / `run_codex_then_claude`：失败或产物缺失时换另一边重试
  - `run_codex_parallel <timeout> <hang_timeout> p1 p2 ...`：N 路并行 Codex（Step 2、Step 4 用），每路独立 hang 检测
  - `run_claude_fallback <step>`：最后一道兜底——告诉 Claude 用自己的工具完成整步，**禁止再调 Codex**

- **每步统一的执行壳层**（主循环里）：
  - 写 `.heartbeat` 为 `ACTIVE:<STEP> <epoch>`（或之后 `STUCK:<STEP> <epoch>`）
  - 启动 `_start_activity_monitor` 后台进程
  - dispatch 到 `run_step_N`，结束后 `kill` monitor、`cleanup_children` 杀残留子进程
  - 用 `verify_step`（即 `infer_step >= 当前步`）判定真假成功，与 exit code 无关
  - **Step 11 特殊**：解析 `VERDICT:` 行决定 `PASS_WITH_DIRECT_FIXES` / 触发回到 Step 10
    （用 `.step11_reopen_to_step10` 和 `.step11_reopened_once` 两个标记控制最多一次回退）

---

## 5. Prompt 渲染与占位符

`render_prompt <template>` 流程：
1. 输出 `common_prompt_preamble`（要求读 `analysis_guide.md`、读 `human_review.md`
   作为最新评审指南、禁止参考已完成项目等）
2. 对 `prompts/<template>` 做 sed 替换：
   - `__PROJECT_PATH__` → 项目绝对路径
   - `__RESEARCH_QUESTION__` → 来自 `checkpoint.md`
   - `__BASE_NAME__` → 项目 base name
   - `__FACTORY__` → factory 根
3. Step 2 额外替换 `__STREAM_ID__` 和 `__STREAM_PREFIX__`（在 step 函数内手动替换，不在 `render_prompt`）
4. 通过 `get_user_note` 从可选 `web/notes.json[base][step_N]` 读"研究者备注"，
   附在末尾作 `NOTE FROM THE RESEARCHER: ...`
5. `prepend_agent_key` 把 step 对应 key 前置作为缓存提示

---

## 6. Stata 提交模型（要被替换的关键模块）

`stata_submit.sh`：
- 接口：`./stata_submit.sh do/file.do` → 输出 `local_<YYYYMMDDHHMMSS>_<PID>`；
  `--status <jobid>`；`--wait <jobid>`；`--dry-run`；`--time HH:MM:SS`（仅做兼容，无效）
- 用 `nohup stata_wrapper.sh -b do <DOFILE>` 后台启动
- 元数据：`run_state/stata_jobs/<jobid>.meta`（pid/dofile/workdir/log_file/moved_log/stderr_log/requested_time/started）
- 状态检测：
  - 进程仍在 → `RUNNING`
  - 日志含 `r(N);` → `FAILED`
  - 日志含 `end of do-file` → `COMPLETED`
  - 否则 → `EXITED` 或 `UNKNOWN`
- 日志先在 dofile 同目录生成 `<base>.log`，agent 自己挪到 `logs/<base>.log`

`stata_wrapper.sh` 仅做二进制路径自动探测（候选清单 + `STATA_BIN` 覆盖）。

---

## 7. Hang 检测的进程白名单（与 refactor_plan 不一致点）

阶段 1.2 提到的 `STATA_PROCS` 变量**实际不存在**——hang detection 的进程白名单是
**硬编码 inline 在两处**：

- `run_paper.sh:1251`（`run_codex_parallel` 的检测块）：
  ```bash
  if [[ "$pname" == srun || "$pname" == stata* || "$pname" == python* \
        || "$pname" == Rscript || "$pname" == R || "$pname" == julia ]]; then
  ```
- `run_paper.sh:967-983`（`run_codex` 单实例）只检测 `children > 0`，不分类型；
  注释提到 stata、python

**改造时**：要修改 line 1251 这一行（把建模相关求解器都加进去：
`matlab|gurobi_cl|cplex|scip|ipopt|octave`），不能搜 `STATA_PROCS`。

---

## 8. 项目内的跨步状态文件

- `checkpoint.md`：每步执行后由 shell（**不是 LLM**）用 `sed` 更新
  `Last completed step` 和 `Timestamp`。出错路径上可能落后于真实状态。
- `findings_brief.md`：从 Step 3 开始的单一权威分析摘要；
  Step 5、6 把审计章节**追加**进去而不是重写。
- `audit_issue_ledger.md`：Step 4 创建的跨步问题追踪台账；
  Step 11 在台账有未解决的 blocking 项时**不能 pass**。
  这是改造时"PROTECTED 标签"的天然挂载点。
- `.heartbeat`：`<STEP> <epoch>`、`ACTIVE:<STEP> <epoch>` 或 `STUCK:<STEP> <epoch>`，
  锁的 stale 判定依赖它（>下一步超时 + 30min buffer 则视为僵死）。
- `.review_state.json`：复审请求 → 让 `infer_step` 跳过更早产物。

---

## 9. 当前环境状况（写于阶段 0.4）

| 依赖 | 状态 | 影响 |
|------|------|------|
| `codex` CLI 0.125.0 | ✓ | 可用 |
| `claude` CLI 2.1.143 | ✓ | 可用 |
| `python3` 3.13.5 | ✓ | 可用 |
| Anthropic API key | ✓（环境变量已配置） | 可用 |
| OpenAI / Codex auth | 未显式见环境变量，但 `~/.codex/auth.json` 存在 4KB+ | 假定 codex CLI 已认证 |
| Stata | **缺失** | 原 demo 16 步几乎不可能跑通 |
| pdflatex / bibtex | **缺失** | Step 16 编译会失败 |
| matlab / julia / R / gurobi | 都缺失 | 阶段 1+ 需要至少装 1-2 个 |

**结论**：阶段 0.3（跑通原始 demo）当前环境下做不到。原计划本就把 0.3
作为基线验证；改造目标既然是替换 Stata 为多求解器，这个基线验证的边际价值有限。
**建议跳过 0.3**，把它替换成"阶段 1 完成后用纯 Python 跑通一个 minimal demo"
作为新基线（既验证基础设施，也验证 solver_submit.sh）。

---

## 10. 改造接入点速查（给后续阶段用）

按改造影响面排序：

### 必须修改

| 文件 | 改动 | 阶段 |
|------|------|------|
| `analysis_guide.md` → `modeling_guide.md` | 替换为建模规范 | 1.3 |
| `stata_submit.sh` → 旁立 `solver_submit.sh` | 新增多求解器调度（保留原 sh 备用） | 1.1 |
| `run_paper.sh:1251` 硬编码进程白名单 | 加入建模求解器进程名 | 1.2 |
| `run_paper.sh` `common_prompt_preamble`（约 line 826）引用 analysis_guide.md | 改为 modeling_guide.md | 1.4 |
| `launch_agents.sh:293` `cp $ANALYSIS_GUIDE` | 改 cp modeling_guide.md | 1.4 |
| `launch_agents.sh:289` 项目目录结构 mkdir 列表 | 加 `problem/`、`models/m1..mN`、`scripts/` 等 | 2 |
| `STEPS.md` | 重写为建模 16 步 | 3.1 |
| `prompts/step*.txt` | 大改 / 重写 | 2.2、3.2 |
| `run_paper.sh::infer_step` | 重定义每步的判断证据 | 3.3 |
| `run_paper.sh::run_step_N` | 调度 prompt 重新连接 | 3.3 |

### 新建

- `solver_submit.sh`（阶段 1）
- `modeling_guide.md`（阶段 1）
- `method_library/**`（阶段 2，方法知识库）
- `prompts/step0_problem_parsing.txt` + 后续步 prompt（阶段 2、3）
- consultation 机制相关（阶段 4，**已被用户暂忽略**）

### 不动 / 仅微调

- `compile_paper.sh`（保持 pdflatex+bibtex+pdflatex×2）
- `scripts/verify_numbers.py`（建模阶段也需要"论文数字 ↔ 日志数字"核对，
  接口很可能可复用，只要把"Stata log → results.json" 改成"求解器 log → results.json"）
- `scripts/cleanup_project_artifacts.py`（用户/作者已表示能识别 `data/raw` vs
  `analysis/*`，多数情况下兼容；可能要扩展识别 `models/` 子目录）
- `trace_viewer.py`（独立工具，与改造解耦）
- 两层启动器架构、snapshot、锁、心跳、stale 重收：**全部保留**
- `render_prompt` 的占位符机制：**保留并扩展**，
  阶段 2 会加 `__PROBLEM_TYPE__`、`__SUBPROBLEM_ID__` 等

---

## 11. 我的疑问 / 待确认点

1. **新仓库的提交策略**：当前 remote 已改为 `origin=mathmodel-factory.git, upstream=paper_factory.git`，
   但本地分支 `modeling-factory` 未推送。等用户在 GitHub 上确认 repo 已创建后再推。

2. **是否要保留 `complete/` 内的原 Paper Factory 历史项目**：当前仓库
   `ongoing/` 和 `complete/` 都是空的（gitignore），无需处理。但用户在自己电脑上
   如果跑过原 demo，可能希望保留参考。**建议在 README 顶部加一节说明
   "ongoing/complete/papers/ 都从空开始；原 Paper Factory 历史不迁移"**。

3. **Codex 模型 ID `gpt-5.5`**：写死在 `run_paper.sh` 多处（约 7 处）。
   是否要在阶段 1 抽成变量，方便比赛中切换到更新的模型？

4. **`codex` 的认证方式**：环境变量没看到 `OPENAI_API_KEY`，但 `~/.codex/auth.json`
   有 4KB+。如果用户改了认证方式，需告知。

5. **stata_wrapper.sh 的探测列表**：是否要保留作为兼容层（一旦用户哪天给某个项目装了 Stata 想用），
   还是阶段 1 彻底删除？目前 refactor_plan.md 说"保留不删除便于回滚"，我跟着这个原则。

6. **阶段 4 的"人类介入点"**：阶段 3 的 Step 3（方法选择）和 Step 14（摘要）
   描述里写了"强制人类介入"。但阶段 4 的咨询协议用户暂忽略——这意味着阶段 3
   实现 Step 3、Step 14 时**不能依赖**咨询协议的 file format。
   暂定方案：用 `human_review.md`（原 Paper Factory 已有的接口）作为人类介入入口，
   step prompt 里告诉 agent "若 `human_review.md` 中含 step3 决策段，遵循之；否则按 default 选最稳的方案推进"。
   留待阶段 3 决策时再确认。

7. **MCM/CUMCM 模板选择**：`modeling_guide.md` 提到"美赛用 mcmthesis、国赛用国赛模板"。
   `resources/style/paper.sty` 是原 Paper Factory 的论文 sty。阶段 1.3 需要决策：
   - 在 `resources/style/` 加两份模板（mcmthesis.cls、cumcm.cls）？
   - 还是 step0 解析题目时自动选模板？
   建议：先在阶段 1 把英文 mcmthesis 模板加进去（用得多），国赛模板等阶段 3 再加。

8. **`__FACTORY__` 引用**：`render_prompt` 把 `__FACTORY__` 替换成 factory 根。
   阶段 2 的方法库 prompt 里需要让 agent 读 `__FACTORY__/method_library/...`，
   这条线索没问题，继承现有机制即可。

---

## 12. 整体感受

原仓库**工程质量非常扎实**——尤其下面这几点值得继承到 Modeling Factory：

- **file-state authoritative**：用磁盘当真相，checkpoint 只做展示。重启鲁棒性极佳。
- **snapshot**：用 `cp + exec` 的快照机制让代码修改和运行的项目互不干扰。
- **小步重试**：每步独立 retry、独立超时、独立日志，失败局限化。
- **白盒 hang 检测**：trace 文件 size 不变 + 子进程白名单 = 真正"卡死"才会被杀。
- **Codex/Claude 双工**：失败时换对面，最后兜底是让 Claude 用自己工具直接做。

需要警惕的"债"：

- `run_paper.sh` 2218 行单文件——后续如果加 step 数量或子任务，可能要拆。
  本次改造步骤数大致不变（16→16），暂不拆。
- 进程白名单是 inline 硬编码而非常量——容易漏改，应在阶段 1 抽成函数或常量。
- Stata 相关知识渗透到很多 prompt 里（每个 prompt 几乎都让 agent 读 `analysis_guide.md`）。
  阶段 3 重写 prompt 时要注意全面替换语言。

以上理解请用户校对，确认无误再进入阶段 1。
