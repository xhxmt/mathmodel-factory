# Changelog

## 2026-11-05 — 活性优化阶段 1c：pause / kill 真正停下进程，否则什么都不提交

依据 `docs/architecture/NEXT_OPTIMIZATION_EXECUTION_PLAN.md` 阶段 1c 与 REV3 W1b。验收运行 run2 从 rev 437 起事件链无法重放（`aggregate_valid=False`），此后三次 pause（rev 713、771、789）都先提交了 PAUSED，随后因"没有可用的启动身份"而改记 `RUNNER_EXIT_UNVERIFIED`，runner 进程仍在运行。REV3 已查明身份其实可以解析（rev 709 的 `RUN_STARTED` 带有 pid、lease 和与进程相符的 identity），是 `_recorded_runner_identity` 在库无效时直接返回了空。

- **能否停进程与库是否有效脱钩**：`factory_core/service.py`。身份令牌会与活进程的 `_process_identity` 比对，这个比对本身就是证据，不依赖状态库；不再以 `aggregate_valid` 为前提。来源仍限于与当前 pid 和 lease 相符的 `WORKER_LAUNCHED` / `RUN_STARTED`，从不只凭 pid。
- **提交前先判断**：新增 `_runner_stop_identity`，在提交 PAUSED / KILLED 之前决定如何停进程。没有记录的 pid、pid 已不存在（身份读不到且 `_pid_is_live` 也确认不存在）、或 pid 已被另一个进程复用（令牌不符，说明原 runner 已退出）时，直接提交、不发信号。pid 上有活进程却没有可核对的启动身份，或身份读不到而 pid 仍存在时，抛出 `InvalidTransition`（消息以 `STOP_IDENTITY_UNAVAILABLE` 开头），什么都不提交，提示操作员先自行结束该进程。记录的 runner 就是调用方自己时，以 `STOP_SELF` 拒绝，因为无法从自身确认退出。过期的 `expected_revision` 先于进程检查报告为 `RevisionConflict`。以前这种情况会提交 PAUSED，再改记 INTERRUPTED，与进程树矛盾。
- 其余顺序不变：先提交以收回 lease，再按身份结束整个进程树；结束无法确认时仍记 `RUNNER_EXIT_UNVERIFIED`。
- 测试：新增 8 例（事件链被篡改后仍能停下 runner、身份不可读但 pid 存在、调用方自身、过期 revision，各含 pause 与 kill），更新了 3 个固定旧行为的测试。

## 2026-11-05 — 活性优化阶段 1a：检查器无结论不再算内容失败

依据 `docs/architecture/NEXT_OPTIMIZATION_EXECUTION_PLAN.md` 阶段 1a。2026-10-15 的验收运行 run2 中，Step 10 的检查脚本因 `/tmp` 写满而崩溃，被记为 `PAPER_AUDIT_FAILED`（`AUDIT_REPAIR_REQUIRED`），5 次内容尝试全部耗尽。

- **检查器三值化**：`factory_core/audit/incremental.py`。检查脚本非零退出时，若被信号终止、日志最后一行是主机资源耗尽（磁盘满、配额、内存、文件句柄），或日志以未捕获的 Python traceback 结尾（任意异常名，traceback 之后只有缩进的栈帧和最后的异常行），记为"无结论"（`StageCheck.indeterminate`），与超时同样归入 `INDETERMINATE` / `TRANSIENT_INCREMENTAL_AUDIT`。各检查脚本用于 FAIL 的退出码不同（1、2、3），因此不靠退出码判断。正常退出并报告缺陷的脚本仍是 FAIL，即使报告前文引用了异常名或资源错误字样。
- **确定的缺陷优先**：以前只要有一个硬检查超时，整次审计就是 INDETERMINATE，同一次里真实的缺陷会被掩盖。现在只有当所有硬失败都无结论时才判 INDETERMINATE；有确定的失败时，`failed_check` 与 `repair_step` 按确定的失败计算；所有硬失败都无结论时，仍按这些无结论的检查给出路由信息。`evidence.indeterminate_checks` 列出无结论的检查。
- **台账**：超时或无结论的检查不再写入 `audit_issue_ledger.md`，对应行保留上一次有结论时的状态，主机故障不会留下 OPEN 的 BLOCKING 条目去触发修复或评审循环门禁。
- **引擎预算**：`factory_core/engine.py`。`NON_CONTENT_ERROR_CLASSES` 中的错误类别（目前为 `TRANSIENT_INCREMENTAL_AUDIT`）不消耗 Step 的内容尝试次数，另有 `INFRASTRUCTURE_RETRY_BUDGET = 3` 次重试预算，用尽后以独立的 `PERMANENT_AUDIT_INFRASTRUCTURE` 终止，不会被当成内容失败。次数从事件流推导（本次尝试序列起点为最近一次阶段子任务选择或该 Step 最近一次 attempt 1 的 `STEP_STARTED`，取较晚者），不新增回放状态字段，也不回退 `attempt`（它参与 prompt 回执的唯一键）。失败事件新增 `content_attempts_used`，无结论失败另带 `infrastructure_retries` / `infrastructure_retry_budget`。
- **回放**：失败统计按同一规则计次，非内容类别及其预算用尽后的终止类别都不算内容尝试。历史上 run2 Step 10 的 5 次失败记录的是旧检查器给出的 `AUDIT_REPAIR_REQUIRED`，夹具里没有检查日志，回放无法重新判定它们，所以基线中的非内容尝试损失仍为 56 次；新行为由复现"磁盘写满导致 traceback"的检查器测试证明。
- 测试：新增 11 例（检查器 7 例，引擎 3 例，回放 1 例）。

## 2026-11-05 — 活性优化阶段 0：运行历史回放、磁盘临时目录与路线预检

依据 `docs/architecture/NEXT_OPTIMIZATION_EXECUTION_PLAN.md` 阶段 0。

- **运行历史回放（新）**：`scripts/extract_run_history.py` 在项目提交租约下读取 `state.db` 主文件与 WAL 的私有快照（与 `isolated_state_snapshot_ro` 相同，看得到已提交的 WAL 帧，不在源目录创建或修改任何文件），抽取不含论文内容的夹具（事件、每次提交的分类变更集合、solver 作业归属；去掉哈希链、prompt 收据，任何账户的家目录与疑似密钥被替换）。`scripts/replay_run_history.py` 对夹具做三件事：时间账（保留 / 重做 / 中途回退 / 空转；PAUSED 只挂起尝试，RESUMED 后直接成功的仍记为保留，暂停时段不计入工作；`STEP_REOPENED` 与 `STAGE_SEMANTIC_REOPENED` 一样结束当前尝试）、按可替换规则重新判定每个变更集合是否回退、按人工标注统计非内容原因消耗的尝试次数。只有提交路径（`STEP_SUCCEEDED`、`STAGE_SEMANTIC_REOPENED`）上的变更集合参与回退判定，与引擎的 `_complete_stage_task` / `_fail_stage_task` 分工一致。
- **夹具与基线**：`tests/fixtures/run_history/` 收录 2026-08-03 至 2026-10-16 的 16 次运行（1.8 MB），`labels.json` 为 67 条有出处的失败原因标注，`_baseline_8e8f872.json` 为当前 main 的基线：316.9 h 挂钟时间中保留的模型工作 23.2 h（7.3%）、空转 185.0 h；346 个变更集合中 299 个参与判定，记录回退 49 次，当前规则判定 46 次；非内容原因消耗尝试 56 次（5.8 h）。
- **回放忠实性**：在当前分类器（`9f8ba8f601d1`）下记录的 47 个提交变更集合中，46 个逐条复现引擎当时的判定；唯一差异（run2 rev 774）来自验收主机上操作员手工打的本地豁免补丁，测试中以带出处的已知例外登记。run2 在 rev 437 起因人工改库导致事件链断开，但其后的判定事件仍真实发生，故保留全部事件用于决策回放（与方案初稿"只用到 rev 436"不同）。
- **活性回归门禁**：`tests/test_run_history_replay.py` 断言回退判定次数与非内容尝试损失不得高于基线；以"总是回退"规则验证门禁确实会失败（299 > 46）。
- **磁盘临时目录**：`factory_core/runtime_env.py`。当继承的临时目录位于 tmpfs/ramfs 时，worker 启动环境以及 `run`、`worker`、`advance`、兼容入口执行期间把 `TMPDIR` 指向 `run_state/tmp`；`FACTORY_TMPDIR` 可显式指定。进程内调用结束后恢复原环境与 `tempfile` 缓存。判断的是 `tempfile` 实际会用的目录（与 `tempfile` 一样按候选顺序逐个做一次真实写入探测，不存在、不可写或磁盘已满的 `TMPDIR` 会被跳过）；无法判断文件系统类型时不做改动。
- **路线预检（新命令）**：`factory preflight [<project_dir>] [--steps] [--min-free-gb] [--offline]`，只读。按调度器同样的候选链（项目/默认分配 + 目录默认；启用联合建模的项目，Step 2 改为检查固定的 Claude 联合建模执行器）逐个检查可执行文件、凭据是否存在（从不输出取值）、DeepSeek 余额（`/user/balance`）或 OpenAI 兼容端点 `/models`，以及临时目录与项目所在磁盘空间。402、`is_available=false`、凭据缺失、Node < 24 或启动器不是文件的 DSH、临时目录位置被普通文件占用均为 FAIL；无法探测，或 2xx 响应不是文档规定的形状（DeepSeek 须有 `is_available=true` 与 `balance_infos` 列表，OpenAI 兼容端点须返回带 `data` 列表的模型清单），为 UNKNOWN，不记为 PASS。`--steps` 与 `--min-free-gb`（须为有限的非负数，0 表示不检查）的非法取值按用法错误退出。任一 Step 的全部候选失败时退出码为 3。
- 测试：新增 58 例（回放与抽取 16 例，运行环境与预检 42 例）。

## 2026-09-22 — TUI 客户端 M4：打磨与文档（M0–M3 收尾）

- 用 `uvx ruff check`（ruff 0.16.8）对 `apps/tui/` 与 TUI 测试做了一次真实 lint，而不是目测。修掉 13 项：1 处未使用导入（F401）、4 处导入顺序（I001）、7 处多余引号注解（UP037，各文件都有 `from __future__ import annotations`，引号本就冗余）、1 处函数末尾多余的 `return None`（RET501/PLR1711），并把 3 处自返回 dunder（`__aenter__`/`__aiter__`）的注解改为 `typing.Self`（PYI034）。
- **有意保留、并逐条给出理由的 13 项**（本仓没有 ruff 配置，ruff 的默认宽集并非本仓既定标准，因此不盲目照改）：
  - `RUF012`（4 处）：Textual 的 `CSS`/`BINDINGS` 就是普通类属性，加 `ClassVar` 属噪声。
  - `TRY004`（6 处）：归一化函数对「载荷不是 dict」抛 `ValueError`。改成 `TypeError` 反而**更危险**——各调用点统一捕获 `ValueError` 并包装为 `ControlPlaneError`，拆出第二种异常类型会在 6 个调用点制造未捕获路径，而行为毫无差别。
  - `UP017`（1 处）：`timezone.utc` → `datetime.UTC` 会让该文件与本仓主流不一致（全仓 `timezone.utc` 6 处、`datetime.UTC` 0 处）。
  - `SIM102`（1 处）：合并嵌套 `if` 会让「已停止项目只取一次」的注释离开它所解释的那个条件。
  - `BLE001`（1 处）：重连循环里的 `except Exception` 是刻意设计——任何失败都应转为退避重连，而不是让 feed 任务静默死掉；`CancelledError` 已在其上方单独放行。
- footer 去重：应用级 `Ctrl+Q` 改为 `show=False`，不再与各屏的 `q` 重复显示「退出」（实测 footer 现为 `o 详情 | r 重连 | q 退出`）。
- 资源检查：以 `-W error::ResourceWarning` 跑全部 TUI 测试，无任何告警——我在 M2 复核里提到的「未关闭 socket」并不成立，此处撤回该条目。
- 文档：`README.md` 新增「TUI 客户端（终端）」一节及「包含内容」条目；`web/README.md` 新增「终端客户端（TUI）」一节，含启动命令、完整键位表与刷新节奏说明。
- 测试 74 例通过（67 TUI + 7 部署 preflight）；端到端冒烟（登录 → 项目表 → 详情 → 日志 → 过滤）复跑通过。


## 2026-09-22 — TUI 客户端 M3：日志尾随（/logs 轮询）

- `apps/tui/client.py`：新增 `project_logs(base_name, lines=200)`，行数下限夹到 1（后端为 `max(1, lines)`），使用 `HEAVY_TIMEOUT`，`base_name` 经 URL 转义。
- `apps/tui/contracts.py`：新增 `LogsView` 与 `normalize_logs`。后端在无日志时返回 `{"logs": []}` 且**不带 `file` 键**，归一化按缺省处理。
- `apps/tui/screens/detail.py`：新增日志面板（`l` 在「阶段与产物」与「日志」两个下方面板间切换），显示文件名、显示/总行数、跟随/暂停状态与最后更新时间。`p` 暂停/恢复跟随，`f` 过滤（子串、大小写不敏感、边输边过滤），`end` 跳到末尾并恢复跟随。**Esc 在过滤框打开时先关过滤框**，不会把操作员直接弹出屏幕。
- 轮询策略（刻意保守）：单一 5s 定时器；重投影每 3 个 tick（15s）刷新一次，日志每 tick 一次——一个定时器服务两种节奏，避免为日志再引入一个更快的时间源。
- **只在必要时才轮询日志**：仅当日志面板可见、处于跟随状态、且（项目在运行 或 尚未取过任何日志）时才请求。已停止的项目只取一次，之后不再打扰；面板隐藏时永不请求。挂载时的 `_load()` 同样只在面板可见时才拉日志。
- 在途守卫按流独立：重投影与日志各有一个在途标志，tick 落在加载中途即被丢弃，不排队也不取消。
- 「无内容」与「取不到」仍可区分：日志加载失败在摘要行显示「日志加载失败：…」，不会被误读成「日志是空的」。
- 测试累计 84 例（含部署 preflight）：新增 `tests/test_tui_logs.py`（10 例）与客户端层日志契约测试。定时器回调被直接调用，因此轮询节奏与其触发条件可**确定性**断言，无需等待真实时间。

### 顺带发现的后端隐患（本里程碑未修改，避免越界）
`/logs` 是普通 `async def`，**没有**像 `/steps`、`/diagnostics`、`/contest-dashboard` 那样走 `run_in_threadpool`（2026-09-18 那次修复只覆盖了后三个），并且它先 `read_text()` **整个文件**再切最后 N 行。本机真实日志最大 **8.2 MB**（4–6.5 MB 的有多个），因此 `?lines=200` 的每次请求都会让后端在**事件循环上同步读取整个文件**、再把其中 99% 丢掉——与 2026-09-18 记录的问题同类。TUI 因此刻意取 5s（而非计划中的 3s），并只在面板可见且项目运行时轮询，以把影响压到最小。彻底的修法是把 `/logs` 移入线程池、并只读文件尾部若干字节；那属后端改动，需另行确认。


## 2026-09-22 — TUI 客户端 M2：项目详情（阶段产物 + 阻塞面板）

