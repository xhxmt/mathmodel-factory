# Paper Factory 文档索引

## 核心文档

### 用户指南
- **[README.md](README.md)** — 项目简介和快速开始
- **[STEPS.md](STEPS.md)** — 完整的 16 步建模工作流规范
- **[modeling_guide.md](modeling_guide.md)** — 项目结构、LaTeX、图表、求解器规范

### 开发指南
- **[CLAUDE.md](CLAUDE.md)** — AI 编码助手指南和仓库规范
- **[STEPS_original.md](STEPS_original.md)** — 原始社会科学工作流（参考）
- **[analysis_guide.md](analysis_guide.md)** — 遗留分析指南（已被 modeling_guide.md 取代）

---

## GCP 集成文档

### 配置指南
- **[CLOUD_SOLVER_ENABLED.md](CLOUD_SOLVER_ENABLED.md)** — Cloud Run Solver 完整指南
- **[docs/SECRET_MANAGER_GUIDE.md](docs/SECRET_MANAGER_GUIDE.md)** — Secret Manager 使用指南
- **[docs/GCP_SERVICES_INTEGRATION.md](docs/GCP_SERVICES_INTEGRATION.md)** — 完整 GCP 服务优化方案

### 当前状态
- ✅ Secret Manager 已启用（5 个密钥）
- ✅ Cloud Run Solver 已启用（自动路由）
- ⏳ Cloud Storage 自动备份（待配置）
- ⏳ Cloud Monitoring 告警（待配置）

---

## 专题文档

### 方法库
- **[docs/METHOD_LIBRARY_INTELLIGENCE_USAGE.md](docs/METHOD_LIBRARY_INTELLIGENCE_USAGE.md)** — 方法库智能检索使用指南
- **[docs/method_library_intelligence_summary.md](docs/method_library_intelligence_summary.md)** — 方法库优化总结

### Web Dashboard
- **[web/README.md](web/README.md)** — Web Dashboard 使用指南
- **[web/USAGE_GUIDE.md](web/USAGE_GUIDE.md)** — Dashboard 功能详解

### 部署和评估
- **[evaluation/README.md](evaluation/README.md)** — 外部评估系统
- **[experiments/README.md](experiments/README.md)** — 消融实验指南

---

## 变更日志和归档

### 部署相关
- **[docs/deployment/](docs/deployment/)** — 部署文档目录
- **[docs/changelogs/](docs/changelogs/)** — 历史变更记录

### 已归档文档
- **[docs/archive/](docs/archive/)** — 过期文档归档
  - 临时完成报告
  - 早期优化分析
  - 历史验证报告

---

## 文档维护原则

### 保留
- 核心工作流文档（STEPS.md, modeling_guide.md）
- 用户指南（README.md）
- 开发规范（CLAUDE.md）
- 当前生效的配置指南

### 归档
- 临时完成报告（*_COMPLETE.md, *_SUMMARY.md）
- 带日期的历史报告（*_2026-*.md）
- 已被新文档取代的旧版本

### 删除
- 空文件
- 自动生成的临时文件
- 损坏的文件

---

## 快速链接

| 我想... | 查看文档 |
|--------|---------|
| 了解整个系统 | [README.md](README.md) |
| 运行一个项目 | [STEPS.md](STEPS.md) |
| 编写模型代码 | [modeling_guide.md](modeling_guide.md) |
| 配置 GCP 服务 | [docs/GCP_SERVICES_INTEGRATION.md](docs/GCP_SERVICES_INTEGRATION.md) |
| 使用 Web 界面 | [web/README.md](web/README.md) |
| 运行消融实验 | [experiments/README.md](experiments/README.md) |
| 贡献代码 | [CLAUDE.md](CLAUDE.md) |

---

**最后更新:** 2026-06-23
