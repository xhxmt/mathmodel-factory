# Web 文件上传功能使用指南

## 快速开始

### 1. 启动服务

```bash
# 在 web 目录下

# 启动后端 (端口 8000)
cd /home/tfisher/paper_factory/web
source venv/bin/activate
cd backend
python app.py &

# 启动前端 (端口 5173)
cd /home/tfisher/paper_factory/web/frontend
npm run dev &
```

### 2. 访问界面

打开浏览器访问: **http://localhost:5173**

### 3. 登录

- **用户名**: `admin`
- **密码**: `T-fisher2005` (在 `.env` 文件中配置)

## 使用流程

### 方式一：上传文件（推荐）

1. 点击右上角 **"➕ 新建项目"** 按钮
2. 输入项目名称（例如：`cumcm2024_a`）
3. 确保选择 **"📤 上传文件"** 标签
4. 上传文件（两种方式任选其一）：
   - **点击上传**: 点击上传区域，选择文件
   - **拖拽上传**: 将文件拖拽到上传区域
5. 等待上传完成（显示进度条）
6. 可选：勾选选项
   - ☐ 仅创建项目，不自动开始执行
   - ☐ 启用人工咨询模式
7. 点击 **"创建项目"** 按钮

### 方式二：指定路径

1. 点击 **"➕ 新建项目"**
2. 输入项目名称
3. 选择 **"📁 指定路径"** 标签
4. 输入服务器文件路径（例如：`/home/user/problems/2024_A.pdf`）
5. 点击 **"创建项目"**

## 支持的文件格式

- ✅ **PDF**: `.pdf`, `.PDF`
- ✅ **Markdown**: `.md`, `.MD`
- ❌ 其他格式会被拒绝

## 文件大小限制

- 最大支持 **100 MB**

## 上传的文件存储位置

文件保存在: `/home/tfisher/paper_factory/uploads/`

文件命名格式: `YYYYMMDD_HHMMSS_原始文件名`

示例:
```
20260616_162144_cumcm_2024_A.pdf
20260616_162145_problem_description.md
```

## 创建的项目位置

项目目录: `/home/tfisher/paper_factory/ongoing/<project_name>/`

项目结构:
```
ongoing/cumcm2024_a/
├── checkpoint.md          # 进度检查点
├── problem/              # 题目解析
├── data/                 # 数据文件
├── models/               # 模型代码
├── figures/              # 图表
├── tables/               # 表格
└── logs/                 # 日志
```

## 常见问题

### Q1: 上传后找不到文件？

**A**: 文件保存在 `uploads/` 目录，文件名带有时间戳前缀。检查:
```bash
ls -lh /home/tfisher/paper_factory/uploads/
```

### Q2: 项目创建失败？

**A**: 检查:
1. 文件路径是否正确
2. 文件是否存在
3. 是否有权限访问

查看后端日志:
```bash
tail -f /home/tfisher/paper_factory/web/backend/backend.log
```

### Q3: 登录失败？

**A**: 确认密码正确。密码在 `.env` 文件中配置:
```bash
cat /home/tfisher/paper_factory/web/.env | grep ADMIN_PASSWORD
```

### Q4: 前端无法连接后端？

**A**: 确认后端正在运行:
```bash
curl http://localhost:8000/
```

应该返回:
```json
{"status":"Paper Factory Dashboard API","version":"1.0.0"}
```

### Q5: 如何修改密码？

**A**: 编辑 `.env` 文件:
```bash
nano /home/tfisher/paper_factory/web/.env
```

修改 `ADMIN_PASSWORD=your-new-password`，然后重启后端。

## API 测试

### 测试脚本

完整的 API 测试脚本位于: `/tmp/final_complete_test.sh`

运行测试:
```bash
bash /tmp/final_complete_test.sh
```

### 手动测试

```bash
# 1. 登录
TOKEN=$(curl -s -X POST "http://localhost:8000/api/auth/login" \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"T-fisher2005"}' | \
  python3 -c "import sys, json; print(json.load(sys.stdin)['access_token'])")

# 2. 上传文件
curl -X POST "http://localhost:8000/api/upload/problem" \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@/path/to/your/file.pdf"

# 3. 创建项目
curl -X POST "http://localhost:8000/api/projects/new" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "base_name": "test_project",
    "problem_path": "/home/tfisher/paper_factory/uploads/xxx.pdf",
    "no_start": false,
    "consult": false
  }'

# 4. 查询项目
curl -X GET "http://localhost:8000/api/projects" \
  -H "Authorization: Bearer $TOKEN"
```

## 安全注意事项

1. **修改默认密码**: 生产环境请修改 `.env` 中的密码
2. **HTTPS**: 生产环境建议使用 HTTPS
3. **防火墙**: 限制 8000 和 5173 端口的访问
4. **文件清理**: 定期清理 `uploads/` 目录中的旧文件

## 技术栈

- **后端**: FastAPI + Uvicorn + Python 3.13
- **前端**: Vue 3 + Vite + Axios
- **认证**: JWT Bearer Token
- **文件处理**: Python-Multipart
- **环境变量**: Python-Dotenv

## 开发

### 后端开发

```bash
cd /home/tfisher/paper_factory/web/backend
source ../venv/bin/activate
python app.py  # 启动开发服务器
```

### 前端开发

```bash
cd /home/tfisher/paper_factory/web/frontend
npm run dev    # 启动开发服务器
```

## 生产部署

参见: `DEPLOYMENT.md`

---

**最后更新**: 2026-06-16
**版本**: 1.0.0
**状态**: ✅ 功能完整并通过测试
