# Modeling Factory 改造计划

> 本文档是 Paper Factory → Modeling Factory 改造的工作契约。
> 来源：用户与外部 LLM 讨论后整理的指令文档（2026-05-18）。
> 第 4 阶段的"外部咨询协议"细节待用户后续补充设计要点（用户暂忽略）。

## 总体说明

将 Paper Factory（一个为社科论文自动化设计的多智能体工作流）改造成
"Modeling Factory"——针对数学建模比赛（美赛 MCM/ICM、国赛 CUMCM、华为杯等）
的工作流系统。

### 核心改造原则

1. 保留原仓库优秀的工程基础设施（launcher、runner、snapshot、heartbeat、
   infer_step、audit ledger 等），不要重写。
2. 主要工作是替换领域特定的部分：prompts、STEPS 契约、analysis_guide、
   求解器调度层。
3. 渐进式改造，分 5 个阶段，每个阶段完成后系统应能跑通至少一道简单建模题。
4. 始终保持原仓库的 file-state authoritative 设计原则——通过磁盘文件
   判断状态，而非 checkpoint。
5. 不要破坏 runner snapshot 机制——run_paper.sh 的修改可以边跑边改。

每个阶段结束时停下来等用户验证后再进入下一阶段。

---

## 阶段 0：环境准备与基线验证（半天到一天）

**目标**：跑通原始 Paper Factory，确认基础设施工作，建立改造的基线。

### 任务清单

- **0.1 Fork 原仓库到 GitHub，clone 到本地工作目录**
  - 新仓库地址：`https://github.com/xhxmt/mathmodel-factory.git`
  - 创建分支 `modeling-factory`，所有改造在此分支进行
  - 在 README.md 顶部加一段说明这是 fork 及改造目标

- **0.2 安装依赖、配置 API key**
  - 验证 Codex CLI 可用
  - 验证 Claude CLI 可用
  - 在 ~/.bashrc 或项目 .env 中配置 API key（不要 commit）

- **0.3 跑通原始的 demo 项目**
  - 用一个简单的研究问题测试，如 "What predicts wage inequality in US cities?"
  - 用任意公开数据集（如 IPUMS CPS 小样本）作为输入
  - 完整跑完一遍 16 步，确认产生 PDF
  - 这是为了验证基础设施，不追求结果质量

- **0.4 阅读关键文件并写一份理解笔记 my_understanding.md**
  - launch_agents.sh 的 CLI 接口
  - run_paper.sh 的 step 调度逻辑
  - STEPS.md 的 16 步契约
  - analysis_guide.md 的样式规范
  - prompts/ 目录的组织
  - 把理解和疑问写下来供用户确认

### 验收标准

- 原始 Paper Factory 能跑通至少一个完整项目
- `my_understanding.md` 中的理解经用户确认无误

---

## 阶段 1：基础设施迁移（1-2 周）

**目标**：把核心执行层从 Stata 改成多求解器调度。这一步不改 prompts，
先确保底层工程能跑数学建模任务。

### 任务清单

- **1.1 创建 solver_submit.sh，替代 stata_submit.sh**

  位置：仓库根目录

  接口要求（保持与 stata_submit.sh 类似的 CLI）：
  - `./solver_submit.sh --type python script.py`
  - `./solver_submit.sh --type matlab script.m`
  - `./solver_submit.sh --type julia script.jl`
  - `./solver_submit.sh --type gurobi model.lp`
  - `./solver_submit.sh --status <jobid>`
  - `./solver_submit.sh --wait <jobid>`

  实现要点：
  - 复用 stata_submit.sh 的 nohup + jobid 机制
  - jobid 格式: `local_<TYPE>_<TIMESTAMP>_<PID>`
  - 元数据写入 `run_state/solver_jobs/<jobid>.meta`
  - 增加 `--max-time` 参数（强制时间上限，重要！建模比赛需要时间预算管理）
  - log 文件输出到 `<script>.log` 同目录，由 agent 自行移到 logs/
  - 状态检测策略（不同求解器不同）：
    - python: 检测进程结束 + 退出码
    - matlab: 检测 "Total time" 或异常关键词
    - gurobi: 检测 "Optimal solution found" 或 "infeasible"
    - julia: 检测进程结束

  保留 stata_submit.sh 不删除，便于回滚。

