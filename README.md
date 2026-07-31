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

- **公开论文展厅**：未登录访客只能阅读 `SHOWCASE_PROJECTS` 白名单中的完成论文。
- **注册与审批**：用户注册后处于 pending，管理员审批用户和项目申请；普通用户只能看到自己的 ACL 项目。
- **题目归档**：按项目内题目内容的规范化 SHA-256 标识聚合同题多次运行，同时保留 `ongoing/` / `complete/` 的真实目录状态。
- **实时监控**：WebSocket 自动推送项目状态、诊断、日志与阻塞原因。
- **项目控制**：在权限范围内暂停、恢复或终止运行。
- **人工咨询与选择**：处理咨询请求；交互式项目可启用 Step 3 `PRIMARY/AUXILIARY` 选择门，CLI 路径仍然保留。

详细使用说明请参阅 [`web/README.md`](web/README.md)。

## 包含内容

- `launch_agents.sh`：本地启动器，包含 `new`、`resume`、`pause`、`run`、`attach`、`trace` 和 `status` 等命令。
- `run_paper.sh`：兼容启动器；新项目和已迁移项目进入 `factory_core/` Python 引擎，未迁移建模项目进入冻结的 Legacy Adapter。
- `factory_core/`：版本化 SQLite 状态、追加式事件、恢复、重试、Step 注册和执行适配器。
- `STEPS.md`：标准的数学建模工作流契约。
- `modeling_guide.md`：项目结构、求解器、LaTeX、图表生成及可复现性规范。
- `prompts/step*.txt`：工作流每个步骤的智能体提示词模板。
- `method_library/`：已注册的建模方法和可运行的种子模板。
- `solver_submit.sh` 和 `solver_wrapper.sh`：异步本地求解器执行助手。
- `compile_paper.sh`：LaTeX 辅助脚本，选择 `xelatex` 编译中文/国赛风格论文。
- `scripts/`：辅助脚本，用于 Antigravity 路由、MinerU 解析、数字校验和清理工作。
- `evaluation/`：评分解析器以及针对外部大语言模型（LLM）裁判的基准校准脚本。
- `experiments/`：消融实验测试工具，用于测试不同流程机制对结果的影响。
- **`docs/guides/`**：优秀论文基准文档（可视化与写作规范）

诸如 `analysis_guide.md`、`stata_submit.sh` 和 `stata_wrapper.sh` 等旧文件仅为历史参考而保留，不再构成可执行社会科学工作流。新建模项目请遵循 `modeling_guide.md` 并使用 `solver_submit.sh`。

## 前置要求

- 安装并认证 `codex` CLI。
- 安装并认证 `claude` CLI（如果使用 Claude 备用路由）。
- Python 3 环境，使用本仓库目录下 `.venv` 虚拟环境中的依赖项。
- `uv` 用于按 `uv.lock` 创建 Python 环境；Node.js/npm 用于按 `web/frontend/package-lock.json` 创建 Web 前端环境。
- LaTeX 工具链：`xelatex`、`pdflatex` 和 `bibtex`。
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
chmod +x launch_agents.sh run_paper.sh compile_paper.sh solver_submit.sh solver_wrapper.sh
./launch_agents.sh status
```

启动器会根据需要创建运行时目录：

- `ongoing/`：进行中的项目。
- `complete/`：已完成的项目。
- `papers/`：最终生成的 PDF 论文及提交用的压缩包。
- `logs/` 和 `run_state/`：进程状态与日志。

这些运行输出会被 Git 自动忽略。

## 最新更新（2026-07-30）

当前未发布变更集中在核心编排、Web 控制面与仓库治理：

- 新项目使用 `factory_core.FactoryEngine`，状态与事件保存在项目内 `.factory/state.db`。
- `run_paper.sh` 已降级为兼容启动器；原生 Step 0-16 不再调用冻结 Bash，冻结实现只服务未迁移或显式回滚的项目。
- CLI、Web 和本地/Cloud Run 求解器通过同一 `FactoryService`、revision 与事件合同运行；云执行仍受全局 quarantine 限制。
- Python、Web backend、Cloud 镜像和前端构建均有锁文件，运行时启动脚本不再动态安装依赖。
- 重复运行按题目内容标识聚合成“题目归档”，历史完成运行保持只读。
- SQLite 用户库、bcrypt 密码、注册/审批、项目申请与项目 ACL 已成为现役权限模型。
- Secret Manager 是生产敏感值来源，弱默认管理员密码或缺失 JWT Secret 会阻止后端启动。
- 当前 Web 使用、部署和历史报告的所有权已重新收敛，避免旧文档继续充当现役 runbook。

详见 [CHANGELOG.md](CHANGELOG.md)。优秀论文写作与可视化基准仍位于 `docs/guides/`。

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

调试时可在前台运行：

```bash
./launch_agents.sh run test_cumcm2024b
```

若项目启用了 `selection/config.json`，Step 3 前会生成 `selection/step3_request.md` 并暂停。可在终端中查看候选后选择：

```bash
python3 scripts/selection_gate.py select-step3 ongoing/test_cumcm2024b \
  --primary m2 --aux m1 --reason "Prefer heuristic contrast"
