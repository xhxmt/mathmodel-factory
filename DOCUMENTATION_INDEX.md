# Paper Factory 文档索引

本索引只列出仓库内长期维护或具有明确历史价值的文档。运行日志、项目产物、外部论文和临时评测结果均由 `.gitignore` 管理，不属于文档目录。

## 快速入口

| 目标 | 文档 |
|---|---|
| 了解项目与快速开始 | [README.md](README.md) |
| 查看正常运行审计合同和支持边界 | [docs/operations/NORMAL_RUN_AUDIT_CONTRACT.md](docs/operations/NORMAL_RUN_AUDIT_CONTRACT.md) |
| 查看当前工作流契约 | [STEPS.md](STEPS.md) |
| 查看编排状态、迁移与恢复契约 | [docs/architecture/ORCHESTRATION_ENGINE.md](docs/architecture/ORCHESTRATION_ENGINE.md) |
| 查看当前 10-Stage 编排合同与实施状态 | [docs/architecture/STAGE_SIMPLIFICATION_PLAN.md](docs/architecture/STAGE_SIMPLIFICATION_PLAN.md) |
| 查看横向运行时基础设施收敛实现与验收计划 | [docs/architecture/RUNTIME_INFRASTRUCTURE_CONVERGENCE_PLAN.md](docs/architecture/RUNTIME_INFRASTRUCTURE_CONVERGENCE_PLAN.md) |
| 查看源码、运行数据和兼容边界 | [docs/architecture/repository-boundaries.md](docs/architecture/repository-boundaries.md) |
| 查看下一步优化执行方案（活性优先）与回放基线 | [docs/architecture/NEXT_OPTIMIZATION_EXECUTION_PLAN.md](docs/architecture/NEXT_OPTIMIZATION_EXECUTION_PLAN.md) |
| 编写模型、代码和论文 | [modeling_guide.md](modeling_guide.md) |
| 检查建模口径 | [docs/guides/MODELING_CHECKLIST.md](docs/guides/MODELING_CHECKLIST.md) |
| 使用 Web Dashboard | [web/README.md](web/README.md) |
| 5 分钟启动 Dashboard | [web/QUICKSTART.md](web/QUICKSTART.md) |
| 部署和回滚 Dashboard | [web/docs/deployment/DEPLOYMENT.md](web/docs/deployment/DEPLOYMENT.md) |
| 查看 Solver 作业证据面板 | [SOLVER_JOBS_FEATURE.md](SOLVER_JOBS_FEATURE.md) |
| 运行外部评估 | [evaluation/README.md](evaluation/README.md) |
| 运行消融实验 | [experiments/README.md](experiments/README.md) |
| 查看 Agent 入口规则 | [AGENTS.md](AGENTS.md) |

主流程入口：[Native 主流程与拆分清单](docs/architecture/NATIVE_MAINLINE.md)。
实验与历史实现的完整文档位于 `~/paper_new`，不作为本仓库当前运行合同。

## 核心契约

