# 建模工厂 (Modeling Factory)

本项目是一个用于数学建模竞赛（如高教社杯 CUMCM、美赛 MCM/ICM 及类似应用建模竞赛）的本地多智能体工作流。它改编自原始的本地 Paper Factory，但当前活跃的工作流专注于：竞赛赛题解析、方法选择、数学建模、求解器执行、鲁棒性检验、论文草拟、模拟评委打分以及最终的打包提交。

原始的社会科学资产仅保留为历史参考，执行路径已经退役。现役建模流程遵循 `STEPS.md` 和 `modeling_guide.md`。

完整文档导航请查看 [`DOCUMENTATION_INDEX.md`](DOCUMENTATION_INDEX.md)。

## Web Dashboard

本项目提供 Web Dashboard，用于公开展示完成论文、管理用户与项目、实时监控进度并进行人工介入：

```bash
cd web
./start_dashboard.sh
```

然后在浏览器中访问 **http://localhost:5173**

启动前需在 `web/.env` 配置非敏感的 `GCP_PROJECT_ID`，并确保当前账号可从 GCP Secret Manager 加载必需 secret。系统没有默认管理员密码。

**主要功能：**

- **按用户授权的论文展厅**：管理员可分别配置未登录访客和注册用户可阅读的完成论文，展示权限不授予项目控制权。
- **注册与审批**：用户注册后处于 pending，管理员审批用户和项目申请；普通用户只能看到自己的 ACL 项目。
- **题目归档**：按项目内题目内容的规范化 SHA-256 标识聚合同题多次运行，同时保留 `ongoing/` / `complete/` 的真实目录状态。
- **实时监控**：WebSocket 自动推送项目状态、诊断、日志与阻塞原因。
- **比赛控制台**：默认用 8 个比赛阶段展示流程，可下钻查看 10 Stage 调度位置与 Step 0–16 验证合同；时间卡根据最近三步耗时预测内容冻结余量。
- **行动与人工节点**：顶部行动中心聚合人工 Gate、deadline、求解失败、审计事项和交付阻塞；三类人工决策分别显示适用证据、确认项、风险与不可变 revision。
- **证据与交付**：证据驾驶舱汇总 canonical results、PRIMARY/AUXILIARY、Solver receipts、阶段审计和三角色状态；交付中心只从 Final Audit 与原子 current release 提供 PDF/ZIP。
- **求解证据**：在项目工作区查看本地/云端 Solver 作业、终态、耗时与两阶段 receipt 完整性。
- **项目控制**：在权限范围内暂停、恢复或终止运行。
- **人工咨询与选择**：处理咨询请求；交互式项目可启用 Step 3 `PRIMARY/AUXILIARY` 选择门，CLI 路径仍然保留。

详细使用说明请参阅 [`web/README.md`](web/README.md)。

## TUI 客户端（终端）

除浏览器控制台外，仓库还提供终端客户端 `apps/tui/`，用于无图形界面或偏好键盘操作的场景。它是**只读客户端**，且不复制任何工作流逻辑：渲染的字段全部来自 `web/backend` 已提供的投影，因此终端与浏览器不会各自演进出两套契约。

先启动后端（`web/start_dashboard.sh`），再运行：

```bash
cd /home/tfisher/paper_factory
uv sync --extra tui     # 首次，安装 textual
./run_tui.sh            # 默认连 http://127.0.0.1:8000
./run_tui.sh --base-url http://其它主机:8000
```