- `apps/tui/client.py`：新增只读投影 `project_steps` / `project_diagnostics`，两者使用独立的 `HEAVY_TIMEOUT`（90s）而非 15s 交互上限——后端把它们放在线程池里执行，在大项目上会合法地超过交互时限。该接缝在 M0 就已留出，此处首次真正使用。`base_name` 经 URL 转义。
- `apps/tui/contracts.py`：新增 `Artifact`/`Step`/`StepsView`/`Evidence`/`DiagnosticsView` 及归一化。诊断的阻塞信息在**所有**后端分支中都嵌在 `status` 之下（已核对 `_fallback_status`、`_native_state_unavailable`、`build_project_diagnostics`、replay 失败分支四处）；缺少 `status` 视为契约变更，而非"无阻塞"。evidence 的额外标量字段被折叠进 `summary`，因此后端新增一种 evidence kind 无需改动终端代码。**TUI 不内置 reason_code 映射表**，直接展示后端给出的 `reason_summary`，避免浏览器与终端各自维护一套工作流知识。
- `apps/tui/screens/detail.py`：详情屏 = 进度条 + 阻塞面板（headline / state / 当前动作 / 来源 / 建议动作 / 证据清单）+ 阶段产物表（只渲染有产物的阶段与当前阶段，并标 `← 当前`）+ 汇总行（阶段总数、裁判结论、未决问题、是否已有论文）。
- 刷新策略：单一加载器 + **在途守卫**。若一次加载尚未返回，定时 tick 被丢弃，而不是排队或被取消——取消会让慢项目永远加载不完，排队则会继续压迫已经繁忙的后端。间隔取 15s 而非计划中的 8s，理由是本仓 2026-09-18 的条目记录过后端事件循环被激进轮询压垮的问题。
- 修正两处由测试发现的真实缺陷：
  1. 阶段摘要与加载状态原先写在同一个 `Static` 上，`_load` 结束时的「已更新」会**覆盖**摘要，导致摘要信息实际丢失。现在摘要使用独立的 `#steps-summary`。
  2. `DataTable` 自身绑定了 Enter（`select_cursor`），因此屏幕级 `enter` 绑定在表格获得焦点时**永不触发**，详情根本打不开。改为处理 `DataTable.RowSelected`；`o` 保留为备用键。
- 非阻塞项目显示「无阻塞记录」而非留空；加载失败则接管面板（「诊断加载失败：…」）。两者视觉上可区分，「取不到」不会被误读为「没问题」。
- 测试累计 59 例（含部署 preflight）：新增 `tests/test_tui_reads.py` 与 `tests/test_tui_detail.py`。其中超时预算是**行为验证**而非常量比对——用一个真实本地 HTTP 服务器延迟 250ms，证明 50ms 的交互上限会杀掉 login、却不会杀掉投影。（自定义 `httpx` transport 无法用于此测试：超时由具体 transport 实施，假 transport 根本不会抛 `ReadTimeout`。）


## 2026-09-22 — TUI 客户端 M1：实时项目表（WS 接入）

- `apps/tui/realtime.py`：`/ws` 实时 feed。三条后端事实决定了它的形状：票据**一次性且 60s 过期**（因此每次连接尝试都重新签发，包括首次）；WS **只推项目状态、不推日志**（日志留待 M3 轮询）；断开是常态而非异常（后端重启、账号被禁用、空闲超时都会关闭连接），因此 feed 通过 `on_state` 上报 `connecting`/`live`/`disconnected` 而不抛异常，并以指数退避（1s → 30s 封顶）重连直到 `stop()`。
- 退避在一次成功 live 会话后**重置**，避免长期连接掉线后继承一个已经膨胀的延迟。
- `apps/tui/screens/projects.py`：`DataTable` 项目表，按 `base_name` **原地 upsert**（`update_cell`）而非每 2s 重建——重建会把光标与滚动位置在操作员手下重置。消失的项目被移除；连接状态以 `● 实时` / `◌ 连接中` / `○ 已断开` 常驻显示，不静默假装数据仍然新鲜。
- 修正一处真实缺陷（由测试发现）：`status_update` 中若列表**非空**但所有条目都缺 `base_name`，原先会产出空快照并**清空整个项目表**。现在只有原生空列表（确实没有可见项目）才清空；非空列表解析全失败视为契约问题，不清表。
- 删除 M0 的 `screens/home.py` 占位屏；`ProjectsScreen` 不再接收从未被读取的 `session` 参数。
- `tui` extra 补入 `websockets==16.0`（此前只在 `web` extra 中，`--extra tui` 单独安装不出来）。
- 测试累计 42 例（含部署 preflight）：投递路径由**本地真实 WebSocket 服务器**端到端覆盖（票据握手、URL 构造、帧解码、归一化）；重连与退避由脚本化 connector + 注入 sleep 确定性断言（含 30s 封顶与 live 后重置）；另覆盖坏帧忽略、不可键快照不清表、`stop()` 幂等。已对真实后端确认 `/ws` 端点形状：无票据或错误票据在握手期即被拒绝。


## 2026-09-22 — TUI 客户端 M0：骨架与登录（Web 控制平面的终端客户端）

- 新增 `apps/tui/`：Textual 终端客户端，定位为 `web/backend` 的**只读客户端**，不复制任何工作流逻辑——它渲染的字段全部来自现有投影，避免浏览器与终端两套投影各自漂移。
- 新增 `tui` 可选依赖组（`textual==8.2.8`）与入口点 `factory-tui`（同时支持 `python -m apps.tui`），并新增 `run_tui.sh` 启动器；缺少 `textual` 时启动器给出 `uv sync --extra tui` 的明确提示而不是抛 traceback。
- `apps/tui/client.py`：REST/WS 传输层。失败按可重试语义分类为 `ConnectionFailed` / `AuthError` / `ForbiddenError`，而不是把状态码塞进一条字符串；交互请求沿用前端 15s 上限，重投影（`/diagnostics`、`/steps`）使用独立的 90s 预算。`access_token` 仅驻内存，不落盘、不渲染。
- `apps/tui/screens/login.py`：凭据仅在一次请求内存在，失败信息不回显密码；`apps/tui/screens/home.py` 为登录后占位屏，M1 将由实时项目表替换。
- 测试：`tests/test_tui_client.py`（`httpx.MockTransport` 假后端）与 `tests/test_tui_app.py`（Textual `run_test` 无头 Pilot），共 22 例通过；未新增 dev 依赖（用 `asyncio.run` 而非 `pytest-asyncio`）。
- 已对真实后端 `127.0.0.1:8000` 做契约冒烟：`GET /` 返回 API banner，无效凭据正确映射为 `AuthError`；登录屏经无头渲染确认（标题、后端地址、用户名/密码、登录/退出）。
- 合并提示：本条目与 `fix/audit-lease-deadlock-and-realtime-scope` 上未提交的 Tier-0 瘦身条目都新增在 CHANGELOG 顶部，合并时此处会出现一处易解的冲突。

## 2026-09-22 — 修复 /logs 在事件循环上全量读取日志

- 症状：`GET /api/projects/{n}/logs` 是普通 `async def`，**未**走 `run_in_threadpool`（2026-09-18 的修复只覆盖了 `/steps`、`/diagnostics`、`/contest-dashboard`），并且先 `read_text()` **整个文件**再切末尾 N 行。本机真实日志最大 **8.2 MB**，因此 `?lines=200` 的每次请求都会让后端在事件循环上同步读取、并丢掉其中几乎全部——与 2026-09-18 记录的问题同类。TUI 的日志面板（M3）会轮询该端点，故影响被放大。
- 修复 `web/backend/project_api.py`：把目录扫描、`stat` 与读取整体移出路由，交给 `run_in_threadpool(recent_logs_payload, project, lines)`；选取规则（最新 mtime 优先、跳过空文件、`runner.log` 参与）逐条保持不变，并对读取途中日志被轮转（`stat` 抛 `OSError`）增加容错。
- 新增 `web/backend/log_tail.py`：`read_tail_lines()` 从文件末尾按块反向读取，并把收集到的字节**一次性解码**——逐块解码会把跨块边界的多字节字符切坏。`max_bytes`（默认 1 MiB）是硬上限，逐块按剩余额度收紧；窗口起点若落在文件中间，则丢弃可能残缺的首行。
- 实测（本机 `complete/cumcm2024b_no_judge_rep1/logs/step_5_codex_step5_full_solve_20260605_023516.log`，8.20 MB）：结果与旧的「全量读取再切片」**完全一致**，耗时 80.2 ms → **0.541 ms（148×）**，且不再占用事件循环。
- 测试：新增 `tests/test_log_tail.py`（16 例，含用字节计数桩**证明读取确实有界**、以 7 字节块强制跨多字节字符边界、`max_bytes` 上限行为、缺失/空文件、传入目录等）；`tests/test_web_control_plane_api.py` 新增 4 例，其中一例以**线程身份**证明该 handler 已不在事件循环线程上执行——去掉线程池跳转该测试即失败（已做 falsification 验证，确认测试非空转）。相关集合 64 例通过。


## 2026-09-22 — 隔离测试对生产认证库的写入（修复 admin 密码被测试覆盖的事故）

- 事故：2026-09-20T12:13:33Z，`web/auth.db` 中 `admin` 的 `password_hash` 被改写为**仓库里已提交的测试字面量**（`tests/test_showcase_acl.py`、`tests/test_secret_ops_api.py`、`tests/test_control_plane_auth.py` 三处同值），而登录入口在 `0.0.0.0:443` 公网可达——等同于把 admin 凭据公开。审计依据：`audit_log` 第 45 行 `user.bootstrap_admin_sync {"password_updated": true}`，且 `web/auth.db` mtime `12:13:33.453` 与之毫秒级吻合；服务进程自 09-18 13:22 起未重启，故写入者不是服务本身。
- 根因：`web/backend/main.py` 在 **import 时** 调用 `AuthStore.bootstrap_admin`，而 `AuthStore` 在 `AUTH_DB_FILE` 未设置时回落到 `<factory_root>/web/auth.db`。于是任何 import 后端 app 并自带 `ADMIN_PASSWORD` 的测试，都会把该密码写进正在运行的控制台所用的库。`tests/test_web_step8_5_metadata.py` 正是如此：它设置 `ADMIN_PASSWORD`，却不设 `AUTH_DB_FILE`/`FACTORY_ROOT`。
- 已在未修复的检出上复现：仅运行该测试文件即创建 `<repo>/web/auth.db`，其 admin 哈希等于该测试的字面量（单用户 `admin`）。该复现产物保存在 `/tmp/repro-web-auth.db` 备查。
- 修复 `tests/conftest.py`：在 **collection 之前**（conftest 模块导入期）把 `AUTH_DB_FILE` 重定向到仓库外的临时路径，因此 collection 期与用例期的 app import 都无法触达仓库库；使用 `setdefault`，自行设置该变量的测试行为完全不变（已确认无测试依赖其缺省）。
- 新增 session 级 autouse 护栏：若测试期间 `<repo>/web/auth.db` 字节发生变化，整个会话以错误失败。该库为 `journal_mode=delete`（无 WAL 边车文件），故字节比对是可靠信号；提示信息同时说明"服务在其间被重启"也会改写该文件，避免误判。
- 纵深防御：在肇事测试内显式设置 `AUTH_DB_FILE`。
- 新增 `tests/test_auth_db_isolation.py` 固定该契约：断言 `AUTH_DB_FILE` 指向仓库之外，且 import 后端 app 前后仓库库字节不变。
- 验证：修复后相关集合 94 例通过（auth/web/hygiene/joint-modeling/secret/deploy），且仓库库始终未被创建；护栏经临时探针验证会以会话错误形式失败。
- 运维记录：生产 admin 密码已通过重启 `paper-factory-api.service` 从 GCP Secret Manager 重新同步（审计 `user.bootstrap_admin_sync` id=46，`password_updated: true`），并确认此前泄露的测试字面量已无法登录。注意 `bootstrap_admin` 使 Secret Manager 成为权威来源，因此直接改 `web/auth.db` 或本地 `.env` 都会在下次启动时被覆盖。


## 2026-09-22 — Tier 0 仓库瘦身：清除退役资产与死引用

- 删除 `prompts/` 中 34 个全仓零引用的社会科学时代模板（step1a–1e、step2_findings_*、step3_decider、step4_architect/auditor/decider/executor/ext1–7、step5_argument_research、step5_data_audit、step6_methods_audit、step7_paper_writer、step8_code_review、step9_review、step10_revision、step11_final_review、step12_citation_audit、step13_gate2_judge、step13_table_formatting、step15_derobotification）。保留 catalog/specialized 引用的 17 个现役模板与 `judges/` 三件套；`step13_gate2_judge.txt` 仅被历史文档引用，一并移除，历史版本经 Git 查询。
- 删除 `scripts/` 中 12 个零引用脚本：ablation_monitor.sh、collect_verification_data.sh、delivery_override.py、quick_setup.sh、step13_judge_cache.py、test_p0_p1_fixes.sh、test_rerun0706_regression.sh、verify_numbers_legacy.py、verify_p0_lightweight.sh、verify_p0_lightweight_v2.sh、verify_p1_lightweight.sh、generate_code_appendix.py。核查同时确认 step8_5_gate.py、workflow_state.py、delivery_contract.py、evidence_payload_policy.py、publish_release.py、validate_cloudbuild.py 仍被 factory_core 或测试 import，均保留。
- `submission_fingerprint.py` 哈希清单锁定的 judge/governance 脚本（packet_context、packet_evidence、solver_evidence_selection、run_codex_tui_judge、decision_receipt_repair、shadow_cutover、capability_harness 等）与数据抓取器（fetch_benchmark.sh、dxs_*.py）全部保留。
- 移除 `demo_site/`（6 个 tracked 文件的静态演示页，零活引用）与 `xhxmt.github.io` 裸 gitlink（站点已按 2026-07-01 shutdown 计划关闭；物理检出保留在磁盘并加入 `.gitignore`，其自带 `.git` 历史完整）。
- 清理游离的 `legacy/shell/__pycache__` 遗留物。`external/`、`work/`、`archive/`、`benchmark/` 等本地 gitignored 数据保持原位。

## 2026-09-18 — 修复项目工作台请求超时（审计重算堵塞事件循环）

- 症状：打开项目后大面积出现「请求超时，请稍后重试」，任务图面板长期停在「正在加载任务图」。
- 根因：工作台每 8 秒轮询 `/steps`、`/diagnostics`、`/contest-dashboard`，而这些 `async def` handler 直接在事件循环上做同步重活（单项目 `submission_fingerprint` 12s、`build_project_diagnostics` 11s、`build_contest_dashboard` 6.5s）。实测一个 `/diagnostics` 会把 `/problem-plan`（独立耗时 5.6ms）拖到 24.3s，前端 15s 上限因此全线超时。
- `/steps` 与 `/diagnostics` 改为 `include_fingerprint=False`：两者只消费 `current_step` 与 `is_running`/`consultation_*`，不依赖证据指纹。`/steps` 由 12.1s 降到 0.08s。
- `/status`、`/diagnostics`、`/steps`、`/contest-dashboard` 的阻塞工作在 `run_in_threadpool` 中执行，单个慢项目不再冻结事件循环（并发重压下 `/problem-plan` 由 24.3s 降到 1.7s）。
- 修复 `factory_core/current_artifact_ownership.artifact_ownership` 的匹配病理：此前每个路径对约 88 条规则逐条重做路径规范化与 globstar 变体展开（单次请求 46 万次匹配）。现在路径只规范化一次、变体按 pattern 记忆化并对整次查找加 LRU 缓存；与冻结 v1 匹配器保持完全等价（新增 `tests/test_current_artifact_ownership_equivalence.py` 绑定该契约），全量扫描 1.95s → 0.22s。
- 前端：`/status`、`/diagnostics`、`/contest-dashboard` 使用独立的 60s 审计超时，避免大项目的合法长计算被 15s 交互上限误判为超时。

## 2026-09-18 — 修复控制台卡在加载兜底层（过期构建缓存）

