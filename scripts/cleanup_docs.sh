#!/bin/bash
# 清理 Paper Factory 项目中的临时文档和过期报告

set -e

FACTORY_ROOT="/home/tfisher/paper_factory"
ARCHIVE_DIR="$FACTORY_ROOT/docs/archive"

echo "=========================================="
echo "  Paper Factory 文档清理脚本"
echo "=========================================="
echo ""

# 创建归档目录
mkdir -p "$ARCHIVE_DIR"

echo "清理策略:"
echo "  1. 临时完成报告 → 归档到 docs/archive/"
echo "  2. 重复的 GCP 文档 → 合并到主文档"
echo "  3. 过期的验证报告 → 归档"
echo "  4. 核心文档保留 → README.md, CLAUDE.md, STEPS.md, modeling_guide.md"
echo ""

# ============================================
# 1. 临时完成报告（归档）
# ============================================
echo "1. 归档临时完成报告..."

TEMP_REPORTS=(
    "ENV_CLEANUP_COMPLETE.md"
    "SECRET_MANAGER_INTEGRATION_COMPLETE.md"
    "ORGANIZATION_COMPLETE.md"
    "TASK_COMPLETION_SUMMARY.md"
)

for file in "${TEMP_REPORTS[@]}"; do
    if [[ -f "$FACTORY_ROOT/$file" ]]; then
        mv "$FACTORY_ROOT/$file" "$ARCHIVE_DIR/"
        echo "  ✓ 已归档: $file"
    fi
done
echo ""

# ============================================
# 2. 重复的 GCP 文档（保留最新）
# ============================================
echo "2. 处理 GCP 文档..."

# 保留: docs/GCP_SERVICES_INTEGRATION.md (最完整)
# 归档: docs/GCP_OPTIMIZATION_ANALYSIS.md (早期分析)
if [[ -f "$FACTORY_ROOT/docs/GCP_OPTIMIZATION_ANALYSIS.md" ]]; then
    mv "$FACTORY_ROOT/docs/GCP_OPTIMIZATION_ANALYSIS.md" "$ARCHIVE_DIR/"
    echo "  ✓ 已归档: docs/GCP_OPTIMIZATION_ANALYSIS.md (被 GCP_SERVICES_INTEGRATION.md 取代)"
fi

# 归档重复的 Secret Manager 文档
if [[ -f "$FACTORY_ROOT/docs/SECRET_MANAGER_INTEGRATION_SUMMARY.md" ]]; then
    mv "$FACTORY_ROOT/docs/SECRET_MANAGER_INTEGRATION_SUMMARY.md" "$ARCHIVE_DIR/"
    echo "  ✓ 已归档: docs/SECRET_MANAGER_INTEGRATION_SUMMARY.md (被 SECRET_MANAGER_GUIDE.md 取代)"
fi
echo ""

# ============================================
# 3. 过期的优化和验证报告（归档）
# ============================================
echo "3. 归档过期的优化和验证报告..."

OLD_REPORTS=(
    "docs/optimization_summary_2026-06-13.md"
    "docs/OPTIMIZATION_REPORT_2026-06-13.md"
    "docs/OPTIMIZATION_USAGE.md"
    "docs/README_OPTIMIZATION.md"
    "VERIFICATION_PLAN.md"
    "VERIFICATION_STRATEGY.md"
    "PROJECT_ORGANIZATION.md"
)

for file in "${OLD_REPORTS[@]}"; do
    if [[ -f "$FACTORY_ROOT/$file" ]]; then
        mv "$FACTORY_ROOT/$file" "$ARCHIVE_DIR/"
        echo "  ✓ 已归档: $file"
    fi
done
echo ""

# ============================================
# 4. 旧的开发文档（归档）
# ============================================
echo "4. 归档旧的开发文档..."

OLD_DEV_DOCS=(
    "docs/refactor_plan.md"
    "docs/my_understanding.md"
)

for file in "${OLD_DEV_DOCS[@]}"; do
    if [[ -f "$FACTORY_ROOT/$file" ]]; then
        mv "$FACTORY_ROOT/$file" "$ARCHIVE_DIR/"
        echo "  ✓ 已归档: $file"
    fi
done
echo ""

# ============================================
# 5. 创建文档索引
# ============================================
echo "5. 创建文档索引..."

cat > "$FACTORY_ROOT/DOCUMENTATION_INDEX.md" <<'EOF'
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
EOF

echo "  ✓ 已创建: DOCUMENTATION_INDEX.md"
echo ""

# ============================================
# 汇总报告
# ============================================
echo "=========================================="
echo "  清理完成！"
echo "=========================================="
echo ""

echo "归档文件数: $(ls -1 "$ARCHIVE_DIR" 2>/dev/null | wc -l)"
echo "归档位置: docs/archive/"
echo ""

echo "保留的核心文档:"
for doc in README.md CLAUDE.md STEPS.md modeling_guide.md CLOUD_SOLVER_ENABLED.md DOCUMENTATION_INDEX.md; do
    if [[ -f "$FACTORY_ROOT/$doc" ]]; then
        echo "  ✓ $doc"
    fi
done
echo ""

echo "保留的 docs/ 文档:"
ls -1 "$FACTORY_ROOT/docs/" | grep -E "\.md$" | grep -v "archive" | while read doc; do
    echo "  ✓ docs/$doc"
done
echo ""

echo "后续建议:"
echo "  1. 查看归档: ls -lh docs/archive/"
echo "  2. 如需恢复: cp docs/archive/<file> ./"
echo "  3. 文档索引: cat DOCUMENTATION_INDEX.md"
echo ""