登录屏需要真实账号；密码由你在登录屏输入，仅驻内存、不落盘、不写入日志或诊断。键位与刷新策略见 [`web/README.md`](web/README.md#终端客户端tui)。

## 包含内容

- `launch_agents.sh`：本地启动器，包含 `new`、`resume`、`pause`、`run`、`attach`、`trace` 和 `status` 等命令。
- `run_paper.sh`：Native 启动器；通过 `factory_core/` Python 引擎与 SQLite 运行 Stage。
- `factory_core/`：版本化 SQLite 状态、追加式事件、恢复、重试、Step 注册和执行适配器。
- `STEPS.md`：标准的数学建模工作流契约。
- `modeling_guide.md`：项目结构、求解器、LaTeX、图表生成及可复现性规范。
- `prompts/step*.txt`：工作流每个步骤的智能体提示词模板。
- `method_library/`：已注册的建模方法和可运行的种子模板。
- `solver_submit.sh`：异步本地求解器执行助手。
- `compile_paper.sh`：LaTeX 辅助脚本，选择 `xelatex` 编译中文/国赛风格论文。
- `scripts/`：辅助脚本，用于 Antigravity 路由、MinerU 解析、数字校验和清理工作。
- `evaluation/`：评分解析器以及针对外部大语言模型（LLM）裁判的基准校准脚本。
- `experiments/`：消融实验测试工具，用于测试不同流程机制对结果的影响。
- **`apps/tui/` 与 `run_tui.sh`**：Web 控制面的只读终端客户端（Textual），监控项目状态、阻塞原因与日志尾随。
- **`docs/guides/`**：优秀论文基准文档（可视化与写作规范）

诸如 `analysis_guide.md`、`stata_submit.sh` 和 `stata_wrapper.sh` 等旧文件已迁出本仓库（登记于 `docs/architecture/EXPERIMENTAL_CODE_SPLIT.json`，历史副本见归档目录），不再构成可执行社会科学工作流。新建模项目请遵循 `modeling_guide.md` 并使用 `solver_submit.sh`。

## 前置要求

- 安装并认证 `codex` CLI。
- 安装并认证 `claude` CLI（如果使用 Claude 备用路由）。
- Python 3 环境，使用本仓库目录下 `.venv` 虚拟环境中的依赖项。
- `uv` 用于按 `uv.lock` 创建 Python 环境；Node.js/npm 用于按 `web/frontend/package-lock.json` 创建 Web 前端环境。
- LaTeX 工具链：`xelatex`、`pdflatex`、`bibtex` 和 `biber`；CI 使用独立 `latex` 作业执行真实三轮 recorder 与 bibliography 反例测试，禁止因工具缺失跳过。
- 至少一套用于项目代码的实用求解器技术栈，通常为带有 `numpy`、`scipy`、`pandas` 和 `matplotlib` 的 Python 环境。
- 当前生产部署通过 GCP Secret Manager 和 `scripts/load_secrets.sh` 注入 MinerU、模型 API、JWT 与管理员凭据；本地 `.env` 只应保留非敏感配置。
- 可选：如果项目需要，可安装 Julia、MATLAB/Octave、R、Gurobi 或其他求解器。

不要在文档、命令历史或仓库文件中保存 secret 值。Secret Manager 操作见 [`docs/SECRET_MANAGER_GUIDE.md`](docs/SECRET_MANAGER_GUIDE.md)。

## 快速开始

克隆仓库，进入目录，按锁文件准备环境并检查启动器：

```bash
git clone <repo-url> mathmodel-factory
cd mathmodel-factory
uv sync --extra web --extra models --locked
(cd web/frontend && npm ci)
chmod +x launch_agents.sh run_paper.sh compile_paper.sh solver_submit.sh
./launch_agents.sh status
```

启动器会根据需要创建运行时目录：

- `ongoing/`：进行中的项目。
- `complete/`：已完成的项目。
- `papers/`：最终生成的 PDF 论文及提交用的压缩包。
- `logs/` 和 `run_state/`：进程状态与日志。

这些运行输出会被 Git 自动忽略。

## 当前主流程（2026-09-16）

主仓库只运行 **FactoryEngine + SQLite + Native Stage**。Authority、Phase 3–9、
shadow UI/API 和历史 Bash runner 已拆至 `~/paper_new`，完整基线与迁移清单均已保留。
详见 [主流程边界](docs/architecture/NATIVE_MAINLINE.md)。现有运行数据和其他 worktree 未迁移。

## 创建建模项目

对于竞赛用途，可以使用赛题的 PDF 或 Markdown 绝对路径来初始化项目。这会触发建模模式的设置过程，包括生成 `problem/` 解析结果。

```bash
./launch_agents.sh new --no-start test_cumcm2024b \
  "/absolute/path/to/problem.pdf"
```

然后恢复工作流运行：

```bash
./launch_agents.sh resume test_cumcm2024b
```

需要以官方截止时间为权威时间时，可直接使用 Native CLI（支持 epoch 秒或带时区 ISO-8601）：

```bash
python3 -m factory_core.cli create test_cumcm2024b \
  "/absolute/path/to/problem.pdf" --contest-deadline "2026-09-13T20:00:00+08:00"
```

调试时可在前台运行：

```bash
./launch_agents.sh run test_cumcm2024b
```

所有新 `contest_core_v1` 项目都会在 Step 3 前生成选择请求并暂停；旧项目仍保留 `selection/config.json` 的 opt-in 行为。可在终端中查看候选后选择：

```bash
python3 scripts/selection_gate.py select-step3 ongoing/test_cumcm2024b \
  --primary m2 --aux m1 --reason "Prefer heuristic contrast"
```

该命令把结构化决策写入 SQLite，并生成 `selection/step3_decision.json` 和 `human_review.md` 投影，默认恢复项目运行；调试时可加 `--no-resume`。

Step 16 前必须完成第二个人工节点，检查主结论、摘要与核心图表后冻结内容：

```bash
python3 scripts/selection_gate.py approve-content-freeze \
  ongoing/test_cumcm2024b --reason "Conclusions, abstract and figures reviewed"
```

正常工作流在 T−6h 后只保留 Final Audit 与交付；T−2h 后若审计要求回退，
还必须通过 Web 或 `approve-delivery-freeze-override --reason ...` 明确授权。

检查状态：

```bash
./launch_agents.sh status
```

工作流会在 Step 4、5/6 和 10 后分别运行 `model`、`results` 和 `paper`
增量审计。也可以手动运行；这些 profile 只记录阶段就绪状态，永远不授权交付：

```bash
python3 -m factory_core.cli audit ongoing/test_cumcm2024b --profile model
python3 -m factory_core.cli audit ongoing/test_cumcm2024b --profile results --checkpoint-step 5
python3 -m factory_core.cli audit ongoing/test_cumcm2024b --profile results --checkpoint-step 6
python3 -m factory_core.cli audit ongoing/test_cumcm2024b --profile paper
```

Step 15 完成后，项目内容达到 `CONTENT_READY` 边界。此时可独立运行最终审计，
该命令默认是 `analysis_only`：会写审计与 Judge 分析证据，但不会创建
`final_submission.sha256`、override/acceptance receipt，也不会发布、打包、归档或修改工作流状态：

```bash
python3 -m factory_core.cli audit ongoing/test_cumcm2024b
```

阶段审计写入 `.factory/audits/profiles/<profile>/<snapshot>/`，最终审计继续写入
`.factory/audits/<snapshot>/`。同一模式、同一快照已有可验证 PASS 时会复用；
使用 `--no-reuse` 可强制新审计，最终 profile 可用 `--no-compile` 审计现有 PDF。
分析结果另投影到 `.factory/audits/analysis_latest.json`；对同一快照重跑只读分析
不会覆盖已验证 acceptance 的 `.factory/audits/latest.json`。
Step 16 才显式请求 acceptance；Native Adapter
调用同一服务的 acceptance 模式。该请求只恢复 Phase1–8/legacy 交付合同；任何真正的
Phase9 acceptance、release、submission 或 delivery 副作用仍永久 fail closed。

跟踪运行日志：

```bash
./launch_agents.sh attach test_cumcm2024b
```

## 在项目中使用求解器

在 `ongoing/<base>/` 或 `complete/<base>/` 目录内，智能体和人员都应该通过 `solver_submit.sh` 来运行复杂的求解任务：

```bash
../../solver_submit.sh --type python --max-time 600 models/m3_milp/03_solve.py
../../solver_submit.sh --status <jobid>
../../solver_submit.sh --wait <jobid>
```

支持的类型包括 `python`、`julia`、`matlab`、`R` 和 `gurobi`，前提是本地已安装相应的环境。

必要时可手动编译论文：

```bash
../../compile_paper.sh "$(pwd)" <base_name>
```

## 工作流概览

活跃的建模工作流包含设置步骤、后续的 1-16 个主步骤，以及插入在 Step 8 和 Step 9 之间的 Step 8.5 辅助 gate：

- 设置 / Step 0：将赛题解析至 `problem/` 目录。
- Step 1：背景调研及方法预选。
- Step 2：并行生成建模方案及示例求解。
- Step 3：方法选择。带 `contest_core_v1` policy 的新 Native 项目必须先暂停，让用户在 Step 2 验证过的候选流中选择 `PRIMARY/AUXILIARY`；无 policy 的兼容项目仍可由 `selection/config.json` 显式启用。SQLite 决定是权威，`human_review.md` 是可重建投影而不是人工覆盖通道。
- Step 4：构建完整模型，并运行 `model` profile 审计。
- Step 5：执行完整求解过程，并运行 Step-5 `results` profile 审计。
- Step 6：敏感性与鲁棒性分析，并以新快照运行 Step-6 `results` profile 审计。
- Step 7：模型评估。
- Step 8：数据可视化润色。
- Step 8.5：阅卷入口设计。为每个子问题定义评委入口三句式、主图/主表锚点和正文首段承接提纲。
- Step 9：撰写论文初稿。
- Step 10：门禁1 - 运行 `paper` profile，检查论文数字、代码、结果与交付附件一致性；符号检查先记 warning。
- Step 11：建设性审稿。
- Step 12：论文修订。
- Step 13：运行隔离的数学预审；`PRECHECK_PASS` 只允许继续写摘要和润色，不代表最终质量 PASS。
- Step 14：撰写摘要。
- Step 15：引用、图表及排版润色；任何修改都会使 Step 13 的预提交结果失效。通过校验后形成 `CONTENT_READY` 内容边界。
- Step 16：Native 适配器调用独立 Final Audit，并在副作用边界显式请求 acceptance。依次完成最终编译、完整论文/溯源检查、视觉页数门禁、三角色 Judge、快照复核及 judgment/acceptance receipt。只有Native 工作流的审计 PASS，或管理员在 `web/auth.db` 中签发并绑定精确快照的 `OVERRIDDEN` 结果，才会在同文件系统 staging 中构建不可变 release，并以一次原子替换切换 `papers/<base>/current.json` 后归档。真正的 Phase9 acceptance、release、submission 和 delivery 永久禁用；项目内 override JSON 没有授权能力。

完整的详细步骤要求请参阅 `STEPS.md`。这些文件仍是产物与验证契约；项目的运行状态权威见下节。

## 注意事项

- 新项目和已迁移项目以 `.factory/state.db` 的版本化快照与追加式事件为工作流状态权威；产物文件是 Step 校验依据。`run_paper.sh --infer-step <project_dir>` 对这些项目读取 SQLite。
- 无 Native 状态的旧项目仅供历史查看；不要手工创建或改写 `.factory/state.db` 来绕过边界。
- 已完成的项目将从 `ongoing/` 移至 `complete/`。
- 独立审计模块同时负责阶段化确定性审计和最终发布分析。Step 13 只调用数学角色；最终 profile 才运行数学、执行和论文质量三角色 Judge、判决路由与指纹。默认 CLI final audit 只生成分析记录和 judgment receipt；`judge_outputs/final_submission.sha256`、override receipt 和 final acceptance receipt 只在 Step 16 显式 acceptance 模式且安全 fence 通过后创建。兼容文件继续投影到 `judge_outputs/`。审计本身不会写 `papers/`、打包、清理或归档；Step 16 消费获准的Native acceptance 结果后才执行这些交付动作。
- 交付权威是 `papers/releases/<base>/<snapshot>/` 与原子指针 `papers/<base>/current.json`；顶层同名 PDF/ZIP 仅为兼容副本。任何 staging、打包或校验失败都不会切换旧的 current release。
- `complete/` 是历史交付目录，不等价于“符合当前最新契约”。使用 `python3 scripts/audit_complete_projects.py --write-manifests` 生成 `complete/_validation_index.json`，将项目分为 `CURRENT_PASS`、`LEGACY_DELIVERED` 和 `INVALID_OR_INCOMPLETE`。

## 旧项目处理

无 Native SQLite 状态的项目仅供历史查看。旧 Native `step_v2` 项目需停止后显式激活 Stage：

```bash
python3 -m factory_core.cli migrate scheduler-activate ongoing/<base> --expected-revision <revision>
```

主仓库不再提供 Legacy 导入或回退，也不转换 Authority/Phase 数据库；历史恢复工具位于
`~/paper_new`，应在独立副本中使用。`run_paper.sh --infer-step` 只读取 Native SQLite 状态。

## 评测与消融实验

代码库包含完整的测试和验证工具集：

### 外部评估系统

**`evaluation/`** 目录提供独立的有效性门禁与条件论文质量评估框架：

- 数学审计和执行审计使用 `PASS / FAIL / INDETERMINATE` 三值硬门；任一非 PASS 都不能被论文分数抵消。
- 只有两个硬角色 PASS 后，才解释六维论文质量：模型呈现、求解叙事、创新性、写作清晰度、结果说服力、敏感性与局限。
- 自动 paper 角色只评价 LaTeX / 文本可观察的结构、论证、图题 / 表题及正文中的图表叙事；分页、字体、颜色、图像清晰度、裁切和真实版式不进入当前自动分数，必须由编译 / 版式机器预检或人工查看最终 PDF。
- 角色输出和聚合结果使用严格版本化 JSON；缺失、格式错误或证据不足均降为 `INDETERMINATE`。
- 每个 packet manifest 还执行确定性的 `judge-packet-completeness-v1`：paper 必须完整包含最终论文与主问题文本；math 必须完整包含问题、最终论文和主要数学阐述；execution 必须完整包含最终论文、主要结果、实现代码与执行轨迹。任一关键项被截断 / 省略都会由聚合器强制将对应角色改为 `INDETERMINATE`，模型自身不能宣告 PASS 绕过。非关键大型代码可截断，但会在 manifest `limitations` 中披露。
- 默认 K=3 只用于重复执行同一评审契约并暴露不一致。当前 API 路径使用 temperature=0，因此这些运行不是独立统计重复，min/max spread 也不是置信区间。
- `proxy_reliability` 只诊断 paper-only 配对 harness；它不能自动赋予运行时评分可比性。`comparison_ready_proxy/human` 还要求 manifest 明确验证精确的 `judge-role-v1` 与 `modeling-factory-judge-packet-v2` 构造，并分别具备代理或人类真值支持。人工校准未 READY 前，不得解释绝对分或预测奖级。
- 旧 Markdown 评分卡统一为 `LEGACY_UNVERIFIED`，只能诊断查看，不能与当前 `judge-aggregate-v1` 结果横比。

```bash
# 评估已完成的项目
./evaluation/run_evaluation.sh complete/test_cumcm2024b --samples 3

# 对比多个项目
python3 experiments/compare_ablations.py \
    --baseline test_cumcm2024b \
    --variant cumcm2024b_no_judge_rep1
```

详见：`evaluation/README.md` 和 `evaluation/baseline_scores.md`

### 消融实验

**`experiments/`** 目录提供系统化的机制验证工具：

通过环境变量选择性关闭流水线机制，探索各组件可能造成的差异。下表是旧评委契约下、单题且每条件仅一次生成的**历史观察值**，已降级为 `LEGACY_UNVERIFIED`，不能证明因果贡献或统计显著性：

| 消融开关 | 关闭的机制 | 历史观察差值（不可作当前比较） |
|---|---|---|
| `ABLATE_NO_METHOD_LIB=1` | 方法库引用硬门 | -6.3（历史读数） |
| `ABLATE_NO_JUDGE=1` | Step 13 评委 + reopen循环 | -3.4（历史读数） |
| `ABLATE_NO_INNOVATION_PROTECT=1` | PROTECTED标记保护 | -2.7（历史读数） |
| `ABLATE_NO_CONSULTATION=1` | Step 1 web文献检索 | -1.7（历史读数） |

重新形成可用消融结论至少需要：当前三角色 schema、最终稿指纹一致、人工校准可比性、多题目、每条件多个生成重复，以及将生成方差与评委重复分开。

**快速启动消融实验**：

```bash
# 在指定题目上运行单个消融（自动生成项目、运行、评估）
./experiments/ablation_no_judge.sh --problem B --reps 3

# 运行所有四个消融并生成对比报告
./experiments/test_ablations.sh  # 先验证开关生效性（秒级）
./experiments/ablation_no_method_lib.sh --problem B --reps 1
./experiments/ablation_no_judge.sh --problem B --reps 1
./experiments/ablation_no_innovation_protect.sh --problem B --reps 1
./experiments/ablation_no_consultation.sh --problem B --reps 1
```

详见：
- **综合报告**: `evaluation/ablation_study_report.md` — 完整的实验设计、结果分析和洞察
- **实验状态**: `evaluation/EXPERIMENTS_STATUS.md` — 当前进度和后续任务路线图
- **实验指南**: `experiments/README.md` — 消融开关的实现细节和使用文档