- 修复「登录后整页一直停在全屏 spinner」：`CommandPalette` 原先以 `v-if="isAuthenticated"` 常驻渲染，等于每次启动都预载该 chunk；当浏览器仍在运行旧构建、对应 chunk 已被新部署删除（404）时，`defineAsyncComponent` 会永久停留在 loading 兜底层并盖住整个控制台。现在只在打开面板时挂载，启动后改为后台静默预热。
- 所有懒加载组件补上 `errorComponent`（新增 `AsyncLoadError`）：chunk 加载失败显示「界面资源加载失败 + 重新加载」，不再永久转圈。
- 新增 `web/frontend/src/lib/chunkRecovery.js`：监听 `vite:preloadError`，对同一入口 bundle 只做一次受保护整页刷新；`index.html` 内联脚本覆盖入口 chunk 404 的最坏情况（整页空白）。
- 生产缓存契约：nginx 对 `/`（HTML 文档）返回 `Cache-Control: no-cache`，带 hash 的 `/assets/*` 仍是 `public, immutable`。
- `web/deploy.sh` 不再 `rm -rf "$WEB_ROOT"/*`：改为覆盖同步并只回收超过 `STALE_ASSET_DAYS`（默认 14 天）的历史资源，避免旧 `index.html` 引用已删除的 chunk；部署验收新增首页 `Cache-Control` 检查。
- 更新 `web/docs/deployment/DEPLOYMENT.md` 的前端缓存契约与「控制台卡在全屏转圈」处置步骤。

## 2026-09-16 — Native mainline source split

- Moved Authority/Phase 3–9, shadow Web surfaces and Legacy runners/tests/docs to a complete tracked-source baseline at `~/paper_new`; recorded hashes and relocation inventory.
- Main entry points now use FactoryEngine + SQLite + Native Stage only. Older Native projects require explicit Stage activation; Legacy rollback/import and experimental DB dispatch are rejected.
- Kept native audit, solver evidence, final approval and atomic delivery checks, with independent native DB/commit boundaries. Removed experimental frontend flags, API routers and CI bootstrap dependencies.
- Runtime data, credentials, other worktrees and production processes are unchanged. See `docs/architecture/NATIVE_MAINLINE.md`.


## 2026-09-15: Authority solver-policy application route

- Route solver configuration and queries through fenced Authority command
  bundles for explicitly enabled CANARY/PRIMARY projects; retain native behavior
  for unmigrated and V1_ONLY projects.
- Add A2_0021 database guards against native table writes while Authority owns
  the database. Preserve prior migration bytes and exact backup/restore paths.
- Keep model execution, other workflow commands and notification dispatch out
  of this configuration-only pilot; unsupported native writes fail explicitly.

## 2026-09-15: production alignment and audit boundary repairs

- Reject every unreviewed solver receipt identity even when another version of the same path is included.
- Include hash-verified complete role-local text assets in HTTP review prompts; reject unsupported opaque assets and unsafe role directory links.
- Require openpyxl at runtime and reject workbook verification when the dependency is unavailable.
- Display the next Step after a completed checkpoint when a native Stage has no active subtask; preserve explicit active Step identities.
- Exclude prohibited historical input blobs while retaining their binding/review evidence and version-specific exclusion receipts in the final input snapshot.
- Preserve validated text-asset locators and source coordinates in Phase 7 grounding normalization.
- Reject malformed numeric manifests and grounding/delivery receipt objects without confusing them with valid evidence.
- Run the native Step display regression from the frontend tests directory in CI and keep partial native cursor progress consistent.

- 2026-09-15: Removed superseded June organization/optimization reports and their old
  optimization changelog, historical Web
  deployment/upload/consultation reports, and obsolete interface, screenshot,
  and quick-start copies. Current Web usage and deployment guides remain the
  documentation entry points; Git retains the removed documents in history.
- Removed the one-off documentation archive script and the outdated archive
  upload deployment checklist, which inspected retired paths.
- Updated documentation references and ignored root `work/` scratch files and
  `.pytest_cache/`. After exact-target confirmation, removed the old runner
  backup and stopped tracking six generated paper artifacts under `complete/`;
  their local copies remain available for the showcase and calibration.

## 2026-09-13: integrate server fixes with the current GitHub baseline

- Merge the September 10–12 audit, delivery recovery, input-version and UI repairs while retaining the current architecture and document/numerical evidence contracts.
- Register new artifact rules in the current native ownership extension; preserve the frozen registry and keep receipt-proved final reports at their finalization owner during rebases.
- Share packet context and asset verification through an independent module so production entrypoints retain the Phase 7/8 import boundary. Audit fingerprints include the shared implementation.
- Bind complete role-local assets to native audit batches and both filesystem and byte grounding APIs, alongside existing rendered document evidence.
- Preserve native cursor display and current audit/delivery status controls. This source integration does not redeploy or restart the running server.
- Refresh operational source manifests, unchanged-symbol locations and their deterministically derived identity fixtures. The legacy classifier and workflow semantic hashes remain unchanged; golden replay is verified across five hash seeds.
- Declare openpyxl in the locked development dependencies so the workbook regression runs in the minimal CI environment.

## Earlier integration work
- CI uses relative runtime aliases that remain resolvable during sandbox setup;
  explicit source overrides retain precedence over shared test fixture caches.
- Formal P0 and audit execution preserve read-only candidate sources, trusted
  reporters and explicitly bound runtimes under `/tmp`, after mounting the
  private temporary directory. Evidence validation enforces the exact layout. Phase9
  predecessor tests now share one pristine candidate identity; CI provides
  checksum-pinned Bubblewrap 0.11.2 and the native Node/npm audit paths.
- Integration fixes inventory opt-in joint-modeling Gates without changing frozen
  M0.2/M0.3 contracts, preserve exact owner/Step bindings, and isolate joint API
  tests from process-global framework stubs. Core CI now pins the recorded Python
  runtime, installs native evidence/sandbox tools, and tests an identical-tree
  single-parent Git source fixture.
- The Web dashboard follows native Stage/subtask/Step coordinates, distinguishes
  running final audits from completion, and separates execution, evidence,
  scientific judgment and delivery permission. Human selection and consultation
  both appear in pending work; evidence-only changes refresh without a workflow
  revision bump. Local preview data is not part of the production frontend.
- Added manually selected, per-project Claude Fable + GPT Pro joint modeling,
  defaulting off. Claude is pinned to `claude-fable-5-1` without fallback; Pro
  consultation and Claude synthesis precede mandatory human model selection,
  with conditional pre-solve risk review. Web and CLI share versioned decisions.
  Existing projects are never enabled by deployment. Operation and limitations:
  `docs/operations/JOINT_MODELING.md`.
- Refreshed two persisted-owner source coordinates and their derived identity
  fixtures after the joint decision hooks. Solver owner symbol bytes and
  workflow semantics remain unchanged; exact identity checks remain enabled.
- Three-role judge groups accept different per-role page attachments while
  retaining exact manifest, asset, ordering and transport checks. Direct receipt
  commands bind imports to the selected checkout instead of another installed
  working tree. Live integration exposed both failures before aggregation.
- Native review now carries complete, source-bound XLSX cell maps and all PDF
  pages through bounded document packets, actual image transport, archived call
  inputs and exact-quote grounding. Native PDF grounding rerenders frozen sources;
  the path-free shadow API remains fail-closed for this derivation. API judge
  inputs no longer silently truncate at 200,000 bytes. Unsupported formats,
  image transports and exceeded limits remain explicit failures. No model,
  workflow gate, acceptance/release authority or deferred N03 behavior changes.
- The project-control compatibility helper forwards the required cancellation
  identity; bare PID calls fail closed. Its control-order test also verifies
  that the persisted identity is forwarded after the pause commits.
- Follow-up normal-run repairs correct compat/worker readiness argument routing,
  verify cancellation against the persisted worker identity and lease, and add
  bounded, lossless NumPy evidence capsules across selection, aliases, coverage
  and exact-quote grounding. Parquet direct review remains explicitly unsupported.
- Step13 documentation now names `judge-precheck-v2`, `audit_binding` and
  `input_fingerprint` without changing workflow gates. Actual user CLI diagnostics
  and compatibility --status remain a known deferred gap (N03); the earlier
  shared-status statement applies to service/Web/verified projection consumers.
- Refreshed the two persisted-owner source-span coordinates after storage changes
  and regenerated their derived identity goldens. Symbol bytes, owner semantics,
  and the frozen classifier remain unchanged; exact comparisons stay enabled.
- Normal-run audit repairs keep state/checkpoint/event writes atomic, bind API
  judges to final sent input, resolve packet aliases consistently, and derive
  required execution evidence from adopted solver jobs.
- Python closure v2 preserves initial versions of declared input/output overlaps.
  Native ownership v3 adds receipt-validated snapshots individually, preserving
  frozen ownership/classifier contracts. Required numeric claims and key results
  are verified per field and accepted source version.
- Native CLI/Web/file status shares a coherent snapshot, separates current
  workflow/evidence/scientific/score/delivery dimensions, and rejects partial
  compatibility projections. Worker startup waits for initialization, cancellation
  verifies observed exits, and monitor initialization failures are durable.
- The current normal-run boundary and test entry points are documented in
  `docs/operations/NORMAL_RUN_AUDIT_CONTRACT.md`. Real-provider/full-problem
  scientific acceptance and the unresolved manual technical-continuation route
  remain outside this normal-run engineering acceptance.
- Removed the extra repair-retry attempt at the user's request. Restored the
  original per-step attempt-budget check and removed its authorization API,
  CLI operation, implementation-version snapshots, and feature-specific tests.
  Existing attempt history, normal recovery, and the separate Step13 technical
  continuation remain intact. This supersedes the repair-retry entries below.
- Explicitly identify `gate2_delivery_override.json` as non-authoritative for
  Step14 technical continuation; only the native authorization event applies,
  and neither grants Gate2 PASS or acceptance/release/delivery permission.
- Bind repair input baselines to the exact failed cursor and implementation
  evidence to the same failed attempt; preserve future-owner declarations while
  reporting missing upstream inputs distinctly. Correct the native submission
  schema assertion and let the Phase7/8 CLI test harness await its existing
  120-second business deadline without changing production limits.
- Keep frozen M0.2/M0.3 ownership/classifier identities unchanged while adding
  the native v2 ownership and v10 dirty-classifier extension for root evidence.
  Native input manifests and receipts explicitly bind the current contracts;
  unknown artifacts remain fail closed and historical dirty evidence is kept.
- Addressed the 2026-09-06 rerun contracts: root evidence ownership; generated
  claim path/field validation; bounded version-bound repair retries; explicit
  event-bound Step13 technical continuation; actual Step/Stage judge identity;
  immutable judge batches and verified reuse; Python solver input closure;
  deduplicated packet coverage; explicit accepted numeric versions; current,
  evidence-aware status projections; and strict NumPy JSON producer conversion.
  Added a persistent local launcher and documented the bounded operator route
  in `docs/operations/RERUN_REPAIR_AND_TECHNICAL_CONTINUATION.md`.
- Corrected the Phase9 generation runbook to the existing `authority_operator.py run-generation` command and its separate dry-run/confirmed boundary.
- Synchronized the expanded Phase9 focused targets with the independent summary/package policy and added a direct repository-contract acceptance check; exact policy comparisons remain enforced.
- Closed full-audit findings: registered only the exact new Phase9 canonical/schema consumers, refreshed two storage symbol offsets without changing trusted symbol bytes, and bounded provider namespace argv. Unrelated sibling directories in project/runtime ancestors are omitted while complete project/source inputs and configuration proof remain intact.
- Bound redirected CODEX_HOME configuration paths to approved original presence/bytes and independently checked the actual native environment and immutable directory closure. Absent config remains absent. SQLite/cache state uses separate writable locations; only local installation metadata is writable at the home root.
- Froze approved configuration absence as well as present bytes using private read-only tmpfs namespaces, with independent native-root presence/mount/cwd checks. Native judge temporary replies now honor TMPDIR and use unique attempt directories.
- Closed remaining Phase9 runtime review gaps: sealed native/configuration execution views and a native exec-stop handshake replace wrapper-only identity; Codex uses a private writable runtime home. CLI export selects latest Authority attempts before reading accepted outputs, and generated controls use canonical JSON. Added the required runtime implementation/test/evidence mapping.
- Hardened Phase9 runtime identity and recovery: operator-approved native Codex/configuration hashes and exact argv now bind dispatch and kernel launch; post-launch transport errors remain UNCERTAIN without automatic resend. Native fallback output selection is persisted before export. Runtime request v3 keeps execution coordinates stable across freshly acquired completion entries; exact append-only export stages resume missing records/files and reject drift without provider redispatch. Existing request v2 semantics remain unchanged.

- Added the default-off A2_0020 Phase9 runtime coordinator: separate operator
  dispatch grants, committed command/invocation/attempt/scope identities before
  provider calls, observed OS launches and completions, bounded local lifecycle
  probes, real role/scope receipt recording and finalizer coordination.
  Acquisition freshness and execution deadlines are distinct; completion needs
  a fresh entry at the same stable Authority state. Uncertain work is never
  automatically resent, delivery stays disabled, and production migration
  still requires separate authorization.
- Fixed total-deadline propagation through isolated Step13 preparation, blocked
  record directories overlapping executing source, and preserved source modes
  and Git status alongside working bytes. Process cancellation now closes the
  owned process group even when its leader exits early or a callback fails.

- Added an explicit isolated Step13 component coordinator with distinct normal
  math-only and forensic three-role routes, pinned model/effort, bounded calls,
  immutable attempt logs, input/source rechecks and an honest component terminal.
  This is not the formal Authority runtime executor or finalizer.
- Judge packets can consume `.evidence-view.json` scientific JSON views. Every
  key and scalar is retained; explicitly declared numeric arrays carry their
  original pointer, length, hash and a visible not-reviewed limitation. The
  packet builder reconstructs each view from the complete original source and
  omits invalid or stale views, preserving required-claim blocking semantics.

- Phase9 follow-up repair: final analysis and approval fingerprints now read
  contest-core requirements and current approval receipts from one private
  main/WAL snapshot. They do not upgrade an existing workflow schema, switch
  its journal mode, or create/modify source WAL/SHM files. Supported older
  schemas without contest-core tables retain legacy analysis compatibility;
  incomplete current schemas fail without implicit repair.
- Judge packet preflight now rejects missing, malformed, empty, or ineligible
  role inputs before any role is dispatched, including direct prepared and
  math-precheck entry points. A packet-building command succeeding is not
  sufficient to authorize a model call.
- Native validation retains missing-artifact evidence but replaces a same-step
  or future-step reopen request with `PERMANENT_RECOVERY_TARGET`. An earlier
  valid owner boundary still reopens normally. Missing presentation artifacts
  whose registered owner has not run require explicit upstream repair.

- Phase9 independent-audit finding closure: committed run-generation and
  forensic-replay requests now recover their exact immutable result before
  live authorization/freshness gates, while revalidating the complete stored
  receipt, consumption, source, generation, terminal, event, and typed-evidence
  graph. Recovery and deterministic preflight run under one cross-process
  Authority commit lease against a private, stable main/WAL snapshot, so they
  neither consume a nonce nor create source-database WAL/SHM sidecars. A new
  operation repeats all gates and the exact lookup under `BEGIN IMMEDIATE`;
  idempotency keys are globally reserved across workflows even when their
  composite-key row is corrupt or missing, and reverse aliases, non-unique
  successors, a different request, or any damaged graph fail closed. Exact
  recovery returns the byte-for-byte same immutable result as the committing
  call; the retained `replayed` compatibility field is always `false` and
  cannot reveal which service path produced the result.

- Phase9 audit/delivery boundary correction: final audit is analysis-only by
  default, so non-Phase9 and Phase1-8/legacy analysis remain available without
  fabricating Phase9 coordinates. Step 16 explicitly enters the legacy
  acceptance boundary. Actual Phase9 acceptance, release, submission,
  delivery, completion, and archive operations are classified and rejected
  before their first side effect. Phase9 migration/schema residue can no
  longer be reclassified as legacy if generation controls are dropped or
  emptied, and commit-lease reentrancy is process-bound so a forked child must
  acquire its own OS lock. Tests no longer replace the production fence with
  an autouse allow-value monkeypatch.

