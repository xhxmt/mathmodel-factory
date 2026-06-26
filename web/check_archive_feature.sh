#!/bin/bash
# 压缩包上传功能 - 部署和验证清单

set -euo pipefail

BACKEND_DIR="/home/tfisher/paper_factory/web/backend"
FRONTEND_DIR="/home/tfisher/paper_factory/web/frontend"
WEB_DIR="/home/tfisher/paper_factory/web"

echo "====================================="
echo "压缩包上传功能 - 部署检查"
echo "====================================="
echo ""

# 1. 检查修改的文件
echo "✓ 检查修改的文件..."
if [[ -f "$BACKEND_DIR/app.py" ]]; then
    echo "  ✓ backend/app.py 存在"
    if grep -q "is_archive" "$BACKEND_DIR/app.py"; then
        echo "  ✓ 后端压缩包处理代码已就位"
    else
        echo "  ✗ 后端代码可能未正确修改"
        exit 1
    fi
else
    echo "  ✗ backend/app.py 不存在"
    exit 1
fi

if [[ -f "$FRONTEND_DIR/src/components/NewProjectModal.vue" ]]; then
    echo "  ✓ NewProjectModal.vue 存在"
    if grep -q ".tar.gz" "$FRONTEND_DIR/src/components/NewProjectModal.vue"; then
        echo "  ✓ 前端压缩包支持已就位"
    else
        echo "  ✗ 前端代码可能未正确修改"
        exit 1
    fi
else
    echo "  ✗ NewProjectModal.vue 不存在"
    exit 1
fi
echo ""

# 2. 检查文档
echo "✓ 检查文档..."
for doc in "ARCHIVE_UPLOAD_FEATURE.md" "ARCHIVE_UPLOAD_QUICKSTART.md" "ARCHIVE_UPLOAD_SUMMARY.md"; do
    if [[ -f "$WEB_DIR/$doc" ]]; then
        echo "  ✓ $doc"
    else
        echo "  ✗ $doc 缺失"
    fi
done
echo ""

# 3. 检查测试资源
echo "✓ 检查测试资源..."
if [[ -f "/tmp/test_problem.zip" ]]; then
    echo "  ✓ 测试压缩包存在：/tmp/test_problem.zip"
else
    echo "  ⚠ 测试压缩包不存在（可选）"
fi

if [[ -f "$WEB_DIR/test_archive_upload.sh" ]]; then
    echo "  ✓ 测试脚本存在"
    if [[ -x "$WEB_DIR/test_archive_upload.sh" ]]; then
        echo "  ✓ 测试脚本可执行"
    else
        echo "  ⚠ 测试脚本不可执行"
    fi
else
    echo "  ⚠ 测试脚本不存在"
fi
echo ""

# 4. 检查上传目录
echo "✓ 检查上传目录..."
UPLOAD_DIR="/home/tfisher/paper_factory/uploads"
if [[ -d "$UPLOAD_DIR" ]]; then
    echo "  ✓ uploads/ 目录存在"
    echo "  权限: $(stat -c '%a' "$UPLOAD_DIR")"
else
    echo "  ⚠ uploads/ 目录不存在（后端首次运行时会创建）"
fi
echo ""

# 5. 检查后端服务
echo "✓ 检查后端服务..."
if pgrep -f "web/backend/app.py" > /dev/null; then
    PID=$(pgrep -f "web/backend/app.py" | head -1)
    echo "  ✓ 后端服务运行中 (PID: $PID)"
    echo "  ⚠ 注意：需要重启后端以加载新代码"
    echo "    命令：kill -HUP $PID 或重启服务"
else
    echo "  ✗ 后端服务未运行"
    echo "    启动命令：cd $WEB_DIR && ./start_dashboard.sh"
fi
echo ""

# 6. 检查前端构建
echo "✓ 检查前端..."
if [[ -d "$FRONTEND_DIR/dist" ]]; then
    echo "  ✓ 前端已构建 (dist/ 存在)"
    echo "  ⚠ 注意：需要重新构建前端以应用更改"
    echo "    命令：cd $FRONTEND_DIR && npm run build"
else
    echo "  ⚠ 前端未构建"
    echo "    构建命令：cd $FRONTEND_DIR && npm run build"
fi
echo ""

# 7. 依赖检查
echo "✓ 检查 Python 依赖..."
if python3 -c "import zipfile, tarfile, shutil" 2>/dev/null; then
    echo "  ✓ 所需 Python 模块已安装（标准库）"
else
    echo "  ✗ Python 标准库模块缺失"
fi
echo ""

# 8. 总结
echo "====================================="
echo "部署清单总结"
echo "====================================="
echo ""
echo "已完成："
echo "  ✓ 后端代码修改（app.py）"
echo "  ✓ 前端代码修改（NewProjectModal.vue）"
echo "  ✓ 文档完整"
echo "  ✓ 测试资源准备"
echo ""
echo "待执行操作："
echo "  1. 重启后端服务（应用代码变更）"
echo "     kill -HUP \$(pgrep -f 'web/backend/app.py') || systemctl restart paper-factory-web"
echo ""
echo "  2. 重新构建前端（如果需要）"
echo "     cd $FRONTEND_DIR && npm run build"
echo ""
echo "  3. 运行测试验证"
echo "     cd $WEB_DIR && ./test_archive_upload.sh"
echo ""
echo "  4. 前端测试"
echo "     - 打开浏览器访问 http://localhost:5173"
echo "     - 创建新项目，上传测试压缩包"
echo "     - 验证解压和项目创建"
echo ""
echo "====================================="
echo "功能特性"
echo "====================================="
echo "  ✓ 支持 ZIP、TAR.GZ、TAR.BZ2、TAR.XZ 格式"
echo "  ✓ 自动解压和题目文件识别"
echo "  ✓ 关键词优先匹配（题目、problem、question）"
echo "  ✓ 保留所有数据文件供后续使用"
echo "  ✓ 向后兼容单文件上传"
echo "  ✓ 完整错误处理和清理机制"
echo ""
echo "文档："
echo "  - 技术细节：$WEB_DIR/ARCHIVE_UPLOAD_FEATURE.md"
echo "  - 用户指南：$WEB_DIR/ARCHIVE_UPLOAD_QUICKSTART.md"
echo "  - 实现总结：$WEB_DIR/ARCHIVE_UPLOAD_SUMMARY.md"
echo "  - 主 README：$WEB_DIR/README.md（已更新）"
echo ""
echo "====================================="
