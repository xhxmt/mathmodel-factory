# 消融实验状态总览

> **最后更新**: 2026-06-13  
> **实验阶段**: 第一轮完成 (CUMCM 2024 B题)

---

## 📊 实验完成度

### 已完成项目 (6/6)

| 项目 | 类型 | PDF | 提交包 | 评估 | 总分 | 状态 |
|---|---|:---:|:---:|:---:|---:|---|
| test_cumcm2024a | 基准 | ✓ | ✓ | ✓ | 66.3 | ✅ 完成 |
| test_cumcm2024b | 基准 | ✓ | ✓ | ✓ | **91.6** | ✅ 完成 (消融基线) |
| cumcm2024b_no_consult_rep1 | 消融 | ✓ | ✓ | ✓ | 89.9 | ✅ 完成 |
| cumcm2024b_no_innov_rep1 | 消融 | ✓ | ✓ | ✓ | 88.9 | ✅ 完成 |
| cumcm2024b_no_judge_rep1 | 消融 | ✓ | ✓ | ✓ | 88.2 | ✅ 完成 |
| cumcm2024b_no_methodlib_rep1 | 消融 | ✓ | ✓ | ✓ | 85.3 | ✅ 完成 |

**进度**: 6/6 (100%)

---

## 📈 关键发现速览

### 消融影响排序 (从大到小)

1. **🔴 方法库 (no_methodlib)**: -6.3分 (-6.9%) — **基础设施级影响**
2. **🟠 评委循环 (no_judge)**: -3.4分 (-3.7%) — 质量门禁
3. **🟡 创新保护 (no_innov)**: -2.7分 (-2.9%) — 符合预期
4. **🟢 文献检索 (no_consult)**: -1.7分 (-1.9%) — 非瓶颈但有贡献

### 维度影响矩阵

| 消融条件 | 最伤维度 | 降幅 | 次伤维度 | 降幅 |
|---|---|---:|---|---:|
| no_methodlib | 模型合理性 | -1.7 | 灵敏度分析 | -1.4 |
| no_judge | 灵敏度分析 | -1.0 | 创新性/模型合理性 | -0.7 |
| no_innov | 创新性 | -0.7 | 灵敏度/求解正确性 | -0.7/-0.6 |
| no_consult | 模型合理性 | -0.7 | 结果说服力 | -0.4 |

---

## 📂 数据资产

### 评估结果文件

```
evaluation/results/
├── test_cumcm2024a_eval.json           (66.3/100, K=3)
├── test_cumcm2024b_eval.json           (91.6/100, K=3, baseline)
├── cumcm2024b_no_consult_rep1_eval.json    (89.9/100)
├── cumcm2024b_no_innov_rep1_eval.json      (88.9/100)
├── cumcm2024b_no_judge_rep1_eval.json      (88.2/100)
└── cumcm2024b_no_methodlib_rep1_eval.json  (85.3/100)
```

### 生成的论文

```
papers/
├── test_cumcm2024a_paper.pdf + _submission.zip
├── test_cumcm2024b_paper.pdf + _submission.zip
├── cumcm2024b_no_consult_rep1_paper.pdf + _submission.zip
├── cumcm2024b_no_innov_rep1_paper.pdf + _submission.zip
├── cumcm2024b_no_judge_rep1_paper.pdf + _submission.zip
└── cumcm2024b_no_methodlib_rep1_paper.pdf + _submission.zip
```

### 分析报告

- **综合报告**: `evaluation/ablation_study_report.md` ← **主文档**
- **基线校准**: `evaluation/baseline_scores.md`
- **实验设计**: `experiments/README.md`

---

## 🎯 后续工作优先级

### P0 — 立即可做 (本周)

- [ ] **在 CUMCM 2024 A题重复消融实验**
  - 命令: `./experiments/ablation_no_*.sh --problem A --reps 1`
  - 目的: 验证结论跨题稳定性
  - 预期工作量: 4个消融 × 1次 × ~2小时/次 = 8小时

- [ ] **检查 no_methodlib 项目的实际行为**
  - 读取 `complete/cumcm2024b_no_methodlib_rep1/method_decision.md`
  - 确认: 是否真的选择了非注册方法？还是绕过了引用检查？
  - 目的: 理解 -6.3分降幅的根本原因

- [ ] **提取失败模式案例**
  - 对比 baseline 和 no_methodlib 的 `model.md`、`assumption_ledger.md`
  - 找出具体的质量劣化点 (符号混乱？假设缺失？)
  - 目的: 为论文提供定性证据

### P1 — 短期补充 (下周)

- [ ] **增加重复次数到 rep3**
  - 目前每个消融仅 1 次，无法量化流水线随机性
  - 在 B题上补跑 rep2 和 rep3: `./experiments/ablation_no_*.sh --problem B --reps 2 --baseline`
  - 预期工作量: 4消融 × 2次 × 2小时 = 16小时

- [ ] **尝试第二评委交叉验证**
  ```bash
  CLAUDE_MODEL=gemini-2.0-flash-thinking-exp \
    ./evaluation/run_evaluation.sh complete/cumcm2024b_no_methodlib_rep1 --samples 3
  ```
  - 目的: 验证 DeepSeek 读数是否有系统性偏置
  - 成本: ~3-5分钟/项目

