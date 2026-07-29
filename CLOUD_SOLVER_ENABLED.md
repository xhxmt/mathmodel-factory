# Cloud Solver 当前安全合同

**状态日期：2026-07-29**

Cloud Solver 仍处于安全隔离状态。默认求解入口是 `solver_submit.sh`，且
`CLOUD_SOLVER_QUARANTINED=true` 时只走本地求解器。P0 代码加固已在当前
工作区完成并通过本地镜像验证，但尚未部署到线上；不要仅通过修改环境变量
解除隔离。

## 线上状态

- Cloud Run `solver-api` 禁止匿名调用，服务 IAM 中不存在 `allUsers`。
- 线上 revision 仍是 `solver-api-p1-e6ec2ad`，镜像标签为
  `p1-e6ec2ad`，`SOLVER_EXECUTION_ENABLED=false`。
- 鉴权策略统一为 Cloud Run IAM。专用无密钥调用身份是
  `solver-invoker@level-night-476302-k0.iam.gserviceaccount.com`，它仅在
  `solver-api` 服务上拥有 `roles/run.invoker`。
- 运行身份 `solver-runner` 只在专用 `solver-jobs` Bucket 上拥有对象管理
  权限，不拥有项目级 Storage Object Admin。

## P0 代码合同

### 鉴权

API 不再维护第二套 `X-Solver-Token`。CLI、监控和 Web 状态检查统一使用
`scripts/cloud_solver_auth.py` 获取 Cloud Run ID Token，audience 为服务
URL。服务账号调用使用无密钥 impersonation：

```bash
export CLOUD_SOLVER_IMPERSONATE_SERVICE_ACCOUNT="solver-invoker@${GCP_PROJECT_ID}.iam.gserviceaccount.com"
scripts/cloud_solver_monitor.py --check
```

认证失败、权限不足、限流、服务错误、超时和网络错误分别报告。401/403
不会被误判成普通服务故障；主动 quarantine 也不会累计故障或触发回退。

### 输入与执行限制

云端仅接受经过能力清单验证的 Python。以下限制是硬上限：

| 项目 | 上限 |
|---|---:|
| HTTP 请求体 | 12 MiB |
| 主脚本 | 1 MiB |
| 单个工作文件 | 2 MiB |
| 工作文件数量 | 64 |
| 输入总量 | 10 MiB |
| 执行时间 | 3600 秒 |
| 单个输出文件 | 16 MiB |
| 输出总量 | 64 MiB |
| 输出文件数量 | 256 |
| 输出目录数量 | 64（含任务私有 `.tmp`） |
| 单路日志 | 8 MiB |
| 文件描述符 | 128 |
| 子进程数量 | 32 |
| 虚拟地址空间 | 6 GiB |

任务 ID、脚本名、工作文件和输出路径使用严格格式；绝对路径、`..`、反斜杠、
重复分隔符、符号链接、设备文件和越界路径均被拒绝。输入由控制进程写入
root 所有的只读目录，输出位于独立目录。Solver 子进程降权到 UID/GID
10001，并且只接收固定基础环境及线程数/随机种子允许列表，不继承
Control API、GCP 或 Secret 环境变量。可信资源限制包装器以隔离模式启动，
任务提供的 `sitecustomize.py` 只能在限制已经生效后运行。共享 `/tmp`、`/var/tmp` 和
`/dev/shm` 对 Solver UID 不可写，同一实例同时只接受一个活动任务。项目
`.env.cloud` 按固定键值白名单解析，不再作为 Shell 脚本执行，也不能覆盖
全局 quarantine、GCP 目标、Invoker 身份、客户端程序、鉴权后端或能力
清单路径。

### 能力与镜像

`cloud/runtime_capabilities.json` 是 API、Web 和 Shell 路由的共同能力来源。
当前唯一启用运行时是 Python；Julia、R、MATLAB 和 Gurobi 在提交阶段返回
`RUNTIME_UNAVAILABLE`。Docker 构建会执行 `cloud/smoke_test.py`，验证声明
的运行时、包版本、线性代数和最小 LP 求解。

`cloud/cloudbuild.yaml` 使用 `${BUILD_ID}` 不可变标签部署，`latest` 只用于
人工浏览。revision 保存 Build ID、commit SHA 和解析后的镜像引用，并启用
verified provenance。部署前检查及 digest 回滚命令：

```bash
python3 scripts/validate_cloudbuild.py cloud/cloudbuild.yaml
scripts/rollback_cloud_solver.sh --digest sha256:<64-hex>
# 核对 dry-run 后才追加 --execute
```

构建和回滚均保持 `SOLVER_EXECUTION_ENABLED=false`。

## 当前使用方式

非平凡任务继续通过本地统一入口提交：

```bash
../../solver_submit.sh --type python --max-time 1800 models/solve.py
../../solver_submit.sh --status <jobid>
../../solver_submit.sh --wait <jobid>
```

隔离期间返回的任务 ID 应为 `local_python_...`。若出现 `cloud_python_...`，
停止任务并检查 operator 级 quarantine 是否被错误关闭。

## 验证

```bash
.venv/bin/pytest -q \
  tests/test_cloud_solver_auth.py \
  tests/test_cloud_solver_security.py \
  tests/test_cloud_solver_deployment.py \
  tests/test_cloud_solver_api_persistence.py \
  tests/test_cloud_solver_monitor.py \
  tests/test_cloud_solver_routing.py

docker build -t paper-factory-solver-p0:test cloud
```

健康检查需要 IAM 身份；匿名 `curl` 返回 403 是预期行为。

## 解除隔离仍存在的阻断项

低权限 UID、只读输入和清洁环境能阻止脚本读取 Control API 的进程环境，
但同一 Cloud Run 实例中的任意代码仍可能访问实例 metadata，进而取得
`solver-runner` 身份。该身份为了持久化任务仍拥有专用 Bucket 对象管理
权限；此外目录扫描器不是独立文件系统配额。因此当前 subprocess 边界不能
视为对恶意代码的完整云身份和存储隔离。

在执行迁移到独立 Cloud Run Job（独立最小权限身份和网络边界），或经验证
的等价 sandbox 落地前，不得同时设置 `SOLVER_EXECUTION_ENABLED=true` 和
`CLOUD_SOLVER_QUARANTINED=false`。这是解除 quarantine 的 Blocker，不是可
通过更多路径校验替代的文档步骤。
