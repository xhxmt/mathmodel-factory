# Cloud Solver 安全隔离状态

**当前状态日期：2026-07-29**

Cloud Solver 当前处于安全隔离状态。非平凡求解任务继续通过
`solver_submit.sh` 执行，但默认只使用本地求解器；不要通过项目
`.env.cloud` 或 Web 控制台重新启用 Cloud Run 执行。

## 当前合同

- Cloud Run `solver-api` 禁止匿名调用；`allUsers` 不拥有
  `roles/run.invoker`。
- `/health` 也位于 Cloud Run IAM 后面。`scripts/cloud_solver_monitor.py`
  获取 ID Token 后检查，不允许匿名探测。
- API 的提交、状态、输出和删除端点同时要求应用层
  `X-Solver-Token`。未配置 `SOLVER_API_TOKEN` 时这些端点返回 503，不能
  静默跳过认证。
- `SOLVER_EXECUTION_ENABLED` 默认且在线上明确为 `false`。即使未来配置
  了应用 Token，提交端点仍保持隔离，直到完整的 P0 输入隔离、路径、
  环境变量和资源上限合同通过验收。
- `CLOUD_SOLVER_QUARANTINED` 默认 `true`。`solver_submit.sh` 和
  `scripts/solver_router.sh` 因而忽略遗留的云端启用配置并走本地路径。
- `solver-runner` 只在专用 `solver-jobs` Bucket 上拥有对象管理权限，不
  再拥有项目级 Storage Object Admin。

## 当前使用方式

从项目目录提交非平凡求解任务：

```bash
../../solver_submit.sh --type python --max-time 1800 models/solve.py
```

检查与等待任务仍使用统一 CLI：

```bash
../../solver_submit.sh --status <jobid>
../../solver_submit.sh --wait <jobid>
```

返回的任务 ID 应为 `local_<type>_...`。若出现 `cloud_<type>_...`，停止该
任务并检查 `CLOUD_SOLVER_QUARANTINED` 是否被错误覆盖为 `false`。

## 只读运维检查

以下命令不会输出 Secret 值：

```bash
gcloud run services get-iam-policy solver-api \
  --region=europe-west4 --project="$GCP_PROJECT_ID"

scripts/cloud_solver_monitor.py --check
```

预期结果：Cloud Run IAM 中不存在 `allUsers` Invoker；监控使用当前操作
身份认证后可以读取服务健康，但会明确报告执行仍处于 quarantine。

## 解除隔离的前置条件

不要只修改环境变量解除隔离。至少需要同时满足：

1. 任务 ID、脚本名和工作文件路径完成规范化与越界拒绝。
2. 请求、文件数量、输入输出字节数、日志和子进程具有硬上限。
3. 用户环境变量改为安全允许列表，执行进程不继承控制服务敏感环境。
4. 输入只读、输出独立，并完成清单与哈希校验。
5. 云端只暴露实际安装并通过冒烟测试的运行时。
6. 恶意输入回归测试、私有 IAM 集成测试和最小权限检查全部通过。
7. 运维人员完成显式安全复审和受控部署。

历史部署、成本估算和多运行时说明位于 `docs/deployment/`，在隔离解除前
只能作为历史设计材料阅读，不能作为当前操作指令。