- [ ] **生成可视化图表**
  - 消融影响柱状图 (总分 + 六维度)
  - 方差散点图 (评估稳定性)
  - 维度雷达图 (baseline vs 各消融)

### P2 — 中期扩展 (本月)

- [ ] **设计 judge-only 消融**
  - 当前 no_judge 同时关闭评委和 reopen，混淆效应
  - 新开关: `ABLATE_JUDGE_NO_REOPEN` (保留评委但跳过 Step 12 回退)
  - 目的: 分离"评委质量"和"迭代次数"两个变量

- [ ] **在 C题或 D题验证**
  - 如果 A/B 题结论一致，扩展到不同问题类型
  - C题 (优化类) vs D题 (数据分析类) 可能显示不同的机制重要性

- [ ] **二阶交互消融**
  - 测试 no_judge + no_methodlib 组合
  - 问题: 两个机制叠加是线性 (-6.3-3.4=-9.7) 还是超线性 (>10分) 劣化？

### P3 — 长期研究 (下月+)

- [ ] **人类评委盲测**
  - 招募 3-5 名有 CUMCM 经验的人类评委
  - 对 6 个项目进行盲评，使用同样的 rubric
  - 目的: 校准 LLM 评委的绝对分数和序

- [ ] **获奖论文基线**
  - 获取 CUMCM 2024 A/B 题的获奖论文 (一等奖/二等奖)
  - 用同一评委评分，建立"人类上限"参考点
  - 法律: 确保使用合规 (已公开/授权)

- [ ] **撰写研究论文**
  - 章节: Introduction → System Design → **Ablation Study** ← 本实验
  - 投稿目标: NeurIPS (Workshop on LLM Agents) / AAAI / IJCAI
  - 素材已齐: 系统架构 + 定量验证 + 案例分析

---

## 🔧 技术债务

### 已知问题

1. **DeepSeek 总分锚定**
   - 现象: 手写总分几乎恒定在 68.2±0.2，与六维分数脱钩
   - 对策: 已切换到 `median_recomputed` 口径 (夹紧后六维加权和)
   - 状态: ✅ 已规避，但需在论文中披露

2. **评估 stderr 日志有错**
   - 现象: 部分 `*_eval_run*.stderr.log` 显示超时或 API 错误
   - 影响: 某些项目有 8 次 run 记录但仅 1-3 次成功
   - 对策: 评估时已取中位数，噪声被过滤
   - 状态: ⚠️ 需排查 `scripts/llm_judge_call.py` 的重试逻辑

3. **no_judge 混淆效应**
   - 问题: 无法区分"无评委"和"无二次修订"
   - 对策: 在报告中明确披露，P2 设计 judge-only 消融
   - 状态: 📝 已记录，待改进

### 清理建议

```bash
# 清理空评估文件 (可选，不影响分析)
find evaluation/results -name "*.md" -size 0 -delete

# 归档 DeepSeek 和 haiku 的对照日志
mkdir -p evaluation/results/archive/deepseek_baseline
mv evaluation/results/*_eval.deepseek.json evaluation/results/archive/deepseek_baseline/
```

---

## 📊 数据统计

### Token 消耗估算

| 阶段 | 项目数 | 单价 (万 tokens) | 总计 (万 tokens) |
|---|---:|---:|---:|
| 流水线生成 (Step 0-16) | 6 | ~50-80 | ~360-480 |
| 外部评估 (K=3×6) | 18 | ~5-8 | ~90-144 |
| **总计** | — | — | **~450-624** |

### 运行时间

| 阶段 | 单项耗时 | 总计 |
|---|---:|---:|
| 流水线 (Step 0-16) | ~1.5-3 小时 | ~9-18 小时 |
| 评估 (per run) | ~3-5 分钟 | ~54-90 分钟 |
| **总计** | — | **~11-20 小时** |

---

## 🔗 快速链接

- **综合报告**: [ablation_study_report.md](ablation_study_report.md)
- **基线分数**: [baseline_scores.md](baseline_scores.md)
- **实验脚本**: [../experiments/](../experiments/)
- **评估工具**: [run_evaluation.sh](run_evaluation.sh)
- **对比脚本**: [../experiments/compare_ablations.py](../experiments/compare_ablations.py)

---

## 📝 变更日志

### 2026-06-13
- ✅ 完成第一轮消融实验 (CUMCM 2024 B题, 4消融 × 1次)
- ✅ 生成综合分析报告 (`ablation_study_report.md`)
- ✅ 完成数据完整性检查 (6/6 项目完整)
- 📝 规划后续工作路线图 (P0-P3)

### 2026-06-10
- ✅ 完成所有项目的外部评估 (DeepSeek, K=3)
- ✅ 验证评分序保持: B题 (91.6) > A题 (66.3)

### 2026-06-05 — 2026-06-09
- ✅ 运行 4 个消融实验 + 2 个基准项目
- ✅ 生成 6 份完整论文 PDF + 提交包

---

**负责人**: Paper Factory Team  
**联系方式**: (内部项目)  
**仓库**: `paper_factory/` (本地)
