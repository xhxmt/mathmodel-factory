# 建模工厂 (Modeling Factory)

本项目是一个用于数学建模竞赛（如高教社杯 CUMCM、美赛 MCM/ICM 及类似应用建模竞赛）的本地多智能体工作流。它改编自原始的本地 Paper Factory，但当前活跃的工作流专注于：竞赛赛题解析、方法选择、数学建模、求解器执行、鲁棒性检验、论文草拟、模拟评委打分以及最终的打包提交。

原始的社会科学资产保留以供参考和兼容，但标准建模流程遵循 `STEPS.md` 和 `modeling_guide.md`。

## 交互式演示界面

请查看位于 [**`xhxmt.github.io/`**](https://xhxmt.github.io/) 的交互式演示项目。它现已升级为一个动态的前端交互页面，采用现代化的 UI 设计风格构建。该演示站点不仅模拟了多智能体数学建模工作流的终端执行过程，还结合真实的数据产出，通过直观的交互式卡片展示了系统的工作流架构、质量检查门禁和评测指标。只需在浏览器中打开 [xhxmt.github.io](https://xhxmt.github.io/) 即可体验全貌。

## 包含内容

- `launch_agents.sh`：本地启动器，包含 `new`、`resume`、`pause`、`run`、`attach`、`trace` 和 `status` 等命令。
- `run_paper.sh`：16步建模工厂运行程序，支持基于文件状态的断点续传（以及消融实验开关）。
- `STEPS.md`：标准的数学建模工作流契约。
- `modeling_guide.md`：项目结构、求解器、LaTeX、图表生成及可复现性规范。
- `prompts/step*.txt`：工作流每个步骤的智能体提示词模板。
- `method_library/`：已注册的建模方法和可运行的种子模板。
- `solver_submit.sh` 和 `solver_wrapper.sh`：异步本地求解器执行助手。
- `compile_paper.sh`：LaTeX 辅助脚本，选择 `xelatex` 编译中文/国赛风格论文。
- `scripts/`：辅助脚本，用于 Antigravity 路由、MinerU 解析、数字校验和清理工作。
- `evaluation/`：评分解析器以及针对外部大语言模型（LLM）裁判的基准校准脚本。
- `experiments/`：消融实验测试工具，用于测试不同流程机制对结果的影响。
- `xhxmt.github.io/`：交互式的项目演示前端界面，用于展示多智能体工作流的运行过程与结果。

诸如 `analysis_guide.md`、`stata_submit.sh` 和 `stata_wrapper.sh` 等旧文件仅为保持跨模式兼容性而保留。新建模项目请遵循 `modeling_guide.md` 规范并使用 `solver_submit.sh`。

## 前置要求

- 安装并认证 `codex` CLI。
- 安装并认证 `claude` CLI（如果使用 Claude 备用路由）。
- Python 3 环境，使用本仓库目录下 `.venv` 虚拟环境中的依赖项。
- LaTeX 工具链：`xelatex`、`pdflatex` 和 `bibtex`。
- 至少一套用于项目代码的实用求解器技术栈，通常为带有 `numpy`、`scipy`、`pandas` 和 `matplotlib` 的 Python 环境。
- 可选：通过 MinerU 解析 PDF 赛题，需在 `.env` 中配置 `MINERU_TOKEN`。
- 可选：如果项目需要，可安装 Julia、MATLAB/Octave、R、Gurobi 或其他求解器。

敏感信息和本地配置请存放在 `.env` 文件中，该文件已被 git 忽略。

## 快速开始

克隆仓库，进入目录并检查启动器：

```bash
git clone <repo-url> mathmodel-factory
cd mathmodel-factory
chmod +x launch_agents.sh run_paper.sh compile_paper.sh solver_submit.sh solver_wrapper.sh
./launch_agents.sh status
```

启动器会根据需要创建运行时目录：

- `ongoing/`：进行中的项目。
- `complete/`：已完成的项目。
- `papers/`：最终生成的 PDF 论文及提交用的压缩包。
- `logs/` 和 `run_state/`：进程状态与日志。

这些运行输出会被 Git 自动忽略。

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
../../solver_submit.sh --wait <jobid>
```

支持的类型包括 `python`、`julia`、`matlab`、`R` 和 `gurobi`，前提是本地已安装相应的环境。

必要时可手动编译论文：

```bash
../../compile_paper.sh "$(pwd)" <base_name>
```

## 工作流概览

活跃的建模工作流包含设置步骤以及后续的1-16个步骤：

- 设置 / Step 0：将赛题解析至 `problem/` 目录。
- Step 1：背景调研及方法预选。
- Step 2：并行生成建模方案及示例求解。
- Step 3：方法选择，支持 `human_review.md` 手工介入修改。
- Step 4：构建完整模型。
- Step 5：执行完整求解过程。
- Step 6：敏感性与鲁棒性分析。
- Step 7：模型评估。
- Step 8：数据可视化润色。
- Step 9：撰写论文初稿。
- Step 10：门禁1 - 数值与代码一致性检查。
- Step 11：建设性审稿。
- Step 12：论文修订。
- Step 13：门禁2 - 模拟评委打分。
- Step 14：撰写摘要。
- Step 15：引用、图表及排版润色。
- Step 16：编译、打包、清理目录，并将项目移至 `complete/`。

完整的详细步骤要求，请参阅 `STEPS.md`，这也是 `run_paper.sh --infer-step` 所遵循的文件状态契约。

## 注意事项

- 文件状态具有最高权威。`run_paper.sh --infer-step <project_dir>` 会检查实际产出的文件，并且修复并对齐检查点文本。
- 当 `modeling_guide.md` 和遗留的 `analysis_guide.md` 同时存在时，以 `modeling_guide.md` 为准。
- 已完成的项目将从 `ongoing/` 移至 `complete/`。
- Step 16 会将最终版本的 PDF 复制到 `papers/` 并生成 `papers/<base>_submission.zip` 文件。

## 评测与消融实验

代码库包含完整的测试工具集：
- **`evaluation/`**：包含校准的外部 LLM 评委（DeepSeek/Claude）解析器。这些脚本能评估最终编译的 PDF，并在 6 个关键维度上模拟竞赛评分。
- **`experiments/`**：消融实验测试工具。你可以设置环境变量（例如 `ABLATE_NO_CONSULTATION=1`、`ABLATE_NO_METHOD_LIB=1`、`ABLATE_NO_JUDGE=1`、`ABLATE_NO_INNOVATION_PROTECT=1`）来选择性地关闭管道特征，并通过外部评委衡量其对成绩的影响。
