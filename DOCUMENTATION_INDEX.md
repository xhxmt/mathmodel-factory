# Paper Factory 文档索引

本索引只列出仓库内长期维护或具有明确历史价值的文档。运行日志、项目产物、外部论文和临时评测结果均由 `.gitignore` 管理，不属于文档目录。

## 快速入口

| 目标 | 文档 |
|---|---|
| 了解项目与快速开始 | [README.md](README.md) |
| 查看当前工作流契约 | [STEPS.md](STEPS.md) |
| 查看编排状态、迁移与恢复契约 | [docs/architecture/ORCHESTRATION_ENGINE.md](docs/architecture/ORCHESTRATION_ENGINE.md) |
| 查看源码、运行数据和兼容边界 | [docs/architecture/repository-boundaries.md](docs/architecture/repository-boundaries.md) |
| 编写模型、代码和论文 | [modeling_guide.md](modeling_guide.md) |
| 检查建模口径 | [docs/guides/MODELING_CHECKLIST.md](docs/guides/MODELING_CHECKLIST.md) |
| 使用 Web Dashboard | [web/README.md](web/README.md) |
| 5 分钟启动 Dashboard | [web/QUICKSTART.md](web/QUICKSTART.md) |
| 部署和回滚 Dashboard | [web/docs/deployment/DEPLOYMENT.md](web/docs/deployment/DEPLOYMENT.md) |
| 运行外部评估 | [evaluation/README.md](evaluation/README.md) |
| 运行消融实验 | [experiments/README.md](experiments/README.md) |
| 查看 Agent 入口规则 | [AGENTS.md](AGENTS.md) |

## 核心契约

- [STEPS.md](STEPS.md)：当前 16 步数学建模工作流及质量门禁。
- [modeling_guide.md](modeling_guide.md)：项目结构、求解器、结果复现、LaTeX 和图表规范。
- [AGENTS.md](AGENTS.md)：Codex 与通用 coding agent 的精简入口和安全边界。
- [CLAUDE.md](CLAUDE.md)：详细仓库架构、工作流和编辑约定。
- [CHANGELOG.md](CHANGELOG.md)：主要功能与工作流变更记录。
- [docs/architecture/ORCHESTRATION_ENGINE.md](docs/architecture/ORCHESTRATION_ENGINE.md)：Python 引擎、SQLite 状态、Legacy 迁移和恢复契约。
- [docs/architecture/repository-boundaries.md](docs/architecture/repository-boundaries.md)：核心、应用、部署、评测、历史资产和运行数据的所有权。
- [docs/architecture/compatibility-removal.md](docs/architecture/compatibility-removal.md)：兼容入口的可观察移除条件；本轮不删除这些入口。
- [docs/archive/WORKTREE_CONSOLIDATION_2026-07-30.md](docs/archive/WORKTREE_CONSOLIDATION_2026-07-30.md)：本轮旧 worktree 的恢复、取舍与合并依据（历史快照）。
- [STEPS_original.md](STEPS_original.md)：原始社会科学工作流，仅供历史参考。
- [analysis_guide.md](analysis_guide.md)：兼容旧流程的分析指南；新项目以 `modeling_guide.md` 为准。

## 建模与写作指南

- [docs/guides/MODELING_CHECKLIST.md](docs/guides/MODELING_CHECKLIST.md)：建模口径纠错清单。
- [docs/guides/model_selection_guide.md](docs/guides/model_selection_guide.md)：模型选择与配置。
- [docs/guides/EXCELLENT_PAPER_WRITING_BENCHMARK.md](docs/guides/EXCELLENT_PAPER_WRITING_BENCHMARK.md)：优秀论文写作基准。
- [docs/guides/EXCELLENT_PAPER_VISUALIZATION_BENCHMARK.md](docs/guides/EXCELLENT_PAPER_VISUALIZATION_BENCHMARK.md)：优秀论文可视化基准。
- [docs/guides/NATIONAL1_CALIBRATION_ANCHOR.md](docs/guides/NATIONAL1_CALIBRATION_ANCHOR.md)：国一论文校准锚。
- [docs/reference/README.md](docs/reference/README.md)：优秀论文研究资料入口。
- [method_library/README.md](method_library/README.md)：可复用建模方法库。

## 质量门禁与评测

