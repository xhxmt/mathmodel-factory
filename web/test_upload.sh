#!/bin/bash
# 验证文件上传功能的测试指南

echo "=========================================="
echo "📤 文件上传功能测试指南"
echo "=========================================="
echo
echo "✅ 前端已重新构建 (15:21)"
echo "✅ Nginx 已重新加载"
echo "✅ 后端 API 运行正常"
echo
echo "=========================================="
echo "🧪 测试步骤"
echo "=========================================="
echo
echo "1️⃣ 清除浏览器缓存"
echo "   Chrome/Edge: Ctrl+Shift+Delete → 清除缓存"
echo "   或使用无痕模式: Ctrl+Shift+N"
echo
echo "2️⃣ 访问 Web 界面"
echo "   URL: https://tfisher.de"
echo "   登录: admin / admin123"
echo
echo "3️⃣ 测试拖拽上传"
echo "   a. 点击右上角 '➕ 新建项目'"
echo "   b. 输入项目名称: test_drag_upload"
echo "   c. 确保选择 '📤 上传文件' 标签"
echo "   d. 从桌面拖拽 PDF/MD 文件到上传区域"
echo "   e. 观察："
echo "      • 拖拽时上传区域应该变蓝色高亮"
echo "      • 释放后显示文件卡片（文件名、大小、图标）"
echo "      • 如果有进度条显示 0% → 100%"
echo
echo "4️⃣ 测试点击上传"
echo "   a. 点击 '➕ 新建项目'"
echo "   b. 输入项目名称: test_click_upload"
echo "   c. 点击上传区域"
echo "   d. 选择文件"
echo "   e. 验证文件信息显示正确"
echo
echo "5️⃣ 创建项目"
echo "   a. 勾选 '启用人工咨询模式'（可选）"
echo "   b. 点击 '创建项目'"
echo "   c. 等待项目创建成功"
echo
echo "=========================================="
echo "🔍 故障排查"
echo "=========================================="
echo
echo "❌ 如果上传区域没有反应："
echo "   1. 按 F12 打开浏览器开发者工具"
echo "   2. 切换到 Console 标签"
echo "   3. 查看是否有 JavaScript 错误"
echo "   4. 尝试硬刷新: Ctrl+Shift+R"
echo
echo "❌ 如果显示 'Method Not Allowed'："
echo "   • 检查后端 API 是否运行:"
curl -s http://localhost:8000/ | grep -q "Paper Factory" && echo "     ✅ 后端 API 正常" || echo "     ❌ 后端 API 异常"
echo
echo "❌ 如果拖拽后没有高亮："
echo "   • 可能是浏览器缓存问题"
echo "   • 强制刷新: Ctrl+Shift+R"
echo "   • 或使用无痕模式重新打开"
echo
echo "=========================================="
echo "📁 测试文件"
echo "=========================================="
echo
echo "已创建测试文件:"
echo "  /tmp/test_problem.md"
echo
ls -lh /tmp/test_problem.md
echo
echo "你可以用这个文件测试上传功能"
echo
echo "=========================================="
echo "✅ 系统已就绪！"
echo "=========================================="
echo
echo "现在请："
echo "1. 打开浏览器访问 https://tfisher.de"
echo "2. 清除缓存或使用无痕模式"
echo "3. 按照上述步骤测试上传功能"
echo
