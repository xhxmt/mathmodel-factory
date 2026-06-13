# 方法库智能化使用指南

## 快速开始

### 1. 初始化（首次使用）

```bash
# 从历史项目学习
python3 scripts/method_fit_score.py --learn complete/
# 生成: scripts/method_fit_model.json

# 构建反例库
python3 scripts/method_antipatterns.py --build complete/
# 生成: scripts/method_antipatterns.json
```

**触发时机**: 
- 首次部署系统
- 每增加5-10个完成项目后重新学习
- 可通过cron每周自动执行

---

### 2. 方法推荐（Step 1使用）

```bash
# 为新项目推荐最适配的方法
python3 scripts/method_fit_score.py ongoing/my_project

# 输出示例:
{
  "top_recommendations": [
    {
      "method": "MILP",
      "name_zh": "混合整数线性规划",
      "path": "method_library/optimization/milp.md",
      "fit_score": 75.5,
      "confidence": 0.8,
      "reason": "based_on_5_features"
    },
    ...
  ]
}
```

**如何使用推荐结果**:
- Step 1 agent在生成 `candidate_methods.md` 时参考推荐列表
- 优先考虑 `fit_score > 60 且 confidence > 0.5` 的方法
- 低置信度推荐仅作参考，不强制

---

### 3. 反例检查（Step 2使用）

```bash
# 检查候选方法是否有历史失败记录
python3 scripts/method_antipatterns.py --check ongoing/my_project \
    method_library/optimization/milp.md

# 输出示例:
{
  "has_antipattern": true,
  "matches": [
    {
      "pattern_id": "ap_006",
      "match_ratio": 0.83,
      "severity": 0.65,
      "failure_reason": "solver_non_converged_2",
      "evidence_projects": ["cumcm2024b_rep1"]
    }
  ],
  "max_severity": 0.65
}

# 退出码: 0=无反例, 1=有反例
```

**决策规则**:
- `severity >= 0.8`: **阻止**使用该方法（高危）
- `severity 0.5-0.7`: **警告**，提示critic特别关注
- `severity < 0.5`: 仅记录，不影响决策

**集成到工作流**:
```bash
# 在Step 2 proposal阶段，自动检查
for method in $(extract_methods_from_spec); do
    if python3 scripts/method_antipatterns.py --check "$PROJECT" "$method"; then
        echo "✓ $method: no antipattern"
    else
        echo "⚠ $method: has antipattern, review carefully"
    fi
done
```

---

### 4. 方法库更新影响分析

```bash
# 场景：开发者修改了method_library/optimization/milp.md

# 1. 检测变更影响
python3 scripts/method_library_update.py --diff HEAD~1 HEAD

# 输出示例:
{
  "changed_methods": [
    {
      "path": "method_library/optimization/milp.md",
      "change_type": "modified",
      "diff_summary": "+15/-8 lines"
    }
  ],
  "affected_projects": [
    {
      "project_name": "cumcm2024b_rep1",
      "uses_method": "method_library/optimization/milp.md",
      "usage_type": "primary"
    },
    ...
  ],
  "recommendations": [
    "Method change affects 3 projects as PRIMARY - high priority re-validation"
  ]
}

# 2. 自动重新验证受影响项目（可选）
python3 scripts/method_library_update.py --validate \
    method_library/optimization/milp.md complete/

# 输出: 验证结果汇总（通过/失败）
```

**推荐工作流**:
1. 修改方法库文件前，先 `git diff` 检查当前未提交变更
2. 提交后运行 `--diff HEAD~1 HEAD` 查看影响
3. 如果影响 PRIMARY 项目，运行 `--validate` 确保兼容性
4. 可选：添加git pre-commit hook自动化

---

## 高级用法

### 定期重新学习

```bash
# 添加到cron（每周日凌晨2点）
0 2 * * 0 cd /path/to/paper_factory && \
    python3 scripts/method_fit_score.py --learn complete/ && \
    python3 scripts/method_antipatterns.py --build complete/
```

