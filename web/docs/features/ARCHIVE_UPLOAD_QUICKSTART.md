# 压缩包上传功能 - 快速开始

## 功能概述

前端现在支持上传包含题目文件和数据的完整压缩包，系统会自动：
1. 解压压缩包
2. 智能识别题目文件（PDF 或 Markdown）
3. 保留所有数据文件供后续使用

## 支持的格式

### 压缩包
- ZIP (`.zip`)
- TAR.GZ (`.tar.gz`, `.tgz`)
- TAR.BZ2 (`.tar.bz2`)
- TAR.XZ (`.tar.xz`)

### 题目文件（必须在压缩包内）
- PDF (`.pdf`)
- Markdown (`.md`)

## 使用步骤

### 1. 准备压缩包

创建包含题目和数据的压缩包，推荐结构：

```
cumcm2024_a.zip
├── 题目.pdf              # 或 problem.pdf、A题.pdf 等
├── 附件1_检测数据.csv
├── 附件2_成本参数.xlsx
└── 说明.txt
```

**题目文件命名建议**（优先识别）：
- 包含"题目"、"problem"、"question"的文件
- 例如：`题目.pdf`, `problem.md`, `A题_问题描述.pdf`

### 2. 前端上传

打开 Web 控制台（http://localhost:8080），点击"新建项目"：

1. **输入项目名称**：如 `cumcm2024_a`
2. **选择"上传"方式**
3. **拖拽或选择压缩包**
4. 系统自动上传并解压
5. 点击"创建项目"

### 3. 后台处理

系统会：
- 解压到 `uploads/<timestamp>_<压缩包名>/`
- 查找题目文件（优先匹配关键词）
- 返回题目文件路径
- 启动 Step 0 解析

### 4. 验证

查看日志确认：
```bash
tail -f ongoing/<项目名>/logs/runner.log
```

Step 0 会处理题目文件，并可访问同目录的所有数据文件。

## 典型场景

### 场景1：CUMCM 竞赛题目包

```
cumcm2024_b.zip
├── B题.pdf                    # 自动识别
├── 附件1.csv
├── 附件2.xlsx
└── 附件3_图片/
    ├── 图1.png
    └── 图2.png
```

上传后：
- 题目文件：`uploads/20260621_123456_cumcm2024_b/B题.pdf`
- 所有附件保留在同目录

### 场景2：MCM/ICM 题目包

```
mcm2024_problem_c.tar.gz
├── problem.pdf                # 自动识别
├── data/
│   ├── dataset1.csv
│   └── dataset2.json
└── README.txt
```

### 场景3：自定义结构

```
my_problem.zip
├── docs/
│   └── 建模问题描述.md        # 深度嵌套也能识别
├── data/
│   ├── raw_data.csv
│   └── processed.json
└── figures/
    └── diagram.png
```

## 错误处理

### 常见错误

1. **"压缩包中未找到题目文件"**
   - 确保包含 `.pdf` 或 `.md` 文件
   - 检查文件扩展名是否正确

2. **"不支持的文件格式"**
   - 使用标准压缩格式（ZIP、TAR.GZ）
   - 避免使用 RAR、7Z 等（目前不支持）

3. **"文件过大"**
   - 压缩包限制 100MB
   - 分离大型数据集，上传后手动添加

### 调试

查看后端日志：
```bash
# 如果使用 systemd
journalctl -u paper-factory-web -f

# 或直接查看进程输出
ps aux | grep "app.py"
```

## 命令行测试

使用提供的测试脚本：
```bash
cd /home/tfisher/paper_factory/web
./test_archive_upload.sh
```

或手动测试：
```bash
# 1. 登录获取 token
TOKEN=$(curl -s -X POST http://localhost:8000/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"admin123"}' \
  | jq -r '.access_token')

# 2. 上传压缩包
curl -X POST http://localhost:8000/api/upload/problem \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@/tmp/test_problem.zip" \
  | jq '.'

# 3. 创建项目
curl -X POST http://localhost:8000/api/projects/new \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "base_name": "test_archive",
    "problem_path": "<返回的file_path>",
    "no_start": false,
    "consult": false
  }' | jq '.'
```

## 高级用法

### 预处理压缩包

如果题目文件名不标准，可以先解压修改：
```bash
unzip problem.zip -d /tmp/problem_temp
mv /tmp/problem_temp/main.pdf /tmp/problem_temp/题目.pdf
cd /tmp && zip -r problem_fixed.zip problem_temp/
```

### 包含预处理脚本

压缩包可包含数据预处理脚本：
```
problem.zip
├── 题目.pdf
├── data/
│   └── raw.csv
└── scripts/
    └── preprocess.py    # Step 4 可调用
```

### 多题目文件

如果压缩包包含多个 PDF/MD：
- 系统优先选择名称包含关键词的文件
- 如果都不包含，选择第一个找到的
- 建议：使用明确的文件名或事先整理

## 与单文件上传对比

| 特性 | 单文件上传 | 压缩包上传 |
|------|-----------|-----------|
| 题目文件 | ✓ | ✓ |
| 数据文件 | ✗ 需要后续手动添加 | ✓ 自动包含 |
| 图片资源 | ✗ | ✓ |
| 文档说明 | ✗ | ✓ |
| 上传次数 | 1次 | 1次 |
| 便捷性 | 简单场景 | 完整场景 |

**建议**：
- 简单题目（仅文本）→ 单文件上传
- 竞赛题目（含附件）→ 压缩包上传

## 后续步骤

上传后，系统自动进入标准流程：
1. **Step 0**：解析题目，生成 `problem/` 目录
2. **Step 1**：研究可行性
3. **Step 2-16**：完整建模流程

所有数据文件可在 Step 4（模型构建）和后续步骤中访问。

## 注意事项

1. **文件路径**：解压后的绝对路径会传给 Step 0，无需担心相对路径问题
2. **编码问题**：确保文件名使用 UTF-8 编码，避免中文乱码
3. **权限**：解压后的文件继承 `uploads/` 目录权限
4. **清理**：成功的项目可以手动清理 `uploads/` 以节省空间

## 故障排查

### 解压失败

```bash
# 测试压缩包完整性
unzip -t problem.zip   # ZIP
tar -tzf problem.tar.gz   # TAR.GZ
```

### 题目文件未识别

```bash
# 查看解压内容
ls -R uploads/<timestamp>_*/

# 手动查找 PDF/MD
find uploads/<timestamp>_*/ -name "*.pdf" -o -name "*.md"
```

### 权限问题

```bash
# 检查 uploads 目录权限
ls -ld /home/tfisher/paper_factory/uploads

# 修复（如需要）
chmod 755 /home/tfisher/paper_factory/uploads
```

## 技术细节

- **解压库**：Python `zipfile` + `tarfile`
- **搜索算法**：递归 `os.walk()`，关键词优先匹配
- **错误恢复**：解压失败自动清理临时文件
- **并发安全**：时间戳命名避免冲突

---

**版本**：1.0  
**更新时间**：2026-06-21  
**维护者**：Paper Factory Team
