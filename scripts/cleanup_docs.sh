#!/bin/bash
# 清理 Paper Factory 项目中的临时文档和过期报告

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FACTORY_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
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
# 5. 检查文档索引
# ============================================
echo "5. 检查文档索引..."

if [[ ! -f "$FACTORY_ROOT/DOCUMENTATION_INDEX.md" ]]; then
    echo "  ✗ 缺少 DOCUMENTATION_INDEX.md，请先恢复或重建文档索引" >&2
    exit 1
fi

echo "  ✓ DOCUMENTATION_INDEX.md 已存在；索引由维护者更新，清理脚本不会覆盖"
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