### 批量推荐

```bash
# 为多个项目生成推荐报告
for project in ongoing/*/; do
    base=$(basename "$project")
    python3 scripts/method_fit_score.py "$project" > "reports/${base}_recommendations.json"
done
```

### 反例严重度调整

编辑 `scripts/method_antipatterns.py`:
```python
# 调整严重度权重
if is_fail:
    severity = 0.9  # 原0.8，提高到0.9（更严格）
elif abandoned:
    severity = 0.6  # 原0.5，提高到0.6
```

### 方法库CI集成

在 `.github/workflows/method_library_check.yml`:
```yaml
name: Method Library Change Check

on:
  push:
    paths:
      - 'method_library/**'

jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
        with:
          fetch-depth: 2
      
      - name: Analyze impact
        run: |
          python3 scripts/method_library_update.py --diff HEAD~1 HEAD > impact.json
          cat impact.json
          
      - name: Validate affected projects
        if: contains(fromJson('impact.json').summary.affected_project_count, '1')
        run: |
          # 提取变更的方法路径
          method=$(jq -r '.changed_methods[0].path' impact.json)
          python3 scripts/method_library_update.py --validate "$method" complete/
```

---

## 常见问题

**Q: 推荐结果的置信度很低（<0.3），是否可信？**  
A: 低置信度说明历史样本不足。可以参考，但不应强制采纳。随着项目积累，置信度会提升。

**Q: 反例检查报了false positive怎么办？**  
A: 检查 `evidence_projects`，如果证据项目确实有特殊性（如数据异常），可以手动降低该反例的severity。

**Q: 方法库更新后，是否必须重新验证所有项目？**  
A: 不必须。优先验证 PRIMARY 使用该方法的项目。AUXILIARY 使用的可以降低优先级。

**Q: 如何添加新的问题特征？**  
A: 编辑 `extract_problem_features()` 函数，添加新的bool/categorical特征。重新学习后即生效。

**Q: 反例库会随时间增长很大吗？**  
A: 会。但相同模式会合并，且可以定期清理 `occurrence_count < 2` 的低频反例。

---

## 性能基准

| 操作                  | 6个项目  | 50个项目（预估） | 200个项目（预估） |
|----------------------|---------|----------------|------------------|
| 学习时间              | <5秒    | ~20秒          | ~60秒            |
| 推荐查询              | <1秒    | <1秒           | <2秒             |
| 反例检查              | <0.5秒  | <0.5秒         | <1秒             |
| 影响分析              | <2秒    | ~5秒           | ~15秒            |

---

## 数据文件说明

### `method_fit_model.json`
- **大小**: 每个项目约8KB，6个项目~50KB
- **更新频率**: 每增加5-10个项目重新学习一次
- **备份**: 建议纳入版本控制（gitignore例外）

### `method_antipatterns.json`
- **大小**: 每个反例约2KB，10个反例~20KB
- **更新频率**: 与fit_model同步
- **备份**: 同上

### 存储策略
```bash
# 纳入版本控制（可选）
git add scripts/method_fit_model.json scripts/method_antipatterns.json
git commit -m "chore: update method intelligence models"

# 或使用LFS（项目数>100时）
git lfs track "scripts/method_*.json"
```

---

## 故障排查

### 错误: "模型文件不存在"
```bash
# 解决：运行学习
python3 scripts/method_fit_score.py --learn complete/
```

### 错误: "method_library/index.json 不存在"
```bash
# 解决：确认method_library结构完整
ls method_library/index.json
```

### 推荐结果全是"no_history"
```bash
# 原因：样本太少或特征提取失败
# 检查：
python3 -c "import json; print(json.load(open('scripts/method_fit_model.json'))['summary'])"

# 如果total_projects=0，说明没有提取到有效样本
# 检查complete/下的项目是否有chosen_method.md
```

---

## 联系方式

问题反馈: 在项目issue tracker提issue，标签 `enhancement:method-library`

文档更新: `docs/method_library_intelligence_summary.md`
