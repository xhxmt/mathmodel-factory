# 设计：用户登录、管理员审批与项目权限系统

> 日期: 2026-07-02  
> 状态: 已讨论确认，待用户评审  
> 目标: 将 tfisher.de 从单管理员控制台升级为多用户系统；用户可自助注册，管理员批准账号和项目启动后，用户拥有自己项目的完整操作权限。

## 背景

当前 Web 控制台的认证模型很简单：

- `web/.env` 提供一个管理员密码；
- `web/backend/auth.py` 启动时在内存中构造单个 `admin` 用户；
- JWT 只包含 `sub` 和 `role`；
- 后端项目接口只检查“是否已登录”，不检查项目归属；
- 所有已登录用户事实上等同管理员。

这对单人本机控制台足够，但不适合公开的 tfisher.de。新系统需要同时控制两类资源：

1. **账号访问权**：谁可以登录系统。
2. **项目资源权**：谁可以启动消耗本机/模型资源的建模项目。

## 用户选择

用户选择的账号策略是：

- 普通用户可以自助注册；
- 注册后账号状态为 `pending`；
- 管理员批准后用户才可以正常使用系统。

本设计在此基础上增加项目启动审批：即使账号已批准，普通用户新建项目也先进入 `pending_project` 状态，管理员批准后才真正调用 `launch_agents.sh new`。

## 方案比较

### 方案 A：JSON 文件存储

用 `web/users.json`、`web/project_requests.json` 保存账号、审批和项目归属。

优点：

- 实现最快；
- 与现有 `web/model_config.json` 风格接近；
- 容易手工查看。

缺点：

- 并发写入和崩溃恢复脆弱；
- 审批记录和权限查询会越来越复杂；
- 密码哈希、审计日志和项目 ACL 混在 JSON 中不够稳妥。

### 方案 B：SQLite 本地数据库

用 Python 标准库 `sqlite3` 在本机维护认证和审批状态。

优点：

- 不引入外部数据库服务；
- 单机部署简单；
- 支持事务、唯一约束和查询；
- 适合账号、项目申请、ACL、审计日志这类结构化状态；
- 易备份和迁移。

缺点：

- 需要新增数据库初始化和迁移逻辑；
- 需要明确 DB 文件位置和权限。

### 方案 C：Postgres 或外部身份系统

引入 Postgres、OAuth、Keycloak 或类似身份系统。

优点：

- 长期扩展性最好；
- 适合大量用户和多服务部署。

缺点：

- 当前系统是本机单服务，运维成本过高；
- 会把第一版权限系统做重。

**最终选择：方案 B，SQLite 本地数据库。**

## 范围

### 包含

- 自助注册；
- 管理员批准/拒绝用户；
- 用户登录状态和角色；
- 普通用户提交项目创建申请；
- 管理员批准/拒绝项目申请；
- 批准后自动创建项目并记录 owner；
- 普通用户只看到自己的项目；
- 普通用户对自己的项目拥有完整项目权限；
- 管理员可查看和操作所有项目；
- 前端管理员后台和用户申请入口；
- 审计日志记录关键审批和权限动作。

### 不包含

- 邮箱验证；
- 找回密码；
- OAuth / 第三方登录；
- 用户组和多管理员分级权限；
- 项目多人协作；
- 配额计费；
- 公开 API token；
- 跨机器数据库同步。

这些可以以后扩展，但第一版不做。

## 数据模型

SQLite 文件默认放在：

- `web/auth.db`

后端 `Settings` 增加：

- `auth_db_file: Path = factory_root / "web" / "auth.db"`

### `users`

字段：

- `username TEXT PRIMARY KEY`
- `password_hash TEXT NOT NULL`
- `role TEXT NOT NULL`，取值 `admin` / `user`
- `status TEXT NOT NULL`，取值 `pending` / `active` / `rejected` / `disabled`
- `display_name TEXT`
- `created_at INTEGER NOT NULL`
- `approved_at INTEGER`
- `approved_by TEXT`
- `rejected_at INTEGER`
- `rejected_by TEXT`
- `rejection_reason TEXT`

规则：

- `admin` 账号由 `web/.env` 中的 `ADMIN_PASSWORD` 首次引导创建；
- 已存在的 `admin` 不在每次启动时被覆盖，避免部署时意外改密码；
- 普通注册用户默认 `role=user,status=pending`；
- 只有 `status=active` 的账号可以正常登录。

### `project_requests`

字段：