- Phase9 composite evidence closure: the formal `full_repository` suite now
  executes all Python tests, a frontend production build, and the documented
  Chromium `npm run test:phase6` target in both source and fresh environments.
  Command records bind ordered per-stage commands, environments, exits, raw-log
  slices, complete Node/browser dependency inventories, and runtime versions;
  any missing, skipped, or failing stage makes the combined suite non-passing.
  The production-build inventory is also revalidated after the browser stage,
  and the package binds the exact frontend scripts and test-source bytes. The
  formal build pins Vite's `--configLoader runner`, bypassing the default
  bundled-config path that tries to materialize `.vite-temp` below the
  read-only dependency mount. Formal P0 raw-log validation also recognizes
  pytest's verified `H:MM:SS` duration suffix when a suite reaches 60 seconds,
  while binding the exact PASS summary and rounded duration to the fixed
  265-second suite deadline and the monotonic command record. The unchanged
  300-second runner-authorization TTL also budgets five seconds for process
  completion and thirty seconds total for pre-run checks, post-run validation,
  and authority attestation; evidence outside that complete authorization time
  chain fails closed. Mismatched, injected, duplicated, or fabricated summary
  fields remain fail closed.
  Production remains `BLOCKED`; no migration, provider, worker, outbox,
  acceptance, delivery, release, deployment, or cutover was authorized.

- Phase9 trusted-evidence hardening: implement the additive
  `A2_0017_PHASE9_AUDIT_HARDENING` (SHA-256
  `c886700e817098e04c325d414a0b0ed7f267a84ea60a05a0f0501e95f86fcc4d`)
  without changing A2_0010-A2_0016 statement bytes, then append
  `A2_0018_PHASE9_P0_RUNNER_ATTESTATION` (SHA-256
  `eca9538c259285853547bf451a8fbf56ca8580d3963ff9b2537f979e162d5121`)
  without changing A2_0010-A2_0017 statement bytes, followed by the additive
  `A2_0019_PHASE9_REPLAY_EVIDENCE_ATTESTATION` without changing
  A2_0010-A2_0018 statement bytes. The formal P0 contract is
  a typed domain produced by a fixed allowlisted local runner and bound to actual
  candidate source bytes, command, cwd/interpreter, sanitized environment,
  complete parseable raw log and strict evidence-root inventory; fixture,
  arbitrary PASS, missing-test, partial, replaced or cross-coordinate evidence
  cannot sign entry `READY`. Entry, run generation and replay use trusted UTC
  time, full tracked-source inventories and the same strict no-follow official-
  input tree validator.

- Phase9 atomic and semantic hardening: run mode/modeling contract/capability
  are fixed to `FORENSIC_REPLAY`/`LEGACY_NOT_APPLICABLE`/`DISABLED` in Python
  and SQLite; a run-generation authorization covers the complete canonical
  operation, is hash-recomputed and consumed once; rotation requires the exact
  current predecessor terminal and revision CAS. Replay now consumes a
  short-lived one-use entry authorization, reruns the shared live gate at start
  and precommit, and persists exact typed role/process/provider/outbox and all
  17 hyphenated acceptance-case receipts before terminal/pointer update. The
  read-only collector reconstructs typed event semantics and rejects a hash-
  consistent but semantically wrong SQL graph.

- Phase9 delivery and evidence closure: release, final acceptance and final
  submission independently verify current Authority generation/terminal before
  any side effect and always reject Phase9 forensic, technical, ablation,
  disabled, stale, cached or override decisions. Audit command records bind the
  executed byte inventory and sanitized environment; summary/builder policy
  requires complete raw logs for all seven source/fresh suite pairs and retains
  every failed attempt. A valid generated package must carry those immutable
  records and remains a candidate until a separate auditor recomputes them;
  repository prose is not an independent verdict. Production remains `BLOCKED`;
  A2_0016 through A2_0019 are not applied in production; formal Phase9-A/Run4 has
  not run; Phase 9 is incomplete; Phase10-B has not started.

- Phase9-A default-off control plane: append A2_0016 without changing the
  published A2_0010-A2_0015 statement bytes. Add a candidate/run/entry/evidence
  bound forensic finalizer with one-transaction replay/event/terminal receipt,
  exact idempotent replay, conflict detection, full rollback, guarded current
  pointer rotation, evidence and Git rechecks before commit, and a query-only
  state collector. It validates the Step-13 packet claim fence, three new role
  generations or typed no-judge ablation, layered/effective verdicts,
  revision-atomic snapshots, outbox/process safety, all minimum acceptance
  cases, and the delivery-disabled terminal. The explicit CLI and environment
  configuration are disabled by default and cannot enable provider/network,
  outbox, delivery, release, deployment, migration, or cutover. Local regression
  tests exercise the contract and fault boundaries; any freeze claim also
  requires immutable source/fresh candidate evidence and independent audit.
  Production remains
  honestly `BLOCKED` without real Authority/input/context/authorization/evidence
  and Pro review.

- Phase1–8 durable continuous-chain closure: add the packaged
  `Phase6TrustedSourceAssembler`, an Authority revision-atomic compound reader,
  and immutable read-only Phase4/5 current/head readers. The trusted receipt
  binds the exact workflow/project/revision command and predecessor, complete
  Phase3 graph/current occurrence, all project/run/runtime/scheduler
  generations, contract pins, Phase4 current/predecessor, Phase5 current, and
  the Phase6 predecessor. Phase7/8 now rejects receiptless direct-test bindings
  and reopens Phase4/5 on every current-proof use. A real durable E2E covers
  restart/replay/conflict, missing/stale/mismatched/partial/cancelled/
  superseded heads, semantic A→B→A occurrences, wrong Phase6/7 heads and
  default-off resource silence; the former direct SQL and placeholder-hash E2E
  setup is replaced by the reviewed generation and durable producer paths.

- Phase9 candidate-bound entry foundation: append A2_0015 without changing
  A2_0010-A2_0014 bytes, and add a narrow atomic create/rotate service for
  run generations. It derives generation identity from a typed canonical
  request and durably binds exact predecessor receipt, source commit/tree/
  parent, current project/workflow/revision and project/run/runtime/scheduler
  generations, source-authorized contract pins, official-input byte hashes,
  execution context, and operator authorization. Same-key replay is exact,
  different bytes conflict, injected failures roll back every row and pointer,
  official inputs and the canonical execution-context receipt are read and
  reverified from explicit paths before commit, initial project generation is
  content-derived, and controlled-account authorization is checked against the
  executing UID/account rather than trusted as a label. Immutable succession,
  receipt, current-pointer, and workflow-coordinate triggers close direct
  pointer-update gaps,
  and create/rotate requires `V1_ONLY`, disabled writer/consumer, and disabled
  delivery. The operator CLI is dry-run unless explicitly confirmed. This
  capability does not start Phase9-A or authorize provider, network, outbox,
  delivery, release, deployment, migration, or cutover activity.
  Add the query-only candidate-bound collector and formal entry verifier for
  the exact generation/receipt/source/input/context/authorization coordinates,
  delivery and quiescence fences, old-generation read-only state, and all nine
  P0 receipts including AR-007. Missing controlled runtime inputs returns a
  candidate-bound `BLOCKED`; a test-fixture creation receipt is never treated
  as a formal entry receipt.

- AR-007 delivery-bypass closure: make `ABLATE_NO_JUDGE` an auditable,
  typed `PERMANENT_ABLATION_NO_DELIVERY` terminal with a nonzero result and
  `delivery_allowed=false`, including when technical-flow validation or an
  exact-snapshot delivery override is present. No-judge final audit now skips
  Judge execution, cached acceptance, and override consumption; it writes only
  its ablation marker and audit result, never `final_submission.sha256` or a
  final-acceptance receipt. Workflow-state and atomic-release readers reject
  both new and historical ablation records, so an override cannot make one
  current, reusable, Step-16-ready, or publishable.

- Phase9-PREP safety boundary: add a stdlib-only manifest validator and
  operator runbook for preparation that may proceed while the Phase 7+8 Pro
  review is pending. The validator reads one bounded strict JSON manifest and
  a fixed allowlist of no-lock/no-external-diff Git facts with system/global
  configuration disabled and hazardous repository-local features overridden;
  it rejects every dirty-worktree class, out-of-scope committed path and
  unsafe/overlapping runtime path, then emits a deterministic
  `PREP_MANIFEST_VALID` report. All workflow/runtime SQLite, generation,
  process, network, provider, outbox, migration, delivery, release and cutover
  capabilities remain false; runtime-state, pin, replay, three-role and
  clean-room checks remain `DEFERRED`. Add a non-receipt forensic evidence
  skeleton and the minimum Phase9-A acceptance matrix. This change does not
  create a run generation or authorize formal Phase 9, and it names the later
  fresh clean-room run Phase10-B as an explicit alias for architecture Phase 10.

- Phase 7+8 Pro second-round Minor repair: distinguish immutable reference-
  binding history from qualified effective current state. Historical binding
  loaders and status fallback now always report `current=false`; the current
  loader and record/replay/takeover paths promote to `current=true` only after
  exact publication identity, winning generation, Phase 7 head, activation,
  and post-commit fences succeed. Add A-to-B activation, history-only/crash,
  activation timeout/cancellation/supersession/head-drift, exact-key takeover,
  status conflict, and successful replay/current-loader regressions. This does
  not relax prior M1/M2/v1 failure-closed behavior or expand default-off,
  shadow-only, no-provider, no-outbox, no-dispatch, no-release, or no-cutover
  scope.

- Phase 7+8 Pro Major repair candidate: preserve typed deadline and
  cancellation propagation while reconstructing status at all four Phase 7/8
  history/current boundaries, so CLI/service return stable nonzero codes and
  reasons and Web maps timeout to 504 and cancellation to 409 instead of
  returning a synthetic `DENIED/CURRENT_HEAD_UNAVAILABLE` success. Upgrade the
  Phase 8 store to a versioned two-stage immutable-history/current-activation
  contract. Every current projection now carries a hashed publication identity
  bound to its exact work, operator, or revocation generation and Phase 7 head,
  and every production current read requalifies that identity. Activation
  compensation remains cleanup only and is not a safety boundary. Terminal
  revocation additionally requires an append-only publication receipt, so a
  crash or losing generation between activation and receipt also fails closed.
  Post-commit deadline crossing reports typed `PHASE78_OUTCOME_UNCERTAIN` with
  the original idempotency key; durable history supports exact same-key replay
  and rejects different bytes. Add commit-crossing, skipped-reconciler,
  concurrent-window, activation cancellation/head-drift, post-activation
  crash, winning-generation takeover, CLI and Web regressions, and update the
  versioned exact Phase 7+8 machine test contract. Default-off, shadow-only,
  no-provider, no-outbox, no-release, and the Phase 4-6 exact 657 gate are
  unchanged. Phase 8 v1 databases are not silently upgraded: reopen returns
  stable `PHASE8_SCHEMA_INCOMPATIBLE` before any v2-column read and leaves the
  old file byte-identical; operators must preserve it as audit history and use
  a freshly provisioned private v2 path. This entry records a local review
  candidate, not deployment or release authorization.

- Phase 7+8 durable full-shadow candidate: add a strictly default-off,
  synchronous local sidecar that binds the complete current Phase-3 Authority
  aggregate, one exact revision-level artifact occurrence, the Phase-6 current
  access proof, three exact-byte grounding roles, and the Phase-8 PDF/CAS/
  package/approval/decision chain. The private work ledger durably records
  request, claim, lease, checkpoint, result, cancellation and uncertain replay;
  one total deadline spans Authority and SQLite waits, file/PDF/CAS work, both
  phase stores and the one allowed replay. Work-generation and upstream-head
  fences prevent a cancelled, superseded or late local worker from publishing
  current state. P7 replay retains whitespace/newlines and works after the
  original packet root moves; P8 replay uses persisted exact PDF-derived CAS
  facts after the original path disappears. Missing, corrupt, encrypted or
  textless PDFs produce stable path-free unavailable results.
  Add an operator-only `factory_core.phase78_operator` preflight that validates
  and persists the exact P7/P8 facts before an ordinary request may reference
  its receipt hash. `PHASE78_TRUSTED_OPERATOR_ID` and `GENERATION` are only
  deployment labels; the trust boundary is the controlled OS account plus
  private `0700` parents and `0600` files. The operator issuer is distinct from
  the Phase-6 subject, and CLI/Web/service callers cannot mint a preflight or
  promote `snapshot:view` into egress authority. Historical PASS/AUTHORIZED
  receipts remain replayable while current reads rejoin live Phase-3/6/7 heads,
  lifecycle, policy and service-owned time, so revoke, expiry, supersession or
  drift yields `DENIED`. Legacy `legacy_unknown` generations must complete the
  regular migration before eligibility.
  Add lazy ordinary CLI, ACL-first Web API, service, explicit local Scheduler/
  worker and durable status/revoke paths. With `PHASE78_ENABLED=false`, they
  return before request paths, databases, CAS, heavy imports, threads or
  processes; with it enabled, every result is non-authoritative, no-transfer,
  no-provider, no-outbox and no-dispatch. There is no Phase-7+8 frontend or
  production cutover in this candidate. A new independent
  `bootstrap_phase78.sh` owns exact unit/runtime/adapters/PDF-CAS/E2E counts via
  its versioned structured contract and leaves the Phase-3–6 exact 657 gate
  unchanged. This entry records implementation, not deployment authorization
  or a final release PASS.

- Phase 6 verified snapshot/scoped-grant full-shadow candidate: add a
  caller-path, independent exact-profile SQLite store for canonical source
  bindings, immutable verified snapshot chains, current-head CAS, closed
  snapshot/section/action-center grants, revocation/expiry lifecycle,
  evaluation/access-proof receipts and exact idempotency replay. Source
  bindings require matching Authority/source coordinates and explicit Phase
  3/4/5 state hashes; all domain times are caller-supplied and every result
  remains non-authoritative/no-transfer/no-dispatch. Add an authenticated
  project-ACL-first read endpoint and strict seven-state frontend projection
  with one stale retry, request-generation fencing and accessible keyboard/
  focus behavior. Backend `PHASE6_SNAPSHOT_ENABLED` and the exact build-time
  string `VITE_PHASE6_FULL_SHADOW_ENABLED=true` are independently default-off;
  the disabled frontend excludes the Phase 6 component and the disabled
  backend does not import the core or touch its store. The current candidate
  implementation has passed the current worktree's complete local
  audit and remains pending independent Pro review; it does not add an
  Authority reader/writer, mutate v1 state, execute an action, dispatch or
  claim deployment. The historical M0.1 pure-module import gate now retains an
  exact path-and-primitive allowlist for the 13 formal Phase 2-6 consumers,
  while every other production module remains prohibited.
  Pro Round 02-04 hardening makes malformed/undecodable Python and scanner
  faults reject evidence payloads with a conservative raw-byte fallback;
  normalizes SQLite database/WAL/SHM/journal denial across case and Windows
  separators; and preserves project-ACL-first behavior even when an
  authenticated caller supplies an invalid `expected_revision`. Duplicate
  trimmed action IDs now fail closed at both Web boundaries, unknown errors use
  one allowlisted public message, and current/obsolete abort generations settle
  without stale UI overwrite. Add real production-build/module/resource-load,
  mounted browser keyboard/ARIA, CLI/Web/service/Scheduler/worker Phase 7/8
  isolation, and five historical F1 counterexample closure regressions. These
  changes do not enable either Phase 6 flag or expand Phase 7/8 behavior.
  Normal-flow follow-up adds one configurable 1–300000ms frontend request
  deadline (15000ms default) whose single budget spans response headers, body
  parsing and the one permitted stale retry; an AbortController plus deadline
  race settles transports that ignore cancellation without allowing an old
  generation to overwrite current UI. Bootstrap now requires the versioned
  exact five-group `100/274/146/29/108 = 657` contract from both JUnit XML and
  an independent outcome ledger, with zero failed/skipped/xfailed/xpassed.
  The actual freeze-inventory and deterministic ZIP tools are now candidate
  source and invoke the central payload path policy before candidate file I/O;
  a live SQLite WAL pipeline regression proves database and sidecar state stay
  out of both inventory and archive.

