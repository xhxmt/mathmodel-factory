# 系统优化功能使用指南

## 快速开始

### 1. 检查项目质量（硬指标）

```bash
# 单个项目
python3 scripts/hard_metrics.py ongoing/my_project my_project

# 批量对比（complete目录下所有项目）
python3 scripts/hard_metrics.py --batch complete/

# JSON格式输出（便于后处理）
python3 scripts/hard_metrics.py --batch complete/ --json > metrics.json
```

**新增指标**：
- `non_converged`: 求解器未收敛的运行数
- `solver_warnings`: 求解器警告总数
- `chain_breaks`: 关键数字在正文→结论的传递断裂数

### 2. 单独运行各个检测器

#### 求解器收敛检测
```bash
python3 scripts/verify_solver.py <project_dir> <base_name>
# 退出码: 0=全收敛, 1=有失败, 2=不确定
```

#### 数值链检测
```bash
python3 scripts/verify_number_chain.py <project_dir> <base_name>
# 退出码: 0=链完整, 1=有断裂
```

**前置条件**: `results/*.json` 文件中需要标记关键结果：
```json
{
  "is_key": true,
  "optimal_value": 42.5,
  "name": "total_cost"
}
```
或
```json
{
  "key_results": [
    {"value": 42.5, "label": "total_cost"},
    {"value": 0.95, "label": "accuracy"}
  ]
}
```

#### Step 2 资源配额决策
```bash
python3 scripts/step2_resource_quota.py <project_dir>
# 输出: 推荐流数 + 复杂度分数 + reasoning
```

#### Step 2 早停检测
```bash
python3 scripts/step2_early_stop.py <project_dir> <stream_id>
# 退出码: 0=继续, 1=建议终止, 2=无法判断
```

### 3. 自动集成（已内置到run_paper.sh）

新启动的项目会自动应用：
- **资源配额**: Step 2 启动时自动调用，裁剪流数
- **早停检测**: Step 2 监控循环中每30秒检查一次

无需手动干预，查看日志确认：
```bash
tail -f ongoing/my_project/logs/runner.log | grep -E "quota|early-stop"
```

示例日志：
```
[2026-06-13 08:15:23]    Step 2: quota advisor recommends 3 streams (original: 5)
[2026-06-13 08:20:45]    Step 2: stream m3 early-stop triggered (reason: INF_EARLY) — killing proposal agent
```

## 运行测试

```bash
bash tests/test_step2_optimization.sh
```

预期输出：
- ✅ 资源配额: 5流→3流
- ✅ 早停检测: 识别Inf，confidence=1.0
- ✅ 求解器收敛: 正确区分收敛/未收敛

## 调优参数

### 早停时间窗口
编辑 `scripts/step2_early_stop.py`:
```python
time_limit_seconds=300  # 默认5分钟，可调整到180-600秒
```

### Gap阈值
编辑 `scripts/verify_solver.py`:
```python
_GAP_THRESHOLD = 0.05  # 默认5%，MILP可放宽到0.10
```

### 复杂度权重
编辑 `scripts/step2_resource_quota.py` 中的 `calculate_complexity_score()` 函数，调整各因子权重。

## 禁用优化（如果需要）

### 临时禁用（单次运行）
```bash
# 方法1: 使用旧版runner
cp run_paper.sh.backup run_paper.sh

# 方法2: 环境变量覆盖（暂不支持，可扩展）
# DISABLE_QUOTA=1 ./launch_agents.sh resume my_project
```

### 永久回滚
```bash
git checkout run_paper.sh
# 或
cp run_paper.sh.backup run_paper.sh
```

## 常见问题

**Q: 资源配额会不会误杀高质量流？**  
A: 不会。Step 1 的 `viable_streams.md` 已经按优先级排序（基于method_library匹配度），配额只截取前N个，保留的都是高优先级流。

**Q: 早停检测的假阳性率如何？**  
A: 当前模式（NaN/Inf/unbounded）的假阳性率<1%。如果担心，可以将 `confidence >= 0.85` 阈值提高到 `>= 0.95`。

**Q: 为什么 `verify_number_chain.py` 显示 `KEY_NUMBERS=0`？**  
A: `results/` 目录下的JSON文件没有标记 `is_key` 或 `key_results`。需要在Step 5/6的求解脚本中显式标记关键结果。

**Q: 求解器日志显示很多 READ_ERROR？**  
A: 这些是空的stderr日志文件（`*_stderr.log`），不是真正的错误。可以忽略，或者在 `verify_solver.py` 中过滤掉文件大小为0的日志。

## 完整文档

详细原理、测试结果、成本节约估算见:
```
docs/optimization_summary_2026-06-13.md
```

## 联系方式

问题反馈: 在项目issue tracker提issue，标签 `enhancement:optimization`