- `id INTEGER PRIMARY KEY AUTOINCREMENT`
- `requester TEXT NOT NULL`
- `base_name TEXT NOT NULL`
- `problem_path TEXT NOT NULL`
- `no_start INTEGER NOT NULL DEFAULT 0`
- `consult INTEGER NOT NULL DEFAULT 0`
- `status TEXT NOT NULL`，取值 `pending` / `approved` / `rejected` / `failed`
- `created_at INTEGER NOT NULL`
- `decided_at INTEGER`
- `decided_by TEXT`
- `decision_note TEXT`
- `launched_at INTEGER`
- `launched_base_name TEXT`
- `launch_output TEXT`
- `failure_reason TEXT`

规则：

- 普通用户调用“新建项目”时只创建申请，不直接启动项目；
- 新申请的 `base_name` 不能与现有项目、待审批申请、已批准申请冲突；已拒绝申请的项目名可以重新申请；
- 管理员批准申请时，后端才执行 `launch_agents.sh new ...`；
- 如果启动失败，申请变为 `failed`，并保存 stderr/stdout 摘要；
- 管理员也可以走审批流，但默认仍允许管理员直接创建项目。

### `project_acl`

字段：

- `base_name TEXT NOT NULL`
- `username TEXT NOT NULL`
- `role TEXT NOT NULL`，第一版只用 `owner`
- `created_at INTEGER NOT NULL`
- `created_by TEXT NOT NULL`
- 主键：`(base_name, username)`

规则：

- 项目申请被批准并成功启动后，写入 `project_acl(base_name, requester, owner)`；
- `owner` 拥有该项目所有项目级权限；
- 管理员不需要写入 ACL，始终拥有全部项目权限。

### `audit_log`

字段：

- `id INTEGER PRIMARY KEY AUTOINCREMENT`
- `actor TEXT NOT NULL`
- `action TEXT NOT NULL`
- `target_type TEXT NOT NULL`
- `target_id TEXT NOT NULL`
- `created_at INTEGER NOT NULL`
- `metadata_json TEXT`

记录事件：

- 用户注册；
- 用户批准/拒绝/禁用；
- 项目申请提交；
- 项目申请批准/拒绝；
- 项目启动失败；
- 项目 ACL 授权。

## 后端设计

### 新模块

#### `web/backend/auth_store.py`

职责：

- 初始化 SQLite schema；
- 引导管理员账号；
- 注册用户；
- 查询用户；
- 审批用户；
- 创建/查询/审批项目申请；
- 查询项目 ACL；
- 写审计日志。

该模块不依赖 FastAPI，便于单元测试。

#### `web/backend/access_control.py`

职责：

- `require_admin(user)`；
- `can_view_project(user, base_name)`；
- `can_manage_project(user, base_name)`；
- `filter_visible_projects(user, projects)`。

第一版规则：

- `admin` 可以做所有事；
- `user` 只能访问 `project_acl` 中属于自己的项目；
- `pending/rejected/disabled` 用户不能访问项目 API。

### 认证接口

新增：

- `POST /api/auth/register`
  - 输入：`username,password,display_name`
  - 输出：`status=pending`
- `GET /api/auth/me`
  - 增加返回 `status`
- `GET /api/admin/users`
  - 管理员查看用户列表
- `POST /api/admin/users/{username}/approve`
- `POST /api/admin/users/{username}/reject`
- `POST /api/admin/users/{username}/disable`

修改：

- `POST /api/auth/login`
  - `pending` 用户返回 `403 USER_PENDING`；
  - `rejected` 用户返回 `403 USER_REJECTED`；
  - `disabled` 用户返回 `403 USER_DISABLED`；
  - 只有 `active` 用户返回 JWT；
  - JWT 继续包含 `sub,role,exp`，可额外包含 `status=active`。

### 项目接口

新增：

- `GET /api/project-requests`
  - 管理员看全部；
  - 普通用户看自己的申请。
- `POST /api/project-requests`
  - 普通用户提交项目申请；
  - 上传后的 `problem_path` 可复用现有上传接口返回值。
- `POST /api/admin/project-requests/{id}/approve`
  - 管理员批准并启动项目；
  - 成功后写 `project_acl`。
- `POST /api/admin/project-requests/{id}/reject`

修改：

- `POST /api/projects/new`
  - 管理员仍可直接创建；
  - 普通用户不再直接创建，而是返回 `403 PROJECT_APPROVAL_REQUIRED` 或内部转为申请。
- `GET /api/projects`
  - 管理员返回全部项目；
  - 普通用户只返回自己拥有的项目。
- 所有 `GET /api/projects/{base}/...` 和 `POST /api/projects/{base}/...`：
  - 管理员允许；
  - 项目 owner 允许；
  - 其它用户返回 404 或 403。

推荐对非 owner 返回 404，避免暴露项目名；管理员接口仍返回真实状态。

### WebSocket

现有 WebSocket ticket 只保存 `sub/role`。第一版保持这个结构，但推送策略调整：

- 管理员连接收到所有项目更新；
- 普通用户只收到自己项目的更新；
- 项目申请审批事件只推给相关用户和管理员。