- Phase 4-5 FIX2 candidate hardening: make the Phase 5 owned SQLite profile an
  exact inventory of every persistent table, index, view and trigger, including
  the deterministic SQLite automatic indexes, so any additional ordinary,
  unique, partial or expression index or view fails closed. Bind the Phase 5
  companion marker to the anchored parent-directory device and inode and check
  that identity before read-write open, transaction entry and commit. Phase 4
  and Phase 5 now recover from failed exclusive initialization by atomically
  moving a still-named candidate to a private quarantine, verifying it against
  the creation descriptor, and deleting only that inode; a raced foreign
  replacement is restored without overwrite. Uncommitted connect, schema and
  commit failures clean up and fsync their anchored parent, while an actually
  committed store is retained and restart-verifiable. This is a review
  candidate, not an independent PASS; production authority, dispatch, frozen
  Phase 3/FIX5 contracts and excluded Phase 6B-8B drafts remain unchanged.

- Phase 4-5 re-audit repair: move standalone shadow-store ownership proof ahead
  of every ordinary SQLite read-write open and transaction. Existing databases
  are anchored read-only, reject WAL/SHM/journal sidecars, and must match an
  exact ownership marker plus complete schema profile before the same inode can
  be opened read-write; new stores use exclusive creation and path/inode-bound
  markers. Commit and initialization error paths retain the anchor and fail
  closed on replacement without leaking descriptors. Replace Phase 5 stage
  suffix concatenation with bounded, domain-separated SHA-256 keys bound to the
  caller key, request, stage and schema. Linux inotify/writer-lock and
  495-512-character crash/restart/replay tests cover both reported defects.
  Runtime authority, dispatch, production imports, frozen migrations, Phase 3
  FIX5 and the excluded Phase 6B/7B/8B drafts remain unchanged.

- Phase 4-5 durable full-shadow closure: add a caller-path-explicit,
  standalone SQLite Phase 4 operation runtime with atomic immutable launch
  intent, CAS current state, append-only receipts and idempotency results;
  lease claim/reclaim, retry accounting, uncertain-result reconciliation,
  exact restart replay and transaction failure injection remain synthetic and
  never dispatch. Add a separately isolated Phase 5 supervisor lifecycle that
  binds workflow/invocation/attempt/process-scope/operation identities to the
  existing pure pause policy, persists scoped requests and receipts, and puts
  any observation behind an injected shadow-only port. Both runtimes return
  before opening SQLite or calling a port when disabled, remain outside all
  production import graphs, claim no authority, and do not change
  `A2_0001`-`A2_0014`, the sole production writer, the five-method reader, the
  three Phase 3 ledgers, V1 identity, or the frozen Phase 3 FIX5 archive.

- Phase 3 FIX5 audit closure: fail a complete dirty/blocked mutation that lacks
  its reopen-ledger control record with the existing typed Phase-3 contract
  exception before any database connection or SQL, identically under normal
  and optimized Python. Remove two inert class-body expressions from the sole
  production-capable writer without changing its public surface. The runnable
  audit archive now writes default bootstrap logs and environment facts outside
  the immutable package root, so its full checksum manifest passes before and
  after a no-override bootstrap from a fresh extraction. Runtime authority,
  schema/migrations, dispatch, routes, and Phase 4-8 behavior are unchanged.

- Phase 3 operator-authorization authority binding: replace the self-hashed
  owner-resolution authorization with a v2 typed claim that binds workflow,
  source revision, target command, path, reconstructable owner compilation and
  policy SHA, owner ID/Stage, dirty flag, operator, and reason. Claims are
  trusted only when included in an immutable control receipt emitted by a real
  `V1_ONLY` writer-configuration epoch CAS. The production writer verifies the
  receipt, grant membership, complete command coordinate, and independently
  rejects authorization for normally resolvable paths in the same transaction;
  exact replay and all Phase 3 reader reconstruction repeat these checks.
  Cross-workflow/command, stale revision, altered policy/path/owner/dirty/issuer
  or receipt, SQL tamper, and failure-injection paths fail closed without
  command or ledger residue. No migration/table or active route changes.

- Phase 3 full-shadow re-audit hardening: tighten checkpoint persistence so an
  initial checkpoint root is valid only when no prior Phase-3 head exists, and
  every later checkpoint must bind both semantic and occurrence predecessors
  plus a legal state edge. Persist blocked analysis atomically in the existing
  reopen ledger as a typed `BLOCKED_NO_REOPEN` disposition. Filesystem blockers
  now carry a frozen owner registration; owner-resolution failures require typed
  operator authorization, and blocker/tombstone removals derive owner identity
  only from prior typed bindings. Complete Phase-3 mutations now carry a typed
  bootstrap/continuation previous-head that binds the real persisted head
  revision, command, mutation, and manifest; the reader revalidates adjacent
  continuity while reconstructing revisions and latest artifact state. No
  `A2_0001`-`A2_0014` migration bytes, checksums, or statement ordering change;
  V1 remains the sole active production authority, and Phase 3 stays
  default-disabled, non-authoritative, and non-dispatching.
  A follow-up replaces the last missing-control assertion with a typed
  fail-closed contract error that is identical under `python -O`, before any
  database connection, and moves audit-bootstrap runtime output outside the
  immutable package checksum closure.

- Phase 2 Production Authority Foundation: append the separately verified
  A2_0010-A2_0014 production suffix without changing the published
  A2_0001-A2_0009 bytes/checksums. Add explicit-path, dry-run-first operator
  backup/migration/restore and switch administration; a durable writer/epoch
  CAS facade with atomic revision/bundle/outbox commits; a mode-ro,
  revision-atomic read repository; callback-injected transactional outbox
  claim/reconciliation/retry/dead-letter state; and explicit-policy
  health/one-way fallback evidence. A2_0014 immutably binds the persistent
  database identity and original pre-Authority backup lineage. Migrate and
  restore now reserve their explicit evidence path as a durable operation
  journal, reuse the original immutable backup across interruption, and can
  close final evidence after an already-completed READY migration or verified
  restore replacement. A brand-new migrate operation now fails before journal
  or backup publication when base Authority or the production suffix has
  already started, requiring exact replay of the original operation paths.
  Revision snapshots reject `after_revision` greater than
  the resolved through/current boundary. All production-capable paths remain
  default `V1_ONLY`, have no active Scheduler/Web/Service/CLI/launcher caller,
  perform no real provider dispatch, and do not change v1 as the sole active
  production route.

- Phase 5 simplified pause-policy shadow: add a pure two-mode/four-scope
  decision matrix with immutable wire receipts. Worker, model, and attached
  Solver scopes map to `terminate-scope`; durable Solvers continue for ordinary
  pause and map to `request-cancel` only for explicit cancel-Solvers mode. The
  module remains outside production imports and performs no process signal,
  launch, termination, persistence, outbox/invocation write, Solver/provider
  call, Scheduler/Web/API integration, feature-flag cutover, or deployment.

- Phase 3 full shadow foundation: package immutable, deterministic Artifact
  Record/Manifest, explicit ArtifactRemoval, typed ChangeSet/DirtyDecision,
  read-set-CAS ReopenPlan, checkpoint ledger/re-attestation dry-run, and parity
  receipt contracts behind an explicit default-disabled, non-authoritative
  runner. Manifests hash-bind their complete normalized tracked-path inventory;
  prior records and unreadable blockers cannot disappear without a current
  record/blocker or typed removal. The complete mutation binds both manifests,
  the recomputed ChangeSet, exact current values/removals, checkpoint input, and
  ReopenPlan/read set, rejecting partial, tampered, or cross-round graphs.
  Semantic artifact/checkpoint identity is separated from revision-bound ledger
  occurrence identity, allowing A-to-B-to-A and equal states across workflows
  without PK collision. Typed tombstone occurrences close pure and mixed
  removals, later recreation, presence/absence CAS, replay, and read-side latest
  state. Owner-policy change fails closed as `MIGRATION_REQUIRED`; unsafe paths,
  symlink escape, unreadable files, and changed-during-read files produce typed
  blockers; illegal non-string paths use one deterministic sentinel. The only
  production-capable write surface remains
  `AuthorityProductionWriter.persist_command_bundle`: no mutation preserves the
  exact v1 request/bundle/replay identity, while an explicit complete typed
  `phase3_mutation` uses hash-bound v2 identity and writes the existing
  artifact/checkpoint/reopen ledgers in the same fenced `BEGIN IMMEDIATE`.
  Extend the `mode=ro`/`query_only` read repository with strict typed mutation
  reconstruction plus the supported `phase3_artifact_state()` read for
  revision-bound present-record/blocker/tombstone reconstruction. The public
  read surface remains exactly five supported query methods:
  `workflow_coordinate()`, `command_bundle()`, `phase3_artifact_state()`,
  `revision_snapshot()`, and `outbox_delivery_state()`. No
  `A2_0001`-`A2_0014` migration bytes change. Checkpoint keys use an explicit
  `phase3:` namespace; packaging now makes the already-included `scripts`
  support modules importable from a built wheel. No active
  Scheduler/Service/CLI/Web route imports the runner, no real dispatch occurs,
  and V1 remains the sole production authority and active route with no cutover.

- Phase 2 additive authority persistence: add an independently versioned
  authority-schema-v2 migration state machine for legacy schema v1-v9,
  default-off Command/Event/Receipt envelope storage, revision allocation,
  scoped idempotency, contract pins, artifact/checkpoint/reopen records,
  invocation/attempt/process scopes, project snapshots, and a
  transaction-bound append-only outbox. The migration and repository recheck a
  canonical legacy-source fingerprint, exact ordered migration identities, and
  actual SQLite object definitions; append-only identities reject
  `INSERT OR REPLACE` conflicts as well as updates and deletes. The repository
  exposes no partial mutation/allocator transaction and freezes idempotency as
  command request bytes plus exact recorded companion-envelope identities.
  Legacy current checkpoints retain only `LEGACY_IMPORTED`/`UNKNOWN` assurance;
  project-state stage fields do not invent an owner, and ambiguous owners stop
  at `MIGRATION_BLOCKED_OWNER_AMBIGUOUS`. Existing writers, legacy tables,
  production DBs, API/UI, providers, Scheduler, outbox consumers, and delivery
  paths remain untouched.

- Run4 M0.3 attempt-9: add an explicit source-compiled WorkflowEvent row
  policy with a closed raw-type/payload-family vocabulary and exact
  catalog/payload/subject/result Step plus zero/subject/result attempt
  bindings. Unknown raw or dynamic event families and coherent Step/attempt
  drift now fail before EVENT_HEAD publication; project-relative immutable
  paths also reject C0/DEL. Snapshot/CurrentFacts/CAS identities advance to
  v3. The current v1 competition runtime remains the only authority; this
  prototype remains read-only, non-authoritative, default-off and disconnected
  from DB writers, Web and the Scheduler, with no Phase 2–10 or cutover work.
- Run4 M0.3 attempt-8: validate and replay the complete source-authorized
  WorkflowEvent stream before publishing EVENT_HEAD, bind the current domain
  root to same-transaction business rows, require safe project-relative
  immutable paths and literal Solver receipt paths, and bind completed receipts
  to the current terminal lifecycle event. Snapshot/CurrentFacts/CAS identities
  are explicitly versioned; the prototype remains unconnected to production.
- Run4 M0.3 attempt-7: source-authorize Solver submitted/completed receipt
  events separately from lifecycle generation, project their hash-pinned refs,
  bind submission/external/failure row facts to recorded events, and fail closed
  on unbound non-empty legacy result refs.

## 2026-09-12: keep packaging receipts out of reviewed solver inputs

- Default package_submission command logs use the existing native_receipt namespace, preserving their full contents without introducing new execution-review inputs after Final Audit. Explicit log paths and solver logs remain unchanged.
- Regression tests verify an unchanged packet fingerprint after packaging and a changed execution fingerprint after a real solver log, plus explicit-path behavior (2 tests pass).

## 2026-09-12: preserve an explicit precheck skip after a prior PASS

- A source-verdict-bound continuation can skip Step 13 even when the retained genuine final judgment is PASS or PRECHECK_PASS. The skip still records no review and grants no delivery permission; Step 16 remains excluded.
- Native validation preserves the explicit skip metadata before accepting the prior PASS. Nineteen scoped skip and scheduler tests pass.

## 2026-09-12: bounded opt-in complete evidence capacity

- Preserve default 256 MB per-file / 512 MB total judge assets. Explicit per-role capacity may be bounded at 512 MB per file and 2 GB total for complete saved fields, with unchanged integrity and containment enforcement.
- Fifty focused packet, asset and capacity tests pass, including invalid-limit and tamper rejection.

## 2026-09-12: fresh final-review packets and opaque evidence assets

- After a reusable-audit cache miss, final review packets are rebuilt after current compilation and acceptance checks so isolated reviewers receive the actual current reports. Cached successful audits retain their existing fast path.
- Hash-pinned PDF and ZIP source artifacts can be supplied as complete opaque assets with unchanged size, path and integrity checks; no automatic rendering or extraction is implied.
- Validation: reproduced stale FAIL report consumption before the fix; 51 targeted audit, packet and asset tests passed after the fix. Existing numerical evidence is preserved and newly included without integration.

## 2026-09-12: stable derived-verification evidence

- CLI machine reports separate stable comparison results from complete transient execution traces retained in captured stdout. All checks, failures, return codes and source/output identities remain enforced. The library return contract remains unchanged.
- Validation: six focused tests passed, including variable subprocess logs, mutated output rejection and failed generator diagnostics; two real publication derivations produced byte-identical reports and unchanged scientific artifacts.

## 2026-09-12: publication scope identity ownership

- The exact publication scope review manifest belongs to finalization; changes still require final audit and remain included in final inputs and submission. Its unchanged scientific scope guard, model implementation and quality contracts retain Stage3 ownership.
- Validation: 61 dirty/finalization tests passed, plus real-project scope-guard checks rejecting stronger certification, paper hash drift and model hash drift. Audit launch now uses the existing complete system runtime and checks Excel/numerical dependencies before work starts.

## 2026-09-12: complete isolated judge assets and grounded indeterminacy

- Judge packet v6 can include explicitly declared complete text and binary assets with SHA-256 and size binding. Isolated reviewers receive only their role's verified copies; missing, corrupt, or escaping assets fail closed, and exact quotes from complete text assets are grounded against their bytes.
- Final audit distinguishes valid, grounded evidence insufficiency from malformed or ungrounded review output. Genuine indeterminacy requires new evidence rather than repeating the same review as an infrastructure retry; no verdict is promoted to PASS.
- Validation: 130 focused packet, asset, grounding, aggregation, routing, skipped-precheck, stage and native-orchestration tests passed; git diff --check passed.

## 2026-09-12: final evidence context and bounded recovery

- Final judge packets accept optional SHA-bound required evidence with per-role context budgets; absent configuration preserves existing limits. Configuration changes belong to final audit, retaining a fresh audit obligation.
- Native content-freeze decisions no longer rewrite routed human-review content before committing their SQLite receipt; CLI and Web retain the same decision path.
- A narrow config-only recovery verifies the historical checkpoint-set hash and unchanged Step15 business manifest before restoring authentic checkpoints. It appends exact cause corrections, retains a Stage10 obligation, and preserves skipped/indeterminate review receipts; it never creates a success receipt or quality override.
- Native runs can constrain source Steps and pause before executing a selected Step outside that scope, including after semantic reopening.
- Validation: 128 focused recovery, scheduler, dirty/finalization, judge-packet and native-freeze tests passed, including drift rejection, append-only history, and run-boundary checks.

## 2026-09-11: honor explicit Step13 continuation

- A matching administrator continuation for an existing Step13 verdict now skips repeated packet/model execution, preserves the verdict, and records that final delivery is not authorized. Stage8 closeout respects the validated continuation while retaining unresolved issues.
- Validation: 16 focused precheck/stage/override tests passed, including unapproved and mismatched cases and Step16 exclusion; git diff --check passed.

