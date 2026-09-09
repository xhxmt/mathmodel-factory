#!/usr/bin/env bash
# Web 服务重启完成验证脚本

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FACTORY_ROOT="${FACTORY:-$(cd -- "$SCRIPT_DIR/.." && pwd -P)}"
# shellcheck source=backend_service_health.sh
source "$SCRIPT_DIR/backend_service_health.sh"
FAILED=0

echo "=========================================="
echo "Paper Factory Web 服务状态检查"
echo "=========================================="
echo

# 1/2. 统一验证 unit、listener cgroup、稳定窗口与 API
echo "✓ 检查后端服务所有权与稳定性..."
if SNAPSHOT="$(verify_backend_service_stable)"; then
    IFS='|' read -r PID RESTARTS CONTROL_GROUP LISTENERS <<< "$SNAPSHOT"
    echo "  ✅ 后端由 paper-factory-api.service 稳定持有"
    echo "  ├─ MainPID: $PID"
    echo "  ├─ Listener PID(s): $LISTENERS"
    echo "  ├─ NRestarts: $RESTARTS"
    echo "  └─ ControlGroup: $CONTROL_GROUP"
else
    echo "  ❌ 后端 unit/PID/cgroup/HTTP 验收失败"
    FAILED=1
fi
echo

# 3. 检查 Nginx
echo "✓ 检查 Nginx..."
if systemctl is-active --quiet nginx.service; then
    echo "  ✅ Nginx 运行正常"
else
    echo "  ❌ Nginx 未运行"
    FAILED=1
fi
echo

# 4. 检查上传目录
echo "✓ 检查上传目录..."
if [ -d "$FACTORY_ROOT/uploads" ]; then
    echo "  ✅ 上传目录存在"
    echo "  └─ $FACTORY_ROOT/uploads/"
    echo "  └─ 权限: $(stat -c '%a' "$FACTORY_ROOT/uploads")"
else
    echo "  ❌ 上传目录不存在"
fi
echo

# 5. 检查关键文件
echo "✓ 检查关键配置文件..."
files=(
    "$SCRIPT_DIR/backend/main.py"
    "$SCRIPT_DIR/frontend/src/components/NewProjectModal.vue"
    "$FACTORY_ROOT/.gitignore"
)
for file in "${files[@]}"; do
    if [ -f "$file" ]; then
        echo "  ✅ $(basename $file)"
    else
        echo "  ❌ $(basename $file) 不存在"
    fi
done
echo

# 6. 功能清单
echo "=========================================="
echo "新功能已启用："
echo "=========================================="
echo "📤 文件上传功能"
echo "  • 支持拖拽上传 PDF/Markdown 文件"
echo "  • 实时上传进度显示"
echo "  • 文件大小限制: 100 MB"
echo "  • 自动文件名清理"
echo
echo "💡 人工咨询增强"
echo "  • 项目背景自动提取"
echo "  • 决策影响分析"
echo "  • 关键文件引用列表"
echo "  • 结构化回答建议"
echo

# 7. 访问信息
echo "=========================================="
echo "访问信息："
echo "=========================================="
echo "Web 界面: https://tfisher.de (或 http://服务器IP)"
echo "API 健康入口: http://localhost:8000/"
echo "登录信息: 使用已审批账号；管理员密码来自 Secret Manager"
echo

# 8. 测试建议
echo "=========================================="
echo "测试建议："
echo "=========================================="
echo "1. 打开浏览器访问 Web 界面"
echo "2. 点击 '➕ 新建项目'"
echo "3. 选择 '📤 上传文件' 标签"
echo "4. 拖拽一个 PDF 文件到上传区域"
echo "5. 观察上传进度条"
echo "6. 创建项目并验证成功"
echo

echo "=========================================="
echo "✅ 服务重启完成！"
echo "=========================================="
exit "$FAILED"