- **1.2 更新 run_paper.sh 中的 hang detection 进程白名单**

  原 `STATA_PROCS="stata|srun|python|Rscript|R|julia"`
  改为：`MODELING_PROCS="python|julia|matlab|R|gurobi_cl|cplex|scip|ipopt|octave"`

- **1.3 创建 modeling_guide.md（替代 analysis_guide.md，原文件保留作参考）**

  位置：仓库根目录

  内容大纲（先写一个最小可用版本）：
  - **项目文件结构**（参考原 analysis_guide.md 的目录约定）
    - `problem/`  题目原文 PDF 和解析
    - `data/raw`, `data/intermediate`, `data/final`
    - `models/`  按建模流分子目录（m1_opt, m2_ode, m3_ml, ...）
    - `scripts/` 求解代码
    - `figures/`, `tables/`, `logs/`
    - `paper/`   LaTeX 源文件
  - **数学符号规范**
    - 变量斜体、向量粗体斜体、矩阵粗体直立
    - 集合花体、概率 ℙ、期望 𝔼
    - 维度声明：文中所有变量必须在符号说明表中列出
  - **LaTeX 模板要求**
    - documentclass: article（美赛用 mcmthesis 模板，国赛用国赛模板）
    - 必含章节：摘要、问题重述、问题分析、模型假设、符号说明、
      模型建立、模型求解、灵敏度分析、模型评价、参考文献、附录
  - **图表规范**
    - 色板：学术风格（避免 Paper Factory 那种商业蓝/品红）
      建议：`#2E5C8A`, `#C04D4D`, `#4D9D5B`, `#D49B3E`, `#6B4D9A`
    - 字体：Times New Roman 正文 + Computer Modern 公式
    - 图注完整自洽（不依赖正文也能读懂）
    - 算法用 algorithm2e 包
  - **代码规范**
    - 每个模型一个文件夹
    - 每个文件头部注释：作者 agent、对应模型、输入输出
    - 随机种子固定（便于复现）
  - **求解器调用规范**
    - 必须用 solver_submit.sh，不直接 nohup
    - 大规模任务必须设 `--max-time`
    - 中间结果定期 checkpoint