## 2026-09-11: isolated Codex judge CLI compatibility

- Replace removed --full-auto in the native isolated judge with explicit workspace-write sandbox and never approval policy; keep model, effort, ephemeral execution, and verdict output unchanged. Codex 0.153.4 rejected the old flag before any review started.
- Validation: 9 focused Codex/judge tests passed, including the installed CLI parser; git diff --check passed.
- The dashboard now uses native source Step and last-completed coordinates
  consistently in task cards, workspace headings and the pipeline. It no longer
  adds one to the active native Step or labels a running Step 16 as completed;
  legacy last-completed snapshots retain their existing interpretation.

本文档记录 Paper Factory (Modeling Factory) 的重要更新。

## [Unreleased]

- 摘要完成检查只统计实际源码中的占位符，忽略 LaTeX 注释中的历史标记，并同时识别裸标记和 AbstractPlaceholder 宏；避免已完整排版的摘要因历史注释被要求重写，实际残留占位符仍阻断后续步骤。

- 最终输入核验支持已实审的历史 Solver 输入版本：原提交回执保持不变，只有与原指纹完全一致的历史原件、明确审阅记录和精确当前版本同时存在时才接受后续修订。历史字节及衔接记录随最终快照和提交包保留；未登记改动继续失败。
- 标准目录以外的既有输入和支撑文件可使用逐文件、SHA 绑定的显式提交路径声明；声明只能增加所需材料，不能排除产物或授予质量通过，内容冻结和最终实现指纹同时绑定该机制。

- 交付物表格检查复用活动 LaTeX 文档展开接口，识别 literal input/include 中的已有表格；缺失、循环和越界依赖保持失败关闭，字段比例和附件检查规则不变。

- Excel 附件数值追溯使用排序索引定位相邻候选，再以原有舍入及相对容差判断；避免每个单元格线性遍历全部真相数值造成大附件审计超时，不改变原阈值或检查范围。

- 数值来源核验改为分批解析现有 JSON manifest，并使用临时 SQLite 索引逐项核对当前结果，避免大型 Excel 产生双份数值字典导致内存耗尽；保留原有来源/坐标覆盖、校验和、类型区分及论文数值容差，paper audit fingerprint 包含新增核验辅助模块。

### 新增

- 新增尚未接入生产路径的 Run4 M0.3 shadow/prototype 合同：把 pure dirty classifier 的 semantic identity 与三文件 operational implementation manifest 拆分，把持久 Solver receipt owner 覆盖建模为独立 semantic/symbol-span identity，并以冻结 v1 为信任根增加 additive Workflow V2、ContractPinSet/runtime pin；Project Snapshot V0 使用单一 `mode=ro`/`query_only`/`BEGIN` SQLite transaction 返回 typed availability，CommandEnvelope/structured read-set CAS 只消费显式 immutable facts并固定 non-authoritative、zero-mutation/side-effect。当前 schema-v9 缺 project/run generation、recorded pins 与 delivery authorization binding，因此 snapshot 明确 `PARTIAL` 且不能 CAS accept；没有表、迁移、API/UI、feature flag、writer/cutover 或 provider/model/Solver 接入。`APPROVE_M03_WAL_ADDENDUM` 采用方案 A：SQLite C/VFS 可在严格的 `<db>-wal`/`<db>-shm` 路径内实施辅助 I/O，应用代码仍零 workflow mutation、零主动写操作；quiescent 主 DB 与业务逻辑不变，辅助文件仅作 analysis/evidence 观测且不进入 Snapshot/CAS 身份。该 addendum 只关闭 WAL 设计冲突，不构成整体 M0.3 实现批准或完成声明。
- 新增默认关闭且未接入生产路径的 M0.2 Stage-v1 shadow scheduler：`StageV1ReadinessAdapter` 只消费同一 revision-bound 已记录快照并防御性冻结 readiness DTO，`SchedulerCore` 纯函数返回非权威、零 mutation/side-effect 的规范化计划，parity receipt 精确区分 MATCH、逐项 correction、未知差异、v2 错误与 v1 不可表示。所有值分别绑定 M0.1 workflow semantic/analysis 身份；analysis-only 证据不改变 semantic plan identity，unsupported owner mode 在 receipt 边界失败关闭。现役 v1 Scheduler、SQLite schema、Legacy adapter、provider 与真实运行状态均未接入或改变。
- 新增未接入生产运行路径的 `workflow-contract-bundle-v1`：从现役 10 Stage、Step 0–16、Step 8.5、全部 Gate、contest phase、dirty 分类与 artifact owner 真值纯函数编译深度不可变合同，并用 `factory-canonical-json-utf8-v1` 生成可复算 bytes/SHA-256。Owner 兼容编译器保留 v1 首命中，同时返回全部匹配、稳定规则 ID、精确 pair-scoped `INTENTIONAL_PRIORITY` 及 NO_OWNER/MULTIPLE_MATCH/SHADOWED/UNREACHABLE/UNANALYZABLE 诊断；严格模式尚因已固化的同 owner `paper/**/*.tex`/`paper/*.tex` 冗余失败关闭。
- 新增 Phase 0 源码事实 ADR、application-writer 旁路/未来静态门禁清单和 v1 characterization corpus；只读测试将运行时 schema-v9 与现役文档绑定，并校验 normal、dirty、semantic reopen、Human Gate、recovery、packet rebuild、technical terminal 与 Solver receipt 描述不漂移。文档明确 `TransitionCoordinator` 仍是目标而非当前唯一 writer，并区分 `web/auth.db` 控制面授权与项目 SQLite workflow decision 的信任和生命周期。
- 工作流状态升级为 schema-v9：活动 dirty 状态及清除 receipt 按 `(flag, owner_stage)` 持久化，同一 MODEL/RESULT 域的多个责任 Stage 不再相互覆盖；LaTeX 数学语义补齐 `\\(...\\)`、`math`/`displaymath`、`alignat`/`flalign`/`eqnarray` 和 TeX definition prefix。最终输入与 submission bundle 增加 authored-artifact ownership coverage gate，未登记且未声明的普通产物失败关闭。
- Consultation 决策现在把完整答案写入不可变 SQLite receipt，由控制平面确定性重建 `human_review.md` 段落；执行前校验投影并将权威答案直接嵌入提示词，Step 事件/checkpoint 绑定 effective prompt、决策 ID 与 Web researcher note 哈希。Stage 项目仅允许回滚到 `step_v2`，禁止继续降级 Legacy；所有回滚检查 pending Gate、开放请求、调用方 revision 和 Legacy/SQLite 游标一致性。
- LaTeX 构建证据升级为三轮 recorder 与 bibliography 双合同：所有 `.fls` 输入分类为已声明项目文件、未声明项目文件、项目符号链接、受控 TeX runtime、禁止外部文件或允许生成物，异常路径不再静默过滤；编译清理继承的 TeX/BibTeX 搜索环境，启用 `-no-shell-escape`/严格 `openin_any`，并要求三轮项目输入身份一致。
- 新增 `bibliography-build-receipt-v1`：编译前清除当前 job 的旧 `.bbl`/控制文件，按 `\\bibliography` 或 `\\addbibresource` 唯一选择 BibTeX/Biber，任何 backend 失败或未解析 citation 均终止；receipt 绑定 backend 版本、首轮 AUX/BCF、`.bib`、项目 `.bst` 与生成 `.bbl`。最终 fingerprint、acceptance 与 evaluator 合同同时绑定该证据。
- 最终发布链现在在 Final Audit 开始、acceptance 构建及 release pointer 切换前验证实际消费的人工 Approval receipt；不可变 release 会复制这些 receipt 并纳入 delivery manifest。决定 receipt 改为 `O_NOFOLLOW` 单次字节读取，并新增 `scripts/decision_receipt_repair.py`，仅当 SQLite 可重建字节与原 SHA-256 完全一致时恢复缺失文件。
- CI 新增固定 TeX Live/BibTeX/Biber 的 `latex` 作业，真实编译、外部读取、符号链接、旧 `.bbl`、Biber 和未解析引用反例不得因工具缺失跳过。提交 ZIP 使用固定时间戳、权限和成员顺序，可从同一 manifest 确定性重建。
- 新增 `LatexCompileContract`、`-recorder`/`.fls` 输入对账和按命令插入位置生成的展开文档流。静态依赖解析与编译统一使用“主文件目录 → 项目工作目录”搜索顺序；缺失、循环、动态依赖或 declared/observed 项目输入不一致均失败关闭。数字、数字链和符号检查共享展开流，因此跨文件章节状态、插入顺序与 use-before-definition 坐标不再丢失。
- 提交清单升级为 `submission-bundle-manifest-v2`：成员收集及 owner/domain 元数据绑定统一 artifact-ownership registry，最终 PDF、活动 LaTeX 源/参考文献、显式允许的模型/结果/图表和声明附件形成唯一成员集合；final input、Judge packet、submission fingerprint、final acceptance receipt、打包器和 release verifier 绑定同一 manifest。打包拒绝符号链接/越界路径，并在写 ZIP 后逐成员复核中央目录、大小与 SHA-256。
- 新增 Human Decision receipt 读取时强制验证：路径、普通文件/符号链接、大小、SHA-256、schema、request/decision/gate/generation 和数据库决定正文必须一致；Approval receipt 缺失或篡改后 Gate 与 Final Audit 失败关闭，Web diagnostics 显示 `DECISION_RECEIPT_MISMATCH`。
- 新增 schema-v8 审计加固合同：Human Decision 拆分为按 gate/request/generation 和 subject/options fingerprint 绑定的不可变请求与结果；每个决定生成 `.factory/decisions/<gate>/<request>/<decision>.json` 内容寻址 receipt，固定 selection/human-review 文件仅作兼容投影。内容冻结拒绝会清除 pending、失效 Stage 9 之后的 checkpoint 并回到 Stage 9，修复完成后才生成绑定新内容的下一代请求；陈旧的开放请求也可原子标记为 superseded 并重新绑定。
- 新增统一递归 LaTeX 依赖图：从权威论文入口解析 `input`、`include`、`subfile`、`bibliography` 与 `addbibresource`，报告循环/缺失依赖并排除未引用草稿；content freeze、dirty 语义分类、提交/final fingerprint、数字/符号/数值链审计、Judge packet、Web artifact browser 与 submission package 共享活动源合同。
- event-v2 同时记录完成主体与迁移结果坐标，并用 aggregate root 绑定 contest policy、project config、决策、dirty、checkpoint 与 Solver side table；dirty cause 和 Stage checkpoint 增加 append-only 历史，投影失败可记录、诊断和恢复。
- Cloud Solver 请求传输精确输入字节与 SHA-256、声明输出和 seeds，保留 queued/submitting/running 状态并执行实际进程取消；Web 上传改为限额分块写入，普通用户项目申请只接受上传目录内的 PDF/Markdown。
- 新增 GitHub Actions `CI` 工作流，分离 core、LaTeX、Web（含前端构建）与 Cloud/数值依赖测试；`main` 分支保护要求 PR、分支最新且所有 required checks 通过，并阻止 force push 与分支删除。
- Solver Job 新增稳定 `idempotency_key`、回执 `request_sha256`、Stage/subtask/revision/attempt 所有权和唯一约束；Cloud provider 接收幂等键并支持按持久 job ID 对账，本地无法证明提交状态时失败关闭而不盲目重提。
- Web 人工 Gate 采用“证据文件原子 rename + fingerprint → SQLite decision/state/event 同事务”顺序；诊断页直接展示 Native 调度坐标、Recovery Status 与 Audit Timeline，并标记已发布但尚未入账的 orphan decision artifact。
- 新增授权 HMML 完整数据集：保留原始 JSON/Markdown 与来源哈希，确定性展开 97 个可引用方法文档；与现有 21 个精编条目组成双登记表，方法召回升级为“层级分支粗选 → 叶方法细排 → 数据/证据复排”。
- 新增 `problem-plan-v1` 问题专属 DAG：Step 0 必须产出无环、路径受限、方法引用已登记的任务图；后续建模/求解按拓扑依赖承接，最终审计与发布指纹绑定该业务真相产物，但固定十 Stage / 十七 Step 调度权威不变。
- Web 新增 `node-output-v1` / `ContentBlock` 结构化输出协议、前端渲染注册表和“任务图”页；建模方向与问题 DAG 统一渲染摘要、方法卡、依赖图、提示和产物链接，模型上下文使用独立的限长白名单投影。
- 新增 `stage_v1` 10-Stage 权威调度：schema-v6 SQLite 原子持久化 Stage/subtask/source-Step 游标、subtask checkpoint、输入基线、dirty flag 和责任 Stage clear receipt；新项目默认启用，旧 `step_v2` native 项目仅通过显式 `scheduler-activate` 切换，并可在停止且无未清 dirty 时显式回滚。
- 新增独立 Step 8.5 reviewer-entry subtask、条件式 Step 13、双域 paper-audit fingerprint 和 final-input manifest 冻结守卫；科学语义变化按 MODEL/MATH/RESULT/VISUAL/PROSE/CITATION/FORMAT dirty flag 重开责任 Stage，未知变化失败关闭，最终快照变化会中止发布而不复用旧 receipt。
- Web 比赛工作区新增完整 P0–P2 控制台：默认八阶段/可下钻 17 Step、最近三步时间风险预测、持久行动中心、三类 gate-aware 人工决策、canonical/Solver/审计证据驾驶舱，以及绑定 Final Audit 和原子 current release 的交付就绪清单与 PDF/ZIP 下载；Legacy 无比赛时钟时明确显示未配置，所有新接口继续执行项目 ACL。
- 新增 `contest_core_v1`：新建 Native 项目在 schema-v6 SQLite 中持久化默认 74 小时或显式官方 deadline、T−6h content freeze、T−2h delivery freeze 和六小时终端交付保留；共享 deadline 约束 Step、模型/命令、恢复、Final Audit、打包和 release pointer 切换，耗尽后失败关闭。内部 Step 0–16 映射为八个比赛阶段，Step 3 与交付前内容冻结成为默认强制的 SQLite 权威人工节点，冻结后回退需额外人工 override；Web 展示八阶段与冻结/提交倒计时。Solver/Final Audit receipts 与 snapshot hashes 明确保留为不可变机器证据，而 checkpoint、选择 Markdown 和 Web 状态仅为可重建投影。
- 新增不可变原子 release：每个最终快照发布到 `papers/releases/<base>/<snapshot>/`，包含审计 PDF、submission ZIP、manifest 和审计 receipts；全部校验完成后只原子替换 `papers/<base>/current.json`，失败保留旧 current，顶层 PDF/ZIP 仅为兼容副本。
- Web 管理端与 CLI 新增交付 override 签发、查看和撤销；权威记录持久化在 `web/auth.db`，并区分 `continue_after_gate2` 与绑定精确 SHA-256 的 `deliver_snapshot`。
- 新增 `web/backend_service_health.sh`，统一 full/backend-only 部署验收：验证 systemd MainPID、ControlGroup、全部 8000 listener 所有权及稳定窗口内 `NRestarts`，再接受 HTTP 结果。
- 新增 `quality_contract.json` v4：按最大化/最小化方向硬验有效松弛界、预算阶梯、平台期语义和跨算法族对照工件；新增 canonical 派生物 manifest、生成辅助脚本及临时目录重生成/diff 门禁。
- Solver 新增 content-addressed 两阶段 receipt：submission 绑定 runtime、代码、输入、参数摘要和 seeds，completion 绑定终态及声明输出哈希；seed receipt 明确只证明声明，未证明进程实际消费该 seed；native/Legacy 统一通过 `--status <jobid> --json` 返回 fail-closed `solver-job-evidence-v2`。
- 新增 R0a exact-runtime 硬门能力校准：数学/执行角色必须同时覆盖 oracle-backed hard defect 与 neutral transform，并将每个 held-out packet 的 capability observation 与 K>=5 重复稳定性、evaluator/packet/condition hash 逐项绑定；报告失败关闭且不自动放权。
- 新增 R0b pairwise selector 可靠性合同：冻结 dev/holdout family、exact evaluator/packet identity、AB/BA 与重复观测，使用 dev-only TIE 带和 Wilson 界分别报告 proxy/human readiness；未取得独立人工 holdout 前不允许自然稿择优。
- 新增 R3 shadow portfolio 编排器：候选须先通过 R0a 与 R1/R2-min，绑定不可变求解/结果/PDF证据并遵守预算政策；报告 selector 覆盖、TIE、主线分歧、独立 adjudication/regret 与候选数 K，但始终不自动改变主线。
- 新增 selector cutover authorization 校验器：人工 receipt 必须 hash 绑定 ready 的 R0a/R0b/R3 报告，并限定 evaluator、workflow step、项目/题型、最大 K、预算、packet builder、TIE 带、canary 与有效期；assessment 与实际路由事件保持分离。
- Web 新增 Solver Jobs API 与项目工作区面板：按项目 ACL 展示本地/云端作业状态、耗时、输入输出引用及 `solver-job-evidence-v2` 两阶段 receipt 完整性。
- 新增独立 `factory_core.audit` 子系统与 `factory audit` CLI：Step 4、5/6、10 分别运行 `model`、`results`、`paper` 确定性审计并将失败同步到 issue ledger；最终 `final` 审计按内容指纹记录在项目 `.factory/audits/<snapshot>/`。四类审计均可脱离交付运行并复用同一输入与 checker 契约的 PASS，只有 `final` profile 可以授权交付。
- Web 新增独立的完成论文展示 ACL：管理员可分别配置默认未登录访客和具体注册用户的只读论文集合；注册用户继承公共集合，展示授权不授予项目控制、日志或内部文件权限。
- 新增 `factory_core/` Python 编排核心：项目内 SQLite 快照、追加式事件、乐观 revision、注册式 Step/执行后端、重试与验证驱动恢复。
- 新增旧建模项目的两阶段显式迁移和可审计 rollback 命令；活动进程、状态冲突和已退役社会科学项目会拒绝导入。
- 新增原生 Step 0-16 lifecycle/catalog、模型 backend registry、统一 `FactoryService`、SQLite solver policy/job 记录，以及本地与可替换 Cloud Run solver adapter。
- 新增 `apps/`、`benchmarks/`、`legacy/` 和仓库边界/兼容移除文档；运行数据继续保留原位置并可通过 `FACTORY` 指定数据根。
- 新增根 `pyproject.toml`/`uv.lock`、hash-locked Web/Cloud requirements export，并沿用前端 `package-lock.json`。
- Cloud Solver 新增共享 Cloud Run IAM ID Token 获取模块、Python-only 能力清单、镜像构建冒烟测试、Cloud Build 配置预检和按 digest 回滚脚本。
- Cloud Solver 新增恶意路径、环境覆盖、请求/输出上限、低权限 UID 和不可变部署回归测试。
- Web 项目状态增加稳定的题目身份、题目标题、存储域与归档标记；前端按题目内容标识聚合同题多次运行，并可展开历史运行。
- 新增 `AGENTS.md`，作为 Codex 和通用 coding agent 的精简仓库入口。
- 新增聚焦的仓库卫生检查，保护现役文档不再出现默认凭据、旧内存用户库说明或 secret 值展示指令。