- [STEPS.md](STEPS.md)：当前 `contest_core_v1` 八阶段展示、10 Stage 调度和内部 Step 0–16 验证合同、全局时限及质量门禁。
- [modeling_guide.md](modeling_guide.md)：项目结构、求解器、结果复现、LaTeX 和图表规范。
- [AGENTS.md](AGENTS.md)：Codex 与通用 coding agent 的精简入口和安全边界。
- [CLAUDE.md](CLAUDE.md)：详细仓库架构、工作流和编辑约定。
- [CHANGELOG.md](CHANGELOG.md)：主要功能与工作流变更记录。
- [docs/architecture/ORCHESTRATION_ENGINE.md](docs/architecture/ORCHESTRATION_ENGINE.md)：Python 引擎、SQLite 状态、引擎原始设计（历史快照）。
- [docs/architecture/STAGE_SIMPLIFICATION_PLAN.md](docs/architecture/STAGE_SIMPLIFICATION_PLAN.md)：当前 10 Stage 映射、Step 0–16 验证/兼容边界、dirty flag、原始迁移/回滚设计与验收记录（以 Native 主流程边界为准）。
- [docs/architecture/RUNTIME_INFRASTRUCTURE_CONVERGENCE_PLAN.md](docs/architecture/RUNTIME_INFRASTRUCTURE_CONVERGENCE_PLAN.md)：已实现的类型化 WorkflowEvent、纯 Projector、Human Decision、StageExecutionPipeline 和 Job 幂等合同，以及尚未实现的 application-writer 唯一性与仍待完成的 clean-room 运营验收。
- [docs/architecture/repository-boundaries.md](docs/architecture/repository-boundaries.md)：核心、应用、部署、评测、历史资产和运行数据的所有权。
- [docs/architecture/compatibility-removal.md](docs/architecture/compatibility-removal.md)：兼容入口的可观察移除条件；本轮不删除这些入口。
- [docs/architecture/NEXT_OPTIMIZATION_EXECUTION_PLAN.md](docs/architecture/NEXT_OPTIMIZATION_EXECUTION_PLAN.md)：基于 16 次运行事件流的优化执行方案（阶段 0–6），以及阶段 0 的执行记录；回放基线见 `tests/fixtures/run_history/_baseline_8e8f872.json`。
- [docs/archive/WORKTREE_CONSOLIDATION_2026-07-30.md](docs/archive/WORKTREE_CONSOLIDATION_2026-07-30.md)：本轮旧 worktree 的恢复、取舍与合并依据（历史快照）。
- [docs/archive/FIRST_PRINCIPLES_AUDIT_2026-08-09.md](docs/archive/FIRST_PRINCIPLES_AUDIT_2026-08-09.md)：2026-08-09 第一性原理审计（历史快照；不得替代当前代码、工作流或部署文档）。

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
- [evaluation/SELECTOR_ROLLOUT_PLAN.md](evaluation/SELECTOR_ROLLOUT_PLAN.md)：Selector 可靠性、影子 portfolio 与人工放权的现役实施计划。
- [evaluation/SELECTOR_RELIABILITY.md](evaluation/SELECTOR_RELIABILITY.md)：R0b 同题 pairwise selector、TIE 带与 holdout 合同。
- [evaluation/SHADOW_PORTFOLIO.md](evaluation/SHADOW_PORTFOLIO.md)：R3 advisory-only portfolio 编排与报告合同。
- [evaluation/SELECTOR_AUTHORIZATION.md](evaluation/SELECTOR_AUTHORIZATION.md)：人工放权 receipt、scope、有效期和撤销合同。
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
- [SOLVER_JOBS_FEATURE.md](SOLVER_JOBS_FEATURE.md)：Solver Jobs API、前端面板、receipt 语义和维护边界。
- [docs/GCP_SERVICES_INTEGRATION.md](docs/GCP_SERVICES_INTEGRATION.md)：历史 GCP 服务集成设计快照；其中的 Cloud Solver 启用步骤已过时（相关代码已于 2026-11-10 移除），不得执行，求解只在本地运行。
- [docs/SECRET_MANAGER_GUIDE.md](docs/SECRET_MANAGER_GUIDE.md)：Secret Manager 配置。
- [docs/deployment/](docs/deployment/)：历史 Web 部署验证记录。

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

- [docs/archive/](docs/archive/)：仍用于解释合并或审计决定的历史记录；过期的整理、优化和完成报告通过 Git 历史查询。
- `docs/sessions/`：历史对话与计划原文的本地归档目录，默认被 Git 忽略，不同步到远端。

## 文档维护规则

- 根目录只保留项目入口、当前契约和兼容性文档。
- 当前可执行指南放入 `docs/guides/`，部署资料放入 `docs/deployment/`，验证记录放入 `docs/verification/`。
- 已被现役文档替代的一次性报告和过期说明从工作树移除，历史版本通过 Git 查询；`docs/archive/` 只保留仍有明确用途的决策与审计记录。
- 历史 Web 报告必须在开头标记“历史快照”并链接到当前使用或部署文档，不得保留可用凭据。
- 历史会话文本放入 `docs/sessions/`，不要继续堆放在仓库根目录。
- 不提交日志、密钥、本地环境、生成论文、构建产物或下载的外部资料；根目录 `work/` 为本地临时工作目录，`.pytest_cache/` 为可再生测试缓存，均由 `.gitignore` 管理。

**最后更新：2026-09-15**