- **1.4 在 run_paper.sh 中：把所有引用 analysis_guide.md 的地方改为 modeling_guide.md**

  搜索范围：render_prompt 函数，prompts/*.txt 的预读列表。
  保持向后兼容：如果项目目录里同时有两个文件，优先 modeling_guide.md。

- **1.5 测试基础设施**
  - 创建一个 hello world 测试：用 Python 求解一个简单 LP
  - 通过 `solver_submit.sh --type python` 提交
  - 验证 jobid 生成、status 检测、log 输出、`--wait` 阻塞正常
  - 测试 `--max-time` 强制超时

### 验收标准

- `solver_submit.sh` 在 python/julia/matlab 三种类型下都能正常提交、查询、等待
- 原有的 launch_agents.sh、run_paper.sh CLI 仍然工作
- `modeling_guide.md` 内容完整、与原 analysis_guide.md 风格一致

---

## 阶段 2：题目解析与方法库（1-2 周）

**目标**：建立建模比赛特有的"Setup 阶段"和"方法知识库"。
这是最有原创性的两个模块。

### 任务清单

- **2.1 创建方法库目录 `method_library/`**

  位置：仓库根目录

  结构：
  ```
  method_library/
    README.md                    # 方法库索引和使用说明
    evaluation/
      ahp.md                     # AHP 层次分析法
      topsis.md
      entropy_weight.md
      pca.md
      dea.md
    optimization/
      lp.md
      milp.md
      nlp.md
      robust_opt.md
      stochastic_opt.md
      multi_objective.md
    prediction/
      arima.md
      lstm.md
      prophet.md
      grey_model.md              # 灰色预测（国赛常用）
    classification/
      logistic.md
      svm.md
      random_forest.md
      xgboost.md
    dynamics/
      ode_system.md
      pde.md
      agent_based.md
      system_dynamics.md
    network/
      shortest_path.md
      max_flow.md
      complex_network.md
    metaheuristic/
      ga.md
      pso.md
      sa.md
  ```

  每个方法文件的模板（先写 3-5 个示例，剩下的可后续补充）：

  ```markdown
  # [方法名]

  ## 适用场景
  [什么类型的问题最适合用这个方法]

  ## 核心假设
  [使用该方法的前提假设]

  ## 数学形式
  [标准的数学表达]

  ## 求解工具
  [推荐的库和求解器，如 Gurobi/scipy/cvxpy]

  ## 代码模板
  [一个可运行的最小示例]

  ## 常见陷阱
  [使用时容易犯的错]

  ## 在建模比赛中的典型应用
  [历年类似题目举例]

  ## 参考文献
  ```

  优先实现的 5 个方法（最常用）：
  - `evaluation/ahp.md`
  - `evaluation/topsis.md`
  - `optimization/milp.md`
  - `prediction/arima.md`
  - `dynamics/ode_system.md`

- **2.2 创建题目解析 prompt: `prompts/step0_problem_parsing.txt`**

  这个 prompt 替代原 Paper Factory 中简单的 project_brief 生成。
  输入：checkpoint.md 中的题目（可能是 PDF 路径或题目文本）
  输出多个文件：
  - `problem/problem_brief.md`        题目重述、子问题分解
  - `problem/terminology_table.md`    模糊术语 → 精确定义
  - `problem/data_inventory.md`       给定数据清单 + 缺失数据 + 建议来源
  - `problem/feasibility_constraints.md`  时间预算、提交格式、字数限制
  - `problem/candidate_methods.md`    基于题目特征从 method_library 推荐的方法

  prompt 关键指令：
  - 必须阅读 `method_library/README.md` 了解可用方法
  - 必须显式列出"模糊术语"——这是建模题目最容易出错的地方
  - 必须区分硬约束（题目明确要求）和软约束（评分倾向）
  - 必须估算每个子问题的时间预算（按总预算 96 小时回推）
  - 输出格式严格遵循上述四个文件，便于下游 agent 消费

- **2.3 修改 run_paper.sh 的 step 0 调度**

  原来的 step 0 只调一个 agent 生成 project_brief.md
  现在改为：
  - 调用 step0_problem_parsing.txt
  - 验证四个输出文件都生成且不为空
  - 如果输入是 PDF，先用合适的工具（pdftotext/pdf2md）转成文本

- **2.4 测试题目解析模块**
  - 用一道公开的历年美赛题作为输入（如 2024 MCM Problem A）
  - 验证四个输出文件的质量
  - 由用户检查产出，反馈给迭代 prompt

### 验收标准

- `method_library/` 至少 5 个方法的详细文档完成
- 题目解析模块在 2024 MCM Problem A 上的产出经用户审核合理
- 四个输出文件结构清晰、内容具体（不是泛泛而谈）

---

## 阶段 3：核心工作流重构（2-3 周）

**目标**：重写 STEPS.md，把 16 步从"社科实证发现"改造成"建模论文生成"。
这是最大的一块工作。

### 任务清单

- **3.1 设计新的 STEPS.md（替代原文件，原文件改名 STEPS_original.md 保留参考）**

  新流程草案（具体细节可在实现中调整）：

  **Setup (步骤 -1 → 0)**：读题目，生成 problem_brief.md 等 4 个文件

  **Step 1: 背景研究 + 方法预选**
  - 文献检索（类似 Paper Factory 的 deep research，但聚焦建模思路）
  - 基于 problem_brief 在 method_library 中筛选 N 个候选建模流
  - viability gate：评估候选方法在数据/时间约束下是否可行
  - 输出：`research_brief.md`, `viable_streams.md`

  **Step 2: N 路并行建模方案**（替代原 6 路 findings）
  - N 路并行，N 由 Step 1 决定（通常 3-5 路）
  - 每路：独立建模 + 小规模 demo 求解 + 可行性报告
  - critic loop：检查假设强度、求解可行性、创新空间
  - 每路输出：`m<N>_spec.md`, `m<N>_demo_result.json`, `m<N>_critique.md`
  - 命名：`m1_`, `m2_`, `m3_`, ... 类似原 `f1_`, `f2_`

  **Step 3: 方法选择**（关键人类介入点）
  - decider agent 整合所有候选方案的优劣
  - **强制人类介入**：写 consultation_request 等待人决策
  - 目标函数：创新性 × 可行性，不只看可行性
  - 允许选择 1 主方法 + 1 辅助方法（建模常见做法）
  - 输出：`chosen_method.md`, `method_rationale.md`

  **Step 4: 完整模型构建**
  - 基于 chosen_method 展开完整数学建模
  - 强制产出：`symbol_table.md`（全文符号表），
    `assumption_ledger.md`（假设清单，类似原 audit_issue_ledger）
  - 多子问题情况下模型衔接的显式说明
  - 代码完整实现 + 单元测试式 sanity check

  **Step 5: 完整求解**
  - 调用 solver_submit.sh 提交求解任务
  - 大规模任务必须 --max-time 限制
  - 结果存到 results/，按子问题组织

  **Step 6: 灵敏度分析 + 稳健性**
  - 参数扰动实验
  - 假设松弛的影响
  - 输出：`sensitivity_report.md`, sensitivity figures

  **Step 7: 模型评价**
  - 优点、缺点、推广可能
  - 与候选方法的比较（为什么选了这个，其他能不能做得更好）

  **Step 8: 可视化打磨**（独立步骤，建模比赛图表权重高）
  - 重画所有图表，遵循 modeling_guide.md 的视觉规范
  - 每张图独立可读，caption 自洽

  **Step 9: 论文起草**
  - 完整 LaTeX 起草
  - 摘要预留 ABSTRACT_PLACEHOLDER
  - 必含章节按 modeling_guide.md 要求

  **Step 10: Gate 1 数值与代码一致性核查**
  - 论文中所有数字必须能在 logs/ 中追溯到
  - 类似原 Step 8 的 verify_numbers.py

  **Step 11: 建设性审稿**
  - 类似原 Step 9
  - **关键改动**：在 audit_issue_ledger.md 中支持 PROTECTED 标签，
    标记为 PROTECTED 的创新性主张，修订时不得删除/降级

  **Step 12: 修订**
  - 基于审稿意见修订
  - 严格遵守 PROTECTED 标签

  **Step 13: Gate 2 评委模拟**（新增，无原型）
  - 多 agent 扮演评委按真实评分表打分
  - 5-6 个维度：建模合理性、求解正确性、创新性、写作清晰度、结果说服力、灵敏度
  - 输出：`judge_evaluation.md`（每个维度分数 + 评语 + 改进建议）
  - 触发条件：总分低于阈值时 reopen 修订（参考原 Step 11 的 reopen 机制）

  **Step 14: 摘要撰写**（关键人类介入点）
  - 严格四段式：问题理解 / 方法 / 结果 / 亮点
  - **强制人类介入**：摘要权重太高，必须人审定
  - 替换 ABSTRACT_PLACEHOLDER

  **Step 15: 引文审计、格式化、去机器人化**（类似原 Step 12-15）

  **Step 16: 编译、附录整理、打包提交**
  - 生成最终 PDF
  - 整理代码附录
  - 移动 ongoing/ → complete/
  - 准备提交用的压缩包

- **3.2 为每个新 step 编写 prompts/stepN.txt**

  优先级顺序（先写最关键的）：
  1. `step0_problem_parsing.txt`（阶段 2 已完成）
  2. `step3_method_selection.txt`（人类介入点，复杂）
  3. `step4_model_construction.txt`
  4. `step5_solve.txt`
  5. `step6_sensitivity.txt`
  6. `step13_judge_simulation.txt`（新增的评委模拟，最有原创性）
  7. `step14_abstract.txt`
  8. 其他 step 可后续完善

  每个 prompt 必须：
  - 读 modeling_guide.md 作为风格规范
  - 读 problem/*.md 了解题目
  - 读 method_library/ 中相关方法文档
  - 读 human_review.md 如果存在
  - 明确输出文件清单和格式
  - 明确成功标准（infer_step 用什么判断）

- **3.3 更新 run_paper.sh 的 infer_step 函数**

  根据新 STEPS.md 重新定义每步的输出文件检测逻辑。
  保留原有的 file-state authoritative 设计。
  保留 review_state 重写机制。

- **3.4 测试单步骤**

  用阶段 2 中的 2024 MCM 问题继续推进。
  一步一步跑，每步验证后再继续。
  重点测试 Step 3 的人类介入机制。

### 验收标准

- 新 `STEPS.md` 经用户审阅认可
- 至少 Step 0-6 在测试题目上跑通
- Step 3 的人类介入机制工作正常（能 pause 等待用户决策）

---

## 阶段 4：人机协作与外部咨询协议（1-2 周）

> **注**：本阶段细节用户暂忽略，待阶段 3 完成后再展开设计。
> 大致涵盖：External Consultation Protocol、pause_for_consultation 原语、
> notify_human 通知机制（Telegram Bot 优先）、consultation 模板库、
> 咨询预算管理、launch_agents.sh 咨询相关子命令。

---

## 阶段 5：评估与打磨（持续进行）

**目标**：建立质量评估基准，迭代优化 prompts，准备研究论文。

### 任务清单

- **5.1 建立 benchmark 测试集**

  创建 `benchmark/` 目录，包含：
  - 5-10 道历年真题（2018-2024 MCM/CUMCM/华为杯）
  - 每道题：题目 PDF、官方/参赛者数据、若干公开的获奖论文作为对照
  - 一份 `benchmark/scoring_rubric.md` 定义评分标准

  数据来源：
  - MCM/ICM: https://www.contest.comap.com/
  - CUMCM: 中国数学建模网
  - 优秀论文：各类建模论文集

  注意版权：不要 commit 受版权保护的论文 PDF，只 commit 题目和数据。

- **5.2 建立评估流程**

  创建 `evaluation/` 目录：
  - `run_evaluation.sh`: 对一个项目自动跑评估
  - `human_rubric.md`: 给人类评审用的评分表
  - `llm_judge_prompt.txt`: 让另一个 LLM 作为评委评分

  评估维度（与 Step 13 的评委模拟对齐）：
  1. 建模合理性 (20%)
  2. 求解正确性 (20%)
  3. 创新性 (20%)
  4. 写作清晰度 (15%)
  5. 结果说服力 (15%)
  6. 灵敏度分析 (10%)

- **5.3 系统化 prompt 迭代**

  创建 `prompts/CHANGELOG.md` 记录每个 prompt 的版本演化。
  每次重要修改：
  - 记录修改前后差异
  - 记录在哪道题目上的效果变化
  - 标记 "deprecated" 的版本，但保留以便回溯

- **5.4 消融实验脚本**（研究阶段）

  创建 `experiments/` 目录：
  - `ablation_no_consultation.sh`    禁用外部咨询的运行
  - `ablation_no_method_lib.sh`      禁用方法库的运行
  - `ablation_no_judge.sh`           禁用评委模拟的运行
  - `ablation_no_innovation_protect.sh`  禁用创新保护的运行

  每个变体在相同题目上各跑 3 次，统计评分差异。

- **5.5 准备研究论文素材**

  创建 `paper_research/` 目录：
  - `notes/` 实验中的观察和洞见
  - `data/` 评估结果数据
  - `figures/` 论文用图
  - `draft.md` 论文草稿

  论文核心贡献候选：
  - 建模比赛领域的 Paper Factory 改造方案
  - 人机协作介入点的实证研究
  - 外部咨询协议的可行性验证
  - prompt 库本身作为公共贡献

### 验收标准

- 在 5 道 benchmark 题目上完整跑通
- 至少完成一次消融实验
- 评估结果数据可用于支撑研究论文

---

## 整体进度管理

### 关键里程碑

- **里程碑 1**（阶段 0-1 完成，约 2 周）：
  基础设施跑通，能调度多求解器，能跑一个简单测试题

- **里程碑 2**（阶段 2 完成，约 4 周）：
  题目解析和方法库就绪，能从一道真实建模题生成结构化的项目设置

- **里程碑 3**（阶段 3 完成，约 7 周）：
  完整建模工作流跑通一道历年真题，产出可读的论文 PDF

- **里程碑 4**（阶段 4 完成，约 9 周）：
  人机协作机制就绪，可实战参赛

- **里程碑 5**（阶段 5 完成，持续）：
  研究层面的实证数据积累，论文素材成型

### 风险管理

1. 不要一次大改 prompts。每改一个 prompt 跑一次测试。
   原仓库的 `prompts/CHANGELOG.md` 习惯一定要继承。

2. 保留所有原始文件的备份（改名为 `*_original.md`），方便对比和回滚。

3. git commit 必须细粒度，每个有意义的修改单独 commit。
   建议每个阶段在分支上完成后再 merge。

4. 不要在用户没确认的情况下删除 `method_library/`、`prompts/`、
   `consultation_templates/` 中的内容——这些是知识资产。

5. 任何对 `run_paper.sh`、`launch_agents.sh` 的修改都要保留
   snapshot 机制不被破坏。

### 沟通约定

每个任务完成后向用户汇报：
- 完成了哪些文件的修改/创建
- 测试了什么（具体命令和结果）
- 遇到的问题或不确定的设计选择
- 下一步建议

用户确认后再继续下一步。不要主动跳到未确认的阶段。

### 阶段 git tag 规范

每阶段结束做 git tag：`v0.1-baseline`、`v0.2-solver`、`v0.3-parsing` 等，
方便回滚和论文中引用版本。