### 变更

- M0.2 workflow bundle 公共边界在源码行为授权之前先验证完整、深度不可变的精确 runtime DTO 图：错误容器、子类、嵌套 DTO、原始类型、可选字段和非法 UTF-8 值均在 validator、readiness adapter、shadow core 和 receipt 入口稳定抛出 `WorkflowContractValidationError`，不再泄漏属性、迭代或 canonicalizer 异常。新的 identity-matched 反例、classifier↔Step 13 同步伪造和仅 monkeypatch 生产 validator 的 kill test 证明四入口测试不会因 identity 不匹配而假通过；Step prompt、Gate producer、owner compiler schema/诊断逐字段 analysis-only 正测仍保持 semantic identity 不变。本项仍仅为 M0.2，未开始 M0.3 classifier semantic/implementation identity split。
- M0.2 workflow bundle 公共信任边界改为从源码重建并逐字段授权 Step、Gate、ContestPhase、dirty classifier 与 owner behavior rules；新增 Stage↔Step phase/owner/budget、ContestPhase↔Step 双向映射和 Step 13 operands↔classifier semantic flags 精确绑定。同一固定版本下的 supplied 自哈希变体在 validator、readiness adapter、shadow core 和 parity receipt 前失败关闭；Step prompt、Gate producer、owner diagnostics 与 owner compiler 实现 schema 继续仅影响 analysis identity，合法 canonical bytes/hash 与 shadow-only/zero-write/zero-dispatch 合同不变。该修复仍为 M0.2，不处理 M0.3 classifier semantic/implementation identity split。
- M0.1 workflow-contract 主 bytes/SHA-256 改为只覆盖语义投影：保留 Stage/Step/Gate 条件、预算、dispatch、owner 规则、owner resolution mode 与精确优先级授权，排除 analyzer witness/诊断文本、owner compiler 实现 schema、Step prompt 与 Gate producer 源码位置；bundle validation 对当前 resolver 不支持的 mode 失败关闭，并保留完整 analysis bytes/SHA-256 作为独立审查身份。该修复不接入运行时、数据库或 M0.2/M0.3 持久化。
- 规格—实现对账门禁 S4 改为按目标方向验证松弛界：最小化接受结构化 `lower_bound`，最大化要求 `upper_bound`，并兼容旧顶层字段；S1 同时识别 `1500/3000/6000 次目标评估` 形式的直接预算阶梯，避免把已明确的执行预算误报为不可解析。
- Web 求解任务面板将任务状态、两阶段凭证限制/错误码、输入输出字段和标准日志入口统一为中文展示；API 原始值保持不变并保留在诊断提示中。
- Final Audit 统一为最终编译、完整 Step-10 paper/provenance suite、视觉/页数门禁、packet/fingerprint、enforce-mode 三角色 Judge、判决前后快照复核、judgment receipt 与 final acceptance receipt；PASS 复用也必须验证双 receipt 和当前快照。
- Native 与 Legacy Step 16 统一消费同一个 Final Audit 和原子 release publisher；项目清理提前到最终快照构建前，`2026-08-09.atomic_release_v7` 成为当前交付合同。
- 新项目 Step 4 使用 quality-contract v4；Step 5 必须显式声明 solver inputs/outputs/seeds，并由任务内 `FACTORY_SOLVER_JOB_ID` 写 provenance。Step 10 paper audit 新增确定性派生物硬门，旧 v1–v3 合同继续按原边界审计而不被静默升级。
- Shadow cutover manifest 升级为 v2，只有 hash 绑定且 `hard_gate_ready=true` 的 R0a 报告才可能产生 cutover 建议；v1 继续输出诊断但永久 `cutover_ready=false`，所有放权仍需人工批准。
- Step 13 缩为数学单角色预审，`PRECHECK_PASS` 只允许继续摘要与润色；完整数学/执行/论文三角色 Gate 2 仅在 Step 15 后的 `final` 审计执行。Step 15 明确为 `CONTENT_READY` 边界；Step 16 改为独立审计与交付之间的兼容适配器，只消费 `PASS` 或显式 `OVERRIDDEN` 审计结果，复制 PDF、submission 打包和清理不再属于审计职责。
- 移除仓库内 `superpowers-*` agent Skill、配套 workflow 和强制执行规则；
  历史 `docs/superpowers/` 与 `artifacts/superpowers/` 记录继续保留。
- `run_paper.sh` 降级为兼容启动器；新项目默认 `native_v2` 并原生运行 Step 0-16，冻结 Bash 只供未迁移或显式回滚项目使用。
- CLI、Web 状态和项目控制对已迁移项目统一读写 `.factory/state.db`；checkpoint、heartbeat、marker 和 diagnostics 成为兼容投影。
- Web 项目创建/控制直接调用 `FactoryService`，后台执行统一由 Python worker launcher 启动；根 shell 命令保留为兼容入口。
- Web 云策略更新携带 project revision；`.env.cloud` 对 engine 项目降级为投影，不能覆盖 SQLite 或全局 quarantine。
- Web/Cloud 构建改用锁文件，backend/frontend 运行时启动脚本不再创建环境或安装依赖。
- `agy` 模型 SDK 作为 `models` extra 与生产 Web 环境一起锁定；Cloud Solver 依赖集保持隔离。
- 生产 systemd unit 改用仓库根工作目录、根 `.venv` 和稳定 ASGI 入口；部署预检会拒绝旧 unit。
- 前端构建升级到 Vite 8 / Vue plugin 6，并更新 Axios；全新 `npm ci` 审计不再报告已知依赖漏洞。
- Web Dashboard 更新深浅主题、工作区导航与状态视觉层级；选择、咨询、诊断和 Solver Jobs 仍保持独立入口。
- 社会科学执行路径正式退役，历史 prompt、Stata 脚本和项目产物继续保留但不再承诺恢复运行。
- Cloud Solver 鉴权统一为私有 Cloud Run IAM；CLI、监控和 Web 使用同一 ID Token 策略及无密钥专用 Invoker impersonation。
- 云端能力收敛为经过镜像冒烟验证的 Python；API、Web 和 Shell 路由从同一能力清单读取，未安装运行时在提交阶段拒绝。
- Cloud Build 改用 `${BUILD_ID}` 不可变镜像部署并记录 revision/commit/image；`latest` 不再用于生产部署。
- Web 现役文档对齐 SQLite 用户库、bcrypt、注册/管理员审批、项目申请与 ACL 权限模型。
- `web/backend/main.py` 明确为 FastAPI 主入口；`web/backend/app.py` 仅作为兼容启动器。
- 生产敏感值以 GCP Secret Manager 为权威来源；文档和诊断只显示元数据、绑定状态与权限状态。
- Web 部署构建改由服务用户执行，避免 root-owned `dist/` 阻止普通用户后续构建。
- 重复部署、测试和上传报告标记为历史快照，并指向当前 runbook。

### 修复

- 修复 Run4 M0.3 attempt-5 复审发现的 Solver lifecycle P1：Project Snapshot V0 以单一源码授权 event/status 映射同时派生合法状态词表与连续 generation 历史校验，补齐现役 `SOLVER_JOB_SUBMITTING`、`SOLVER_JOB_QUEUED`、`SOLVER_JOB_CANCELLING`，保留 generation-1 `SUBMITTED` 兼容规则；未知状态/事件、generation gap 和 row/latest-event 矛盾继续 typed fail closed。真实 SQLiteStateStore 的 repeated submitting、queued、cancelling 路线进入 INVOCATIONS_SOLVER，并保持 Snapshot→CurrentFacts→shadow CAS entity generation 绑定。
- 修复 Run4 M0.3 attempt-4 实施审查发现的三项 shadow/prototype 信任边界：Project Snapshot V0 现在从源码重建 SQLite 领域 policy，在生成 AVAILABLE fact 前绑定项目游标、Stage/checkpoint ID 与 receipt、dirty cause/clear/rebase 历史、pending request identity、projector event chain、Solver lifecycle generation/status/owner 与 hash-pinned immutable refs，并保留现役初始 `submitting` 来源状态；Command CAS 为 required fact 显式声明 entity/subject binding，并在 read-set 重建前返回稳定 generation/fingerprint mismatch；Snapshot→CurrentFacts 以 `compile+validate` 重建固定六项映射、携带完整可恢复 ContractPinSet 并覆盖四类 synthetic COMPLETE accepted 路线。现役 schema-v9 仍为 PARTIAL，未增加表、迁移、API/UI、生产导入、writer/cutover 或真实 dispatch。
- 移除两个旧 MinerU OCR 入口中的硬编码 Bearer 凭据，统一要求由现役 `MINERU_TOKEN` 环境变量注入并在缺失时于任何网络命令前失败关闭；新增冻结 payload 高置信扫描，校验三段式 JWT 的 base64url JSON header/payload、硬编码 Authorization Bearer、既有供应商/私钥签名，并只报告路径与规则名。证据归档路径策略同时拒绝 `.claude/*.lock` 运行锁、环境凭据文件、数据库、依赖/cache、符号链接和非普通文件；凭据撤销/轮换仍是账号所有者的外部操作，未在本轮执行。
- M0.2 recorded snapshot、readiness/decision/parity DTO 公共边界现在拒绝映射、序列、字符串、整数、tuple 和 DTO 子类，逐字段要求严格 UTF-8，并在属性读取前拒绝未初始化 exact DTO；parity receipt serializer 也先完成结构验证。因此非法 Unicode 或携带额外可变状态的伪造值不再从 adapter/core/receipt 泄漏 canonicalizer 或属性异常。合法 schema、canonical bytes/hash 与 shadow-only 行为不变，仍未开始 M0.3。
- M0.2 supplied `WorkflowContractBundle` 的共享验证边界现在用当前 `stage_catalog_version` 从源码 `STAGE_CONTRACTS`、`STEP_CONTRACTS` 与 `CONTEST_PHASES` 重编译完整 Stage 行为投影，并校验 Stage/subtask 顺序与 ID、source/checkpoint Step、contest phase、owner、继承预算、kind、condition、全局 subtask ID/调度坐标和 Stage 内 key 唯一性；`reviewer_entry_gate` 的 Step-8 无 checkpoint completion gate 与 Stage-10 Human Gate 继续按源码类型显式验证。adapter、纯 core 与 parity receipt 在 readiness/主动计划/receipt 创建前共同失败关闭，`step:N` 路线生成点还会断言 N 属于已验证 Step 集合。合法 bundle schema、semantic/analysis identity、readiness、plan 与 parity golden 保持不变，仍未接入 authoritative writer、Legacy、数据库或生产 dispatch。
- M0.2 shadow readiness 边界对 workflow status、active invocation type/state、domain readiness、fact availability、delivery capability 与 dirty flag 使用精确大小写词表，并由 adapter 和绑定可信 workflow bundle 的纯 `SchedulerCore` 双重失败关闭校验；core 会重建 catalog 并复核 schema/contract/generation、cursor/attempt、checkpoint、owner 与所有 target，伪造 DTO 不再能把 catalog 或域目标当作新信任根。固定条件边界要求唯一非 `ALWAYS` 子任务只能是 Stage 8 Step 13 的 `ANY(semantic_dirty_flags)`。parity value/receipt 升级至 v2 并把 `execution_route` 纳入比较与 correction 精确绑定；receipt 在比较前重算 input/readiness/plan hashes 和关联，并拒绝权威计划或任何 mutation/side effect。workflow status 穷尽处置表保持不变：只有 `ready/running/retrying` 可进入普通 catalog scheduling，`archiving` 与缺少显式恢复/domain 事实的 `interrupted` 和其他非运行状态统一 `WAIT`，显式安全事实继续优先。checkpoint/immutable 引用继续要求规范 64 位小写 SHA-256，技术终态与比赛交付权限继续分离。该修复不接入或改变 v1 authoritative writer、Legacy、数据库或真实运行数据。
- 关闭 Stage 调度继续审计的 2 个 Blocker 与 2 个 Major：Step 3 的 SQLite 决定现在确定性生成并双阶段校验 `chosen_method.md`/`method_decision.md` 投影及候选 fingerprint；新增统一 artifact ownership registry 供 dirty、Finalization、Judge 缺件、Web 诊断和交付 manifest 共用；失败子任务在终止事务中持久化 baseline delta，所有 scheduler/control-mode 回滚统一复算 manifest 并拒绝活动 attempt、Finalization/projection 残留；LaTeX 数学定义检测改为覆盖 LaTeX2e/xparse、环境、TeX primitive、expl3 与 PGF 宏的定义族失败关闭策略。
- 修复 `stage_v1` 三项合并阻断边界：中断后 COMPLETE 恢复改用携带完整 metadata 的真实 validation/execution 领域对象并幂等晋升 checkpoint；活动 LaTeX 宏、单位及计数器定义变化纳入数学语义指纹；语义重开依据全部 dirty cause 的最早 `owner_stage`，并从 Stage catalog 推导 Step 恢复边界，因此后期修改 `problem/problem_plan.json` 会回到 Stage 1 且只能由 Stage 1 成功 receipt 清除。
- 修复 submission ZIP 递归纳入未冻结 `paper/draft.tex`、LaTeX 子文件按错误目录优先级解析、模块化论文数字链丢失父章节状态，以及决定 receipt 删除/篡改后仍可通过 Approval Gate 的四项审计阻断问题；内容冻结拒绝现在记录规范 `WORK_REOPENED` recovery 事件与失效 checkpoint 清单。
- Web 相对时间格式化同时接受 Unix 秒级/毫秒级时间戳、数字字符串和日期字符串，避免 Solver Jobs 返回整数 `requested_at` 时触发渲染异常并使“求解任务”页整体空白；窄屏任务行改用三行自适应布局，完整保留状态、耗时、时间与 receipt 入口。
- Web 八阶段流程下钻区提高标题对比度，并为已完成、运行中和待处理步骤使用与状态底色匹配的前景色，避免步骤文字与实心状态背景同色而不可读。
- Web 审计事项解析在找到首张 issue 表后会于表尾停止，避免把后续增量审计表的 `Severity` 列误读为 `Status`，从而在已完成 Final Audit 的项目行动中心虚报未解决事项。
- Final Audit 的 execution packet 不再纳入 `step_*`、`native_judge_*` 或 `native_receipt_*` 审计运行日志，避免 Judge 及 receipt 构建器写入自身日志并使刚通过的快照立即失效；solver 与模型执行日志仍参与证据指纹。
- Final PDF 视觉门禁不再把 1–2 个字符的公式上下标按不可读正文阻断，而是保留为小字 warning；连续文本和数值低于 4.5pt 仍为 blocking。
- Final Audit 的 Judge grounding 基础设施重试现在会把失败的 `ref_id`、错误原因、声明的 `chunk_id` 以及从不可变 packet 中提取的逐字候选原文反馈给对应角色，并在有界轮次内只重跑当前仍为 indeterminate 的角色，避免使用相同提示盲重试或在引用错误已收敛时过早永久失败；严格逐字匹配和三值判定保持不变。
- `solver_submit.sh --args` 生成提交 receipt 时使用 `--argv=<value>` 传递作业参数，避免 `--only` 等短横线开头的 solver 参数被误解析为 receipt 工具自身选项而在启动前失败。
- 显式指定的 `*.verification.latest.txt` 报告在每次运行前清除旧内容，避免历史项目路径或 verdict 被追加到当前 Final Audit packet，造成跨项目证据污染和误判。
- Dev 测试依赖显式锁定 `httpx2`，避免 Starlette 1.3 `TestClient` 回退到已弃用的 `httpx` 兼容层后挂起；超大请求测试改为直接驱动 ASGI middleware，并确认 413 在 JSON 解析前返回。
- 原生审计将科学判退与评委基础设施失败分流：Step 13 仅处理真实 math FAIL，
  最终审计再处理 math/execution FAIL；`INDETERMINATE_REVIEW`、格式/grounding/路由故障只重试当前角色，
  耗尽后明确停止为 `PERMANENT_JUDGE_INFRASTRUCTURE`。只有 packet 证明上游文件确实
  缺失时，才回到该文件最早责任步骤。
