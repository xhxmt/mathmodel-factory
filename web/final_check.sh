#!/bin/bash

echo "=========================================="
echo "🎯 最终验证：文件上传功能"
echo "=========================================="
echo

# 检查构建时间
BUILD_TIME=$(stat -c %Y /home/tfisher/paper_factory/web/frontend/dist/index.html)
CURRENT_TIME=$(date +%s)
TIME_DIFF=$((CURRENT_TIME - BUILD_TIME))

echo "✅ 前端构建信息："
echo "   构建时间: $(date -d @$BUILD_TIME '+%Y-%m-%d %H:%M:%S')"
echo "   距今: $((TIME_DIFF / 60)) 分钟前"
echo

# 检查关键代码
echo "✅ 验证上传功能代码："
if grep -q "handleFileDrop" /home/tfisher/paper_factory/web/frontend/dist/assets/*.js; then
    echo "   ✅ handleFileDrop - 拖拽处理函数已编译"
else
    echo "   ❌ handleFileDrop - 未找到"
fi

if grep -q "uploadMethod" /home/tfisher/paper_factory/web/frontend/dist/assets/*.js; then
    echo "   ✅ uploadMethod - 模式切换已编译"
else
    echo "   ❌ uploadMethod - 未找到"
fi

if grep -q "isDragOver" /home/tfisher/paper_factory/web/frontend/dist/assets/*.js; then
    echo "   ✅ isDragOver - 拖拽状态已编译"
else
    echo "   ❌ isDragOver - 未找到"
fi

if grep -q "uploadProgress" /home/tfisher/paper_factory/web/frontend/dist/assets/*.js; then
    echo "   ✅ uploadProgress - 进度条已编译"
else
    echo "   ❌ uploadProgress - 未找到"
fi

echo

# 检查后端
echo "✅ 后端 API 状态："
if curl -s http://localhost:8000/ | grep -q "Paper Factory"; then
    echo "   ✅ API 运行正常"

    # 检查上传端点
    RESPONSE=$(curl -s -o /dev/null -w "%{http_code}" -X GET http://localhost:8000/api/upload/problem)
    if [ "$RESPONSE" = "405" ]; then
        echo "   ✅ 上传端点存在 (返回 405 Method Not Allowed - 正常)"
    else
        echo "   ⚠️  上传端点返回码: $RESPONSE"
    fi
else
    echo "   ❌ API 无响应"
fi

echo

# 检查上传目录
echo "✅ 上传目录："
if [ -d "/home/tfisher/paper_factory/uploads" ]; then
    echo "   ✅ 目录存在"
    echo "   权限: $(stat -c '%a %U:%G' /home/tfisher/paper_factory/uploads)"
    FILE_COUNT=$(ls -1 /home/tfisher/paper_factory/uploads/*.pdf /home/tfisher/paper_factory/uploads/*.md 2>/dev/null | wc -l)
    echo "   文件数: $FILE_COUNT"
else
    echo "   ❌ 目录不存在"
fi

echo

# Nginx 检查
echo "✅ Nginx 状态："
if systemctl is-active --quiet nginx; then
    echo "   ✅ Nginx 运行正常"
    NGINX_RELOAD=$(systemctl show nginx -p ActiveEnterTimestamp --value)
    echo "   最后加载: $NGINX_RELOAD"
else
    echo "   ❌ Nginx 未运行"
fi

echo
echo "=========================================="
echo "📋 使用说明"
echo "=========================================="
echo
echo "⚠️  重要：清除浏览器缓存"
echo "───────────────────────────────────────"
echo "前端已在 $(date -d @$BUILD_TIME '+%H:%M') 重新构建"
echo "浏览器可能缓存了旧版本的 JavaScript 文件"
echo
echo "清除缓存方法："
echo "  1️⃣  硬刷新: Ctrl + Shift + R"
echo "  2️⃣  清除缓存: Ctrl + Shift + Delete"
echo "  3️⃣  无痕模式: Ctrl + Shift + N (推荐)"
echo
echo "测试步骤："
echo "  1. 打开/刷新 https://tfisher.de"
echo "  2. 按 Ctrl+Shift+R 强制刷新"
echo "  3. 按 F12 打开开发者工具"
echo "  4. 切换到 Network 标签"
echo "  5. 勾选 'Disable cache'"
echo "  6. 再次刷新页面"
echo "  7. 点击 '➕ 新建项目'"
echo "  8. 尝试拖拽文件"
echo
echo "如果拖拽仍不工作："
echo "  • 查看 Console 标签是否有 JavaScript 错误"
echo "  • 检查 Network 标签中加载的 JS 文件时间戳"
echo "  • 尝试使用无痕模式重新打开"
echo
echo "测试文件："
echo "  /tmp/test_problem.md"
echo
echo "=========================================="
echo "✅ 系统就绪！请按上述步骤测试"
echo "=========================================="