```

该命令会写入 `selection/step3_decision.json` 和 `human_review.md`，并默认恢复项目运行；调试时可加 `--no-resume`。

检查状态：

```bash
./launch_agents.sh status
```

跟踪运行日志：

```bash
./launch_agents.sh attach test_cumcm2024b
```

## 在项目中使用求解器

在 `ongoing/<base>/` 或 `complete/<base>/` 目录内，智能体和人员都应该通过 `solver_submit.sh` 来运行复杂的求解任务：

```bash
../../solver_submit.sh --type python --max-time 600 models/m3_milp/03_solve.py
../../solver_submit.sh --status <jobid>
../../solver_submit.sh --status <jobid> --json
../../solver_submit.sh --wait <jobid>
```

`--status <jobid> --json` 返回字段白名单化的 `solver-job-evidence-v1`，可在不读取
SQLite 或旧状态文件的前提下核对任务、运行时、脚本、工作目录和状态。原生项目与
兼容项目使用同一公开格式。

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
- Step 3：方法选择，支持 `human_review.md` 手工介入修改；显式启用 `selection/config.json` 时会先暂停，让用户在 Step 2 验证过的候选流中选择 `PRIMARY/AUXILIARY`。
- Step 4：构建完整模型。
- Step 5：执行完整求解过程。
- Step 6：敏感性与鲁棒性分析。
- Step 7：模型评估。
- Step 8：数据可视化润色。
- Step 8.5：阅卷入口设计。为每个子问题定义评委入口三句式、主图/主表锚点和正文首段承接提纲。
- Step 9：撰写论文初稿。
- Step 10：门禁1 - 数值与代码一致性检查。
- Step 11：建设性审稿。
- Step 12：论文修订。
- Step 13：门禁2 - 数学有效性、执行证据和论文质量三角色隔离评审（此时为预提交结果）。
- Step 14：撰写摘要。
- Step 15：引用、图表及排版润色；任何修改都会使 Step 13 的预提交结果失效。
- Step 16：缓存未命中时先编译最终 PDF，再对 Step 15 后的三角色文本 packet 重新执行 Gate 2，并把通过结果绑定到该 PDF 的精确字节；自动评委不读取 PDF 画面，视觉质量另行检查。之后打包、清理并移至 `complete/`。

完整的详细步骤要求请参阅 `STEPS.md`。这些文件仍是产物与验证契约；项目的运行状态权威见下节。

## 注意事项

- 新项目和已迁移项目以 `.factory/state.db` 的版本化快照与追加式事件为工作流状态权威；产物文件是 Step 校验依据。`run_paper.sh --infer-step <project_dir>` 对这些项目读取 SQLite。
- 未迁移建模项目继续由冻结的 Legacy Adapter 根据产物文件推断。不要手工创建 `.factory/state.db`；使用下述显式迁移命令。
- 当 `modeling_guide.md` 和遗留的 `analysis_guide.md` 同时存在时，以 `modeling_guide.md` 为准。
- 已完成的项目将从 `ongoing/` 移至 `complete/`。
- Step 16 在缓存未命中时先成功编译新 PDF，再以当前数学 / 执行 / 论文三角色文本 packet 执行最终 Gate 2；`judge_outputs/final_submission.sha256` 同时绑定 packet、角色 prompts、聚合 / 调用实现、Step 13 模型路由与该 PDF 的精确字节，证明交付物和评审契约版本一致。它不表示自动评委观察过 PDF 渲染或图片像素。编译失败会立即停止，不会复制旧 PDF。之后才复制到 `papers/`、生成 `papers/<base>_submission.zip` 并按 `final_judge_v3` 契约写入 `delivery_manifest.json`。
- `complete/` 是历史交付目录，不等价于“符合当前最新契约”。使用 `python3 scripts/audit_complete_projects.py --write-manifests` 生成 `complete/_validation_index.json`，将项目分为 `CURRENT_PASS`、`LEGACY_DELIVERED` 和 `INVALID_OR_INCOMPLETE`。

## 迁移旧项目

`ongoing/` 迁移只允许已停止、无锁且 checkpoint 与 Legacy 推断一致的建模项目。历史 `complete/` 差异会作为 warning 保留，并以真实推断 Step 导入只读完成态，不会伪装成当前契约 Step 16。先检查并审阅报告，再用报告摘要导入：

```bash
python3 -m factory_core.cli migrate inspect ongoing/<base> \
  --report /tmp/<base>-migration.json
python3 -m factory_core.cli migrate apply ongoing/<base> \
  --report /tmp/<base>-migration.json --digest <report-digest>
```

迁移后 CLI、Web 和 `run_paper.sh --infer-step` 都读取 SQLite。需要回滚到冻结 Legacy Runner 时，项目必须已停止：

```bash
python3 -m factory_core.cli migrate rollback ongoing/<base>
```

迁移不会删除 checkpoint、marker、诊断、日志或论文产物。详细状态和恢复契约见 [`docs/architecture/ORCHESTRATION_ENGINE.md`](docs/architecture/ORCHESTRATION_ENGINE.md)。

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