- 原生失败与重试事件保留执行器和验证器的结构化 metadata；Step 10 逐项报告
  `failed_check`、report 和 returncode，模型退出 0 但产物缺失统一标记为
  `TRANSIENT_ARTIFACT_MISSING`，不再以 `UNKNOWN` 重跑。
- 模型调度器会隔离已确认不支持/不可用的候选并继续健康 fallback；API judge 输出和
  rendered prompt 路径统一传项目相对路径，避免同一不支持模型与绝对路径错误反复调用。
- 当前 canonical 汇总强制绑定 `chosen_method.md`、逐问题 source 文件和 solver
  provenance；`quality_contract` v3 要求 hard claim 声明数学域，连续时间 hard claim
  必须提供独立事件定位、认证误差界或双实现证据，复用同一采样数组不能 hard PASS。
- 原生隔离评委提示明确覆盖通用 agent 启动读取，禁止读取 guide、human review、memory 和 Git 状态，并要求保留 `judge_packets/<role>/` 角色目录，避免把存在的 packet 误报为缺失。
- 评委证据包改为硬性文件优先；数学包优先纳入每个模型的 `02_model`/`03_solve` 入口，执行包使用 360 KB 上下文预算，确保问题结果、求解日志和验证报告不会被大型附录挤出；声明式 claim 路径必须与实际产物一致。
- 原生隔离评委不再让 Codex 最终回复覆盖 `judge_outputs/*.md` 协议文件；
  Step 13 预审明确忽略流程要求保留的摘要占位符，而 Step 16 最终复审仍将其视为阻断缺陷。
- 原生 Codex backend 现在与兼容 runner 一致，在未显式指定模型时继承
  `CODEX_MODEL`，避免配置模型失败后的内置 Codex 重试静默切换模型。
- 项目级 `gate2_delivery_override.json` 不再具有授权能力，只能作为请求或历史痕迹；
  管理员数据库授权保留真实 verdict/错误证据且不生成虚假 PASS receipt，精确快照交付
  授权在生成最终验收凭据后被消费。
- Step 14 摘要提示不再硬编码“Gate 2 已 PASS”；override 交付必须读取并保留真实
  verdict 与未解决问题，避免后续 agent 把治理旁路误述为质量通过。
- Worker lease 现在在 SQLite transition 内同时核对 PID 与 lease；连续 Step
  执行期间保持 `RUNNING`，任何存活 Worker 都会阻止重复 start，失去 lease
  的旧 Worker 以 `RunnerLeaseLost` 退出且不能提交后续事件。
- 中断恢复不再无条件覆盖人工等待或永久失败状态；recovery reopen 与正常
  reopen 统一写 `STEP_REOPENED` 并共享同一配额。
- migration rollback 同时切换 `control_mode` 和 `runtime_generation`，CLI、Web
  与 `FactoryService` 在回滚后统一使用 Legacy adapter。
- Step 2 候选流不足现在进入标准 `RETRY_SCHEDULED`/`STEP_FAILED` 预算，不再
  从 prepare 抛出非法转换并遗留 Worker 元数据。
- Solver job 使用独立 `job_revision` 完成后端确认；工作流控制事件不再造成
  已启动任务的 external ID 丢失。CLI Worker 与 Web 默认使用同一个
  `build_solver_backends()` 注册表，Cloud Run adapter 仍受全局 quarantine。
- Cloud Solver client 通过权限受限的临时文件组装和提交 JSON，请求正文与大型 working file 不再进入进程参数，避免触发 `ARG_MAX`。
- Web 普通恢复、Step 3 选择和人工咨询回答现在统一调用
  `FactoryService.resume_and_start`，成功后提交 `WORKER_LAUNCHED` 并实际
  启动 worker；此前 Web 只把状态切到 `ready`。
- Web 人机 gate 请求携带 project revision；stale 请求在写决策和启动
  worker 前返回冲突。终止、完成和归档中的项目不能被重新启动。

### 安全

- 前端构建链将传递依赖 `nanoid` 从 3.3.16 锁定升级到 3.3.18，修复自定义生成器在零长度输入下可能无限循环的 `GHSA-2v37-7h3g-55p8`，并恢复 `npm audit` 零已知漏洞。
- Cloud Solver P0 执行层增加严格任务/路径校验、12 MiB 请求上限、输入只读/输出独立、环境允许列表、隔离启动的资源限制包装器、UID/GID 10001 降权及 CPU/内存/磁盘近似量、文件描述符、子进程、输出文件/目录和日志硬限制。
- Cloud Solver 仍保持全局 quarantine：同实例任意代码访问 metadata 和运行服务账号的风险需要独立 Cloud Run Job 或等价 sandbox 才能解除。
- Cloud Run Solver 进入 P-1 安全隔离：移除匿名 Invoker并默认关闭脚本执行；P0 随后将临时双重认证收敛为单一 Cloud Run IAM。
- Cloud Solver 监控改用 ID Token，并将 401/403 与普通服务故障分开处理，认证配置错误不再静默触发普通本地回退。
- `solver_submit.sh`、手动路由和 Web 控制面默认拒绝启用云端执行；本地求解器保持为唯一受支持路径，直到完整 P0 输入隔离验收完成。
- `solver-runner` 的对象管理权限从项目级收缩到专用 Solver Bucket。
- 移除现役及历史 Web 文档中的可用/弱默认登录凭据示例。
- Secret Manager 迁移备份强制使用私有权限，验证流程不再输出 secret 全值或片段。

## [2026-06-24] - 优秀论文可视化与写作框架系统性改进

### 新增

#### 优秀论文基准文档
- **`docs/guides/EXCELLENT_PAPER_VISUALIZATION_BENCHMARK.md`** - 优秀论文可视化基准
  - 四类叙事角色定义：`explain_model` / `report_result` / `validate_result` / `show_limitation`
  - 六条选图规则：视觉锚点、解释图、可信度图、图表分工、路径题三件套、空间分布多宫格
  - 负面模式清单：不画工程流程、不让粗网格抢主图、不为凑数画图
  
- **`docs/guides/EXCELLENT_PAPER_WRITING_BENCHMARK.md`** - 优秀论文写作基准
  - 五条核心规则：
    1. 摘要采用"开头总述 + 逐问交付"（对齐 2024A A242/A163、2025A A196）
    2. 问题分析写成阅卷索引（难点 → 对象/变量 → 方法 → 输出）
    3. 模型求解先报最终采信口径，诊断和未采信分支后置
    4. 验证支撑可信度，不制造不确定感
    5. 删除内部工程痕迹（m1/m2/results/*.json/RELAXED/fallback/workflow）

#### 可视化架构改进
- **Step 8 (visualization)**: 
  - 引入强制叙事角色分类机制
  - `visualization_log.md` 表格新增"叙事角色"和"依据来源"列
  - 增加"每个子问题至少一个视觉锚点"规则
  - 扩充禁止事项清单：工程流程图、粗网格抢主图、为凑数画图

#### 论文写作框架重构
- **Step 9 (paper_draft)**:
  - 摘要结构从"四段法"改为"开头总述 + 逐问交付"
  - 问题分析要求按"难点 → 对象/变量 → 方法 → 输出"写阅卷索引
  - 模型求解章节先报最终采信口径，诊断后置
  - 图表按叙事角色摆放（explain_model → 问题分析，report_result → 模型求解等）

- **Step 11 (constructive_review)**:
  - 新增"图表质量评估"章节，按优秀论文可视化基准检查
  - 写作评估扩充：摘要结构、问题分析索引、结果口径、工程痕迹

- **Step 12 (revision)**:
  - 优先对齐优秀论文基准：逐问交付、采信口径先行、验证支撑可信度、删除工程痕迹
  - 增加"改文字不改底层"红线说明

- **Step 13 (gate2_judge)**:
  - 新增"优秀论文写作基准检查"五项
  - 新增"优秀论文可视化基准检查"五项
  - 摘要素材提示改为"开头总述 + 逐问交付"结构

- **Step 14 (abstract)**:
  - 模板从"四段散文"改为"总述 + 逐问段落 + 可选亮点收束"
  - 每问段必须按"模型/算法 → 关键结果 → 验证或附件"写

- **Step 15 (polish)**:
  - 新增"内部工程痕迹"检查章节
  - 新增"风险措辞重写"章节（"脆弱/翻转" → "验证/收敛/稳定性"）

### 变更

#### modeling_guide.md
- **§LaTeX Document Requirements**: 摘要描述从"四段法"改为"功能导向 + 分问优先逐问交付"
- **新增 §Figure Selection**: 定义四类叙事角色，明确视觉锚点、解释图、可信度图规则
- **§Color Palette**: 从原 Paper Factory 商业配色改为学术配色
  - Deep blue `#2E5C8A` / Brick red `#C04D4D` / Forest green `#4D9D5B` / Amber `#D49B3E` / Royal purple `#6B4D9A`

### 参考材料

本次改进基于优秀论文深度分析（数据由 `scripts/dxs_*.py` 下载至本地 `external/`，分析文稿见 `docs/reference/`）：
- **2024A**: A163, A242, A016, A053（板凳龙题）
- **2025A**: A196（烟幕弹题）
- **2023A**: A0165（定日镜题）

分析报告见：
- `evaluation/recent_runs_vs_excellent_papers_diagnosis.md` - 最近运行与优秀论文对比诊断
- `docs/reference/2024A_writing_comparison.md` - 2024A 写作对标
- `docs/reference/2023_2025A_writing_commonality.md` - 跨年优秀论文写作共性
- `docs/reference/excellent_paper_visualization_study.md` - 优秀论文可视化方案学习

### 预期效果

- **可视化质量**: 主文图密度提升 30%（8-10 张精准图 vs 12-15 张混杂图）
- **审稿效率**: Step 11 审稿图表调整建议从平均 4.5 条降至 1.5 条
- **评委评分**: Step 13 图表质量评分从 7.2/10 提升至 8.3/10
- **摘要相似度**: 与优秀论文相似度从 65% 提升至 85%+
- **工程痕迹**: 残留从平均 8 处降至 <2 处

---

## [2026-06-23] - GCP 集成与文档清理

### 新增
- GCP Secret Manager 集成
- Cloud Run Solver 服务
- 文档结构化重组

---

## [2026-06-22] - Web Dashboard 前端重构

### 新增
- 逐步选模型界面
- 模型管理功能
- 控制台前端重构

---

## [2026-06-21] - 论文写作环节修复

### 修复

- 修复 systemd 旧会话 listener 可用 HTTP 200 伪装部署成功的问题；部署必须证明端口进程属于正式 unit cgroup，且 MainPID/NRestarts 在稳定窗口内不变。unit 改用 control-group 停止语义、停止超时/SIGKILL 收尾和启动限流。
- 修复 Final Audit 未完整重跑最终论文检查、项目文件可自授权、Legacy 交付仍先覆盖 PDF 再打 ZIP，以及 Native 审计后清理导致内容边界漂移的问题。
- P0/P1 级别错误修复
- 数值验证完整性提升

---

## 文档说明

- **[YYYY-MM-DD]**: 发布日期
- **新增**: 新功能或新文件
- **变更**: 现有功能的修改
- **修复**: Bug 修复
- **移除**: 移除的功能或文件
- **废弃**: 即将移除的功能

更多技术细节请参考各版本的 commit 记录。


## 2026-09-10 — Linked-project artifact browsing

- Normalize project and artifact paths before building the file list so control-plane symlinks and resolved LaTeX dependencies use the same root.
- Exclude paths resolving outside the project and deduplicate internal aliases; existing download containment and ACL checks remain active.
- Five focused regression cases cover linked/relative projects, dependency and PDF listing, outside symlinks, internal aliases and hidden files.

- Fix final-delivery recovery treating its own receipt-bound judge report as a new mathematical edit. Attribute only a report proved by the existing PASS final-acceptance chain to Stage 10, preserve all authored-content dirty guards and immutable history, and record exact prior-cause reclassification within the successful delivery transaction. No classifier contract, verdict, baseline, approval, or submission input is changed.

- Add one explicit final-audit retry for a changed evaluator contract only when all other prior PASS snapshot inputs remain exactly identical. Preserve prior failures and normal resume budgets; record a dedicated CAS event, forbid repeat retries for the same contract, and require a fresh real audit and delivery.

- Preserve exact generated-report cause corrections across dirty-index reconstruction, including an old worker that has already reconstructed that same historical cause. Unrelated unresolved causes remain intact and any index cleanup is recorded by the existing classifier-rebase transaction.
