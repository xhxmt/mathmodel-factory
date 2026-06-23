# 项目文件整理报告

**整理日期：** 2026-06-23  
**整理范围：** 文档、测试脚本、部署脚本

---

## 整理原则

1. **核心文档保留在根目录** — README、配置指南、工作流文档
2. **历史文档按类型归档** — 部署、验证、变更日志、会话记录
3. **测试/临时脚本移至 archive/** — 保持 scripts/ 目录仅含活跃脚本
4. **Web 文档分层组织** — 主要文档在 web/，详细文档在 web/docs/

---

## 目录结构

### 📁 根目录 (~/paper_factory/)

```
paper_factory/
├── README.md              ← 项目介绍
├── CLAUDE.md              ← AI 助手指南
├── STEPS.md               ← 建模工作流（16 步）
├── modeling_guide.md      ← 建模规范
├── analysis_guide.md      ← 分析规范（遗留）
├── docs/                  ← 历史文档归档
│   ├── deployment/        (4) 云部署文档
│   ├── verification/      (8) P0/P1 验证报告
│   ├── changelogs/        (3) 变更日志
│   ├── guides/            (3) 修正指南、问题总结
│   └── sessions/          (1) 会话总结
├── archive/               ← 归档脚本
│   ├── test_scripts/      (6) 测试验证脚本
│   └── deploy_scripts/    (2) 实验性部署脚本
├── scripts/               ← 活跃工具脚本
├── web/                   ← Web 控制台
└── [运行脚本: *.sh]
```

**根目录保留文档 (9 个)：**
- `README.md` — 项目介绍
- `CLAUDE.md` — 编码助手指南
- `STEPS.md` — 16 步建模工作流
- `modeling_guide.md` — 建模规范
- `analysis_guide.md` — 分析规范（遗留）
- `STEPS_original.md` — 原始步骤定义
- `TASK_COMPLETION_SUMMARY.md` — 任务完成总结
- `VERIFICATION_PLAN.md` — 验证计划
- `VERIFICATION_STRATEGY.md` — 验证策略

---

### 📁 docs/ (文档归档)

#### docs/deployment/ (4 个文档)
- `CLOUD_DEPLOYMENT_GUIDE.md` — 云端部署指南
- `CLOUD_DEPLOYMENT_SUCCESS.md` — 云端部署成功报告
- `GCP_CLOUDRUN_SETUP_GUIDE.md` — GCP Cloud Run 设置
- `DEPLOYMENT_VERIFICATION.md` — 部署验证清单（最新）

#### docs/verification/ (8 个文档)
- `P0_P1_FIX_REPORT.md` — P0/P1 修复报告
- `P0_FIXES_COMPLETION.md` — P0 修复完成
- `P1_FIXES_COMPLETION.md` — P1 修复完成
- `P0_VERIFICATION_REPORT.md` — P0 验证报告
- `P1_VERIFICATION_COMPLETION.md` — P1 验证完成
- `P0_LIGHTWEIGHT_VERIFICATION_SUMMARY.md` — P0 轻量验证总结
- `P1_LIGHTWEIGHT_VERIFICATION_SUMMARY.md` — P1 轻量验证总结
- `ABLATION_COMPLETION_SUMMARY.md` — 消融实验完成总结

#### docs/changelogs/ (3 个文档)
- `CHANGELOG_method_intelligence.md` — 方法库智能化变更
- `CHANGELOG_optimization.md` — 优化变更日志
- `P0_P1_FIX_SUMMARY.md` — P0/P1 修复总结

#### docs/guides/ (3 个文档)
- `MODELING_CORRECTIONS_GUIDE.md` — 建模修正指南
- `CUMCM2025A_ISSUES_SUMMARY.md` — CUMCM 2025A 问题总结
- `STEP4_CONFIG_CHANGES.md` — 第 4 步配置变更

#### docs/sessions/ (1 个文档)
- `SESSION_SUMMARY_20260621.md` — 2026-06-21 会话总结

---

### 📁 archive/ (归档脚本)

#### archive/test_scripts/ (6 个脚本)
- `test_hang_check.sh` — 挂起检测测试
- `test_infer_step.sh` — 步骤推断测试
- `verify_batch1.sh` — 批量验证 1
- `verify_batch2.sh` — 批量验证 2
- `verify_batch3.sh` — 批量验证 3
- `test_cloud_solver.py` — 云端求解器测试

#### archive/deploy_scripts/ (2 个脚本)
- `deploy_cloud_solver.sh` — 云端求解器部署
- `deploy_direct.sh` — 直接部署脚本

---

### 📁 web/ 目录

```
web/
├── README.md              ← Web 控制台介绍
├── QUICKSTART.md          ← 快速开始
├── USAGE_GUIDE.md         ← 使用指南
├── INTERFACE_GUIDE.md     ← 界面指南
├── SCREENSHOTS.md         ← 截图说明
├── docs/                  ← Web 详细文档
│   ├── features/          (8) 功能文档
│   ├── deployment/        (5) 部署文档
│   ├── testing/           (2) 测试报告
│   └── archive/           (5) 历史总结
├── frontend/              ← Vue.js 前端
└── backend/               ← FastAPI 后端
```

#### web/docs/features/ (8 个文档)
- `FILE_UPLOAD_FEATURE.md` — 文件上传功能
- `FILE_UPLOAD_SUMMARY.md` — 文件上传总结
- `ARCHIVE_UPLOAD_FEATURE.md` — 归档上传功能
- `ARCHIVE_UPLOAD_SUMMARY.md` — 归档上传总结
- `ARCHIVE_UPLOAD_QUICKSTART.md` — 归档上传快速开始
- `CONSULTATION_EXAMPLE.md` — 咨询示例
- `WEB_CONSULTATION_ENHANCEMENT.md` — 咨询增强
- `ENHANCEMENT_SUMMARY.md` — 功能增强总结

#### web/docs/deployment/ (5 个文档)
- `DEPLOYMENT_COMPLETE.md` — 部署完成
- `DEPLOYMENT_CONFIRMED.md` — 部署确认
- `DEPLOYMENT_SUMMARY.md` — 部署总结
- `DEPLOYMENT_TO_TFISHER_DE.md` — tfisher.de 部署
- `PATH_FIX_REPORT.md` — 路径修复报告

#### web/docs/testing/ (2 个文档)
- `TEST_CONSULTATION_ENHANCEMENT.md` — 咨询增强测试
- `TEST_REPORT.md` — 测试报告

#### web/docs/archive/ (5 个文档)
- `FINAL_SUMMARY.md` — 最终总结
- `IMPLEMENTATION_COMPLETE.md` — 实现完成
- `IMPLEMENTATION_SUMMARY.md` — 实现总结
- `PROJECT_SUMMARY.md` — 项目总结
- `WEB_ISSUES_DISPLAY.md` — 问题显示

---

## .gitignore 更新

新增归档目录规则：

```gitignore
# Document archives (organized historical docs)
/docs/sessions/
/web/docs/archive/
/archive/
```

这些目录包含会话记录、历史总结和临时脚本，不纳入版本控制。

---

## 整理统计

| 类别 | 操作 | 数量 |
|------|------|------|
| 根目录文档 | 移动至 docs/ | 20 个 |
| Web 文档 | 移动至 web/docs/ | 20 个 |
| 测试脚本 | 归档至 archive/ | 6 个 |
| 部署脚本 | 归档至 archive/ | 2 个 |
| **总计** | **整理文件** | **48 个** |

---

## 核心文档快速索引

### 新用户起点
1. `README.md` — 项目介绍
2. `web/README.md` — Web 控制台
3. `web/QUICKSTART.md` — 快速开始

### 开发者文档
1. `CLAUDE.md` — AI 助手指南（编码规范）
2. `STEPS.md` — 16 步建模工作流
3. `modeling_guide.md` — 建模规范

### 运行脚本
- `launch_agents.sh` — 项目启动器
- `run_paper.sh` — 论文生成主流程
- `solver_submit.sh` — 求解器提交
- `compile_paper.sh` — LaTeX 编译

### 历史文档
- `docs/deployment/` — 云部署相关
- `docs/verification/` — P0/P1 验证报告
- `docs/guides/` — 修正指南和问题总结
- `web/docs/features/` — Web 功能文档

---

## 维护建议

### 文档归档规则

**应归档的文档类型：**
- 会话总结（`SESSION_SUMMARY_*.md`）→ `docs/sessions/`
- 验证报告（`*_VERIFICATION_*.md`）→ `docs/verification/`
- 部署记录（`DEPLOYMENT_*.md`）→ `docs/deployment/` 或 `web/docs/deployment/`
- 功能总结（`*_SUMMARY.md`）→ `web/docs/archive/`

**应保留在根目录的文档：**
- 核心指南（`README.md`, `CLAUDE.md`, `STEPS.md`, `modeling_guide.md`）
- 活跃的任务文档（`TASK_*.md`, `VERIFICATION_PLAN.md`）
- Web 主要文档（`web/README.md`, `web/QUICKSTART.md`）

### 定期清理

每月检查以下目录：
- `docs/sessions/` — 删除 3 个月前的会话记录
- `archive/test_scripts/` — 删除已失效的测试脚本
- `web/docs/archive/` — 压缩 6 个月前的总结文档

---

**整理完成时间：** 2026-06-23 09:15 UTC  
**执行者：** Claude Code  
**状态：** ✅ 完成