如果第一版实现成本需要控制，可以先在收到 WebSocket 更新后让前端重新拉取 `/api/projects`，后端列表过滤保证不会泄露其它项目。

## 前端设计

### 登录页

增加：

- “注册账号”切换；
- 注册表单字段：用户名、显示名、密码、确认密码；
- 注册成功后显示“账号等待管理员批准”。

登录错误状态：

- `USER_PENDING`：显示“账号等待管理员批准”；
- `USER_REJECTED`：显示“账号申请已被拒绝”；
- `USER_DISABLED`：显示“账号已停用”。

### 主界面角色分流

`useAuth()` 保存：

- `username`
- `role`
- `status`

管理员显示：

- 全部项目；
- 待审批用户；
- 待审批项目；
- 模型管理；
- 原有项目控制入口。

普通用户显示：

- 我的项目；
- 我的项目申请；
- 新建申请；
- 自己项目的工作区。

### 项目创建体验

管理员：

- “新建项目”保持现有能力；
- 可选择是否走审批流，但默认直接创建。

普通用户：

- “新建项目”按钮文案改为“申请项目”；
- 上传题目文件和填写项目名后，创建 `project_request`；
- 申请状态为：
  - `pending`：等待管理员审批；
  - `approved`：项目已创建；
  - `rejected`：显示拒绝原因；
  - `failed`：显示启动失败原因。

### 管理员后台

第一版可以做成现有控制台中的一个新 overlay 或 tab：

- `用户审批`
  - pending 用户列表；
  - 批准/拒绝/停用动作。
- `项目审批`
  - pending 项目申请列表；
  - 显示申请人、项目名、题目文件、创建时间；
  - 批准/拒绝动作；
  - 批准后显示启动结果。

## 安全边界

必须满足：

- 密码只存 bcrypt hash；
- 注册用户名只允许 `[A-Za-z0-9_-]`；
- 普通用户不能枚举其它项目；
- 普通用户不能读取其它项目文件、日志、PDF、诊断、咨询；
- 普通用户不能改全局模型配置；
- 上传文件仍走现有安全路径和格式校验；
- 项目启动只在管理员批准后执行；
- 审批和授权动作写审计日志。

## 迁移策略

第一版上线时：

1. 启动后端时创建 `web/auth.db`；
2. 如果 `users` 表没有 `admin`，用 `ADMIN_PASSWORD` 创建 `admin,status=active`；
3. 已存在项目默认只有管理员可见；
4. 如需把历史项目授权给用户，可后续添加管理员“分配 owner”动作。第一版不强制迁移历史项目 owner。

这样可以避免上线后普通用户突然看到历史项目。

## 错误处理

- 注册用户名重复：`409 USER_EXISTS`；
- 账号未批准登录：`403 USER_PENDING`；
- 普通用户直接创建项目：`403 PROJECT_APPROVAL_REQUIRED`；
- 项目申请 base_name 冲突：`409 PROJECT_NAME_EXISTS`；
- 批准项目但启动失败：申请状态变 `failed`，保留 `failure_reason`；
- 非 owner 访问项目：`404 PROJECT_NOT_FOUND`。

## 测试策略

后端单元测试：

- 管理员引导创建；
- 用户注册默认 pending；
- pending/rejected/disabled 用户不能登录；
- 管理员批准后可登录；
- 普通用户提交项目申请；
- 普通用户不能直接创建项目；
- 管理员批准项目后调用 launcher，并写 ACL；
- 用户只能看到自己的项目；
- 用户不能读取别人的项目文件；
- 管理员能看到全部项目；
- 审计日志写入。

前端构建验证：

- 登录/注册表单编译；
- 管理员后台入口编译；
- 普通用户申请项目 UI 编译。

集成验证：

- 注册新用户；
- 管理员批准用户；
- 用户提交项目申请；
- 管理员批准项目；
- 用户看到并控制自己的项目；
- 另一个用户看不到该项目。

## 部署策略

1. 先在本机运行后端测试和前端构建；
2. 部署到 tfisher.de；
3. 首次启动创建 `web/auth.db`；
4. 用现有 admin 登录；
5. 创建测试普通用户并走完整审批流；
6. 确认 `/api/projects` 对管理员和普通用户返回不同结果；
7. 确认普通用户无法访问非 owner 项目文件。

## 开放问题

本设计先固定以下决策：

- 自助注册需要管理员批准；
- 项目启动也需要管理员批准；
- 普通用户获批项目后拥有该项目全部权限；
- 历史项目默认只对管理员可见；
- 第一版不做多人协作。

如果后续需要，可以扩展：

- 管理员手动分配历史项目 owner；
- 一个项目多个用户；
- 项目只读/可管理权限拆分；
- 用户配额和并发限制；
- 邮箱通知。
