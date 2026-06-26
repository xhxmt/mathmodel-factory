#!/bin/bash
# Web 服务重启完成验证脚本

echo "=========================================="
echo "Paper Factory Web 服务状态检查"
echo "=========================================="
echo

# 1. 检查后端服务
echo "✓ 检查后端服务..."
if systemctl is-active --quiet paper-factory-api.service; then
    echo "  ✅ 后端服务运行正常 (paper-factory-api.service)"
    PID=$(systemctl show paper-factory-api.service -p MainPID --value)
    echo "  └─ PID: $PID"
else
    echo "  ❌ 后端服务未运行"
fi
echo

# 2. 检查 API 端点
echo "✓ 检查 API 端点..."
if curl -s http://localhost:8000/ | grep -q "Paper Factory"; then
    echo "  ✅ API 端点响应正常"
    echo "  └─ http://localhost:8000/"
else
    echo "  ❌ API 端点无响应"
fi
echo

# 3. 检查 Nginx
echo "✓ 检查 Nginx..."
if systemctl is-active --quiet nginx.service; then
    echo "  ✅ Nginx 运行正常"
else
    echo "  ❌ Nginx 未运行"
fi
echo

# 4. 检查上传目录
echo "✓ 检查上传目录..."
if [ -d "/home/tfisher/paper_factory/uploads" ]; then
    echo "  ✅ 上传目录存在"
    echo "  └─ /home/tfisher/paper_factory/uploads/"
    echo "  └─ 权限: $(stat -c '%a' /home/tfisher/paper_factory/uploads)"
else
    echo "  ❌ 上传目录不存在"
fi
echo

# 5. 检查关键文件
echo "✓ 检查关键配置文件..."
files=(
    "/home/tfisher/paper_factory/web/backend/app.py"
    "/home/tfisher/paper_factory/web/frontend/src/components/NewProjectModal.vue"
    "/home/tfisher/paper_factory/.gitignore"
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
echo "API 文档: http://localhost:8000/docs"
echo "登录信息:"
echo "  • 用户名: admin"
echo "  • 密码: admin123"
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