- [docs/complete_project_contract_audit.md](docs/complete_project_contract_audit.md)：历史完成项目与当前交付契约的审计说明。
- [evaluation/README.md](evaluation/README.md)：独立外部评估框架。
- [evaluation/human_rubric.md](evaluation/human_rubric.md)：人工评审量表。
- [evaluation/baseline_scores.md](evaluation/baseline_scores.md)：基准评分记录。
- [evaluation/calibration_report.md](evaluation/calibration_report.md)：评委校准报告。
- [evaluation/EXPERIMENTS_STATUS.md](evaluation/EXPERIMENTS_STATUS.md)：实验状态与后续任务。
- [experiments/README.md](experiments/README.md)：消融实验运行说明。
- [docs/verification/](docs/verification/)：历史验证报告。

## Web、云服务与部署

- [web/README.md](web/README.md)：Dashboard 安装与使用入口。
- [web/QUICKSTART.md](web/QUICKSTART.md)：最短启动路径。
- [web/USAGE_GUIDE.md](web/USAGE_GUIDE.md)：上传、题目归档与权限使用说明。
- [web/docs/deployment/DEPLOYMENT.md](web/docs/deployment/DEPLOYMENT.md)：唯一现役生产部署与回滚 runbook。
- [web/INTERFACE_GUIDE.md](web/INTERFACE_GUIDE.md)：历史界面快照；当前行为以 `web/README.md` 为准。
- [docs/GCP_SERVICES_INTEGRATION.md](docs/GCP_SERVICES_INTEGRATION.md)：历史 GCP 服务集成设计；当前状态以 Cloud Solver 隔离合同为准。
- [docs/SECRET_MANAGER_GUIDE.md](docs/SECRET_MANAGER_GUIDE.md)：Secret Manager 配置。
- [CLOUD_SOLVER_ENABLED.md](CLOUD_SOLVER_ENABLED.md)：Cloud Solver 当前 P0 代码合同、线上隔离状态、硬限制与解除隔离阻断项。
- [docs/deployment/](docs/deployment/)：历史 Cloud Solver 部署指南与验证记录；不得用于解除当前隔离。
- [web/docs/](web/docs/)：Web 专题记录；除 `deployment/DEPLOYMENT.md` 外，部署/测试完成报告默认按历史快照阅读。

## 专题报告与修复记录

- [docs/METHOD_LIBRARY_INTELLIGENCE_USAGE.md](docs/METHOD_LIBRARY_INTELLIGENCE_USAGE.md)：方法库智能检索使用说明。
- [docs/method_library_intelligence_summary.md](docs/method_library_intelligence_summary.md)：方法库优化总结。
- [docs/OPTIMIZATION_REPORT_2026-06-23.md](docs/OPTIMIZATION_REPORT_2026-06-23.md)：2026-06-23 系统优化报告。
- [docs/BLIND_2025A_FIX_PLAN.md](docs/BLIND_2025A_FIX_PLAN.md)：2025A blind 项目修复计划。
- [docs/RERUN0706_REPAIR_PLAN.md](docs/RERUN0706_REPAIR_PLAN.md)：2025A rerun 事故链修复计划。
- [docs/analysis_requests/](docs/analysis_requests/)：专题分析请求与结果。
- [docs/changelogs/](docs/changelogs/)：专项变更记录。
- [docs/superpowers/](docs/superpowers/)：历史设计与实施计划；仅作为项目记录保留。

## 历史归档

- [docs/archive/](docs/archive/)：已过期或已完成的组织、优化和清理报告。
- `docs/sessions/`：历史对话与计划原文的本地归档目录，默认被 Git 忽略，不同步到远端。

## 文档维护规则

- 根目录只保留项目入口、当前契约和兼容性文档。
- 当前可执行指南放入 `docs/guides/`，部署资料放入 `docs/deployment/`，验证记录放入 `docs/verification/`。
- 一次性完成报告、已被替代的方案和过期说明移入 `docs/archive/`。
- 历史 Web 报告必须在开头标记“历史快照”并链接到当前使用或部署文档，不得保留可用凭据。
- 历史会话文本放入 `docs/sessions/`，不要继续堆放在仓库根目录。
- 不提交日志、密钥、本地环境、生成论文、构建产物或下载的外部资料。

**最后更新：2026-07-27**
