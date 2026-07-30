# Web Dashboard 使用指南

本文描述当前用户流程。安装与架构见 [`README.md`](README.md)，最短启动路径见 [`QUICKSTART.md`](QUICKSTART.md)，生产操作见 [`docs/deployment/DEPLOYMENT.md`](docs/deployment/DEPLOYMENT.md)。

## 访问角色

### 未登录访客

访客进入只读论文展厅，只能查看 `SHOWCASE_PROJECTS` 白名单中的完成论文。访客不能访问内部项目状态、日志、文件、用户管理或控制动作。

### 普通用户

1. 在登录页注册用户名、密码和可选显示名。
2. 等待管理员审批；只有 `active` 用户可以登录。
3. 登录后上传题目并提交项目申请。
4. 管理员审批申请后，系统创建项目并把申请人写入项目 ACL。
5. 用户只看到获授权项目，可查看状态和执行允许的项目操作。

### 管理员

管理员可查看全部项目，直接创建项目，审批/拒绝用户和项目申请，查看审计日志与 Secret Manager 元数据健康状态。

系统没有默认管理员密码。不要从历史报告、测试脚本或命令历史获取登录值。

## 上传题目

“新建项目”支持上传或服务器路径两种方式。

### 支持格式

- 单文件：PDF、Markdown
- 压缩包：ZIP、TAR、TGZ、TAR.GZ、TAR.BZ2、TAR.XZ
- 默认大小上限：100 MB

压缩包会解压到 `uploads/` 下的独立目录，并自动寻找题目 PDF/Markdown。归档中的其他数据文件会保留给后续建模步骤使用；目录穿越归档会被拒绝。

### 项目名

项目名只能使用字母、数字、下划线和连字符。名称不能与 `ongoing/`、`complete/` 或待处理/已批准申请中的项目冲突。

### 创建选项

- “仅创建，不自动开始”对应 `--no-start`。
- “启用人工咨询”对应 `--consult`。

管理员提交后直接调用 `FactoryService`。普通用户提交后进入申请队列，管理员批准时调用同一服务并授予项目 ACL。

## 题目归档与运行

Dashboard 不再把每个运行都当作一张独立题目卡。后端根据项目内题目源文件计算 canonical SHA-256 身份，前端据此聚合同题的多次运行。

归档卡会显示：

- 题目标题；
- 运行总数、完成数和运行中数量；
- 最新运行；
- 可展开的历史运行。

如果题目源文件不可用，系统退回以项目名区分运行。归档只影响展示：

- `ongoing/<base_name>/` 仍是进行中项目的存储位置真相；
- `complete/<base_name>/` 仍是已交付项目的存储真相；
- engine 项目的运行状态以 `.factory/state.db` 为权威；
- UI 不会移动、合并或重命名目录。

同名目录同时出现在 `ongoing/` 和 `complete/` 时，列表优先使用 `ongoing/`，避免产生歧义。

## 查看和控制项目

项目工作台提供：

- 当前步骤、状态和诊断摘要；
- checkpoint、日志、文件和渲染预览；
- 人工咨询请求与回答；
- Step 3 方案选择；
- 按权限开放的暂停、恢复和终止动作。

`checkpoint.md` 仅用于显示，不是工作流权威状态。需要判断真实步骤时，在仓库根目录运行：

```bash
./run_paper.sh --infer-step ongoing/<base_name>
```

该命令对新建/已迁移项目读取 `.factory/state.db`，对未迁移项目调用冻结的 Legacy 产物推断。

完成归档保持只读；不要把 `complete/` 目录存在本身当作当前质量契约 PASS。

## 人工咨询

启用咨询的项目可能在 preflight、Step 4 或动态请求处暂停。Web 会展示请求内容，提交回答后写入 `human_review.md`，解析对应 gate，并启动统一 worker 恢复运行。提交携带当前 revision；过期页面会收到冲突响应，不会覆盖较新的状态。

咨询 gate 采用退出并等待恢复的方式，不在后台持锁阻塞。回答前应核对项目、gate 和当前状态，避免把旧请求提交到新运行。

## Step 3 方案选择

启用 `selection/config.json` 后，项目会在 Step 3 前等待 PRIMARY/AUXILIARY 选择。Web 与 CLI 是并行入口。

CLI 示例：

```bash
cd /home/tfisher/paper_factory
python3 scripts/selection_gate.py select-step3 ongoing/<base_name> \
  --primary m2 --aux m1 --reason "Prefer the verified stream"
```

默认会写入 `selection/step3_decision.json`、同步 `human_review.md` 并启动 worker 恢复项目；调试时可加 `--no-resume`。Web 选择请求使用 project revision 防止旧页面覆盖较新的控制操作。

## 用户和项目审批

管理员面板中的操作会写入 `web/auth.db` 和审计日志：

- 用户：approve、reject、disable、delete；管理员账号不能删除。
- 项目申请：approve 或 reject。
- 项目批准成功后：调用 launcher、记录启动结果、授予申请人 owner ACL。

如果 launcher 失败，申请会标记为 `failed`，不会伪装成已创建项目。

## Secret 与审计可见性

管理员只能看到 Secret Manager 的元数据、绑定状态和访问状态，不应看到 secret 值或片段。审计日志记录 actor、action、target 和时间等治理信息。

任何凭据轮换、旧版本禁用或 secret 删除都是运维变更，按 [`docs/deployment/DEPLOYMENT.md`](docs/deployment/DEPLOYMENT.md) 执行并单独确认。

## 故障排查

### 上传失败

- 检查格式和 100 MB 默认限制。
- 压缩包必须含 PDF 或 Markdown 题目文件。
- 检查后端日志是否报告归档目录穿越或无可识别题目。

### 普通用户看不到项目

检查用户是否为 `active`、项目申请是否获批，以及 `project_acl` 是否已授予该用户。

### 状态看起来过期

刷新页面并检查 WebSocket；必要时用 `run_paper.sh --infer-step` 对照权威运行状态。

### 生产问题

不要采用本目录旧完成报告中的命令。只使用 [`docs/deployment/DEPLOYMENT.md`](docs/deployment/DEPLOYMENT.md) 的服务名、路径、预检、回滚和 live smoke。
