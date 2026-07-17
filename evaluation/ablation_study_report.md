# 消融实验历史观察报告（Ablation Study Report）

> **状态：`LEGACY_UNVERIFIED`，仅作探索性记录。**
>
> 实验生成于 2026-06-05 至 2026-06-09，旧评估完成于 2026-06-10。它只覆盖 CUMCM 2024 B 一道题，每个条件只有一个生成重复（rep1），评分又来自旧合并上下文评委。旧 K=3 调用使用 temperature=0，不能作为独立统计重复。因此本报告不再声称因果贡献、统计显著性、稳定提升或组件优先级。

## 1. 历史实验设计

### 1.1 当时的探索问题

实验分别关闭四个工作流机制，观察旧评分器是否给出不同读数。这只能用于提出后续假设，不能回答“该机制使论文质量提升多少”。

| 环境变量 | 关闭的机制 | 当时关注的维度 |
|---|---|---|
| `ABLATE_NO_CONSULTATION` | Step 1 web 文献检索 | 模型呈现、背景研究 |
| `ABLATE_NO_METHOD_LIB` | HMML-lite 方法库引用硬门 | 模型呈现、方法选择 |
| `ABLATE_NO_JUDGE` | Step 13 Gate 2 + reopen 循环 | 下游修订、整体呈现 |
| `ABLATE_NO_INNOVATION_PROTECT` | `PROTECTED` 不可降级规则 | 创新性、假设保护 |

### 1.2 历史配置

- 基线：`test_cumcm2024b`
- 题目：CUMCM 2024 B
- 生成重复：每个条件 1 次（rep1）
- 旧评估调用：K=3，temperature=0
- 旧分数口径：`median_recomputed`
- 当前兼容状态：所有下列评分均为 `LEGACY_UNVERIFIED`

K=3 只重复了同一评委契约。它没有增加独立生成样本，也没有估计人类评委方差；min/max 区间不是置信区间。

## 2. 历史原始读数

### 2.1 旧总分

| 项目 | 旧总分 | 相对旧基线 Δ | 旧 VERDICT | 当前状态 |
|---|---:|---:|---|---|
| `test_cumcm2024b` | 91.6 | — | PASS | `LEGACY_UNVERIFIED` |
| `cumcm2024b_no_consult_rep1` | 89.9 | -1.7 | PASS | `LEGACY_UNVERIFIED` |
| `cumcm2024b_no_innov_rep1` | 88.9 | -2.7 | PASS | `LEGACY_UNVERIFIED` |
| `cumcm2024b_no_judge_rep1` | 88.2 | -3.4 | PASS | `LEGACY_UNVERIFIED` |
| `cumcm2024b_no_methodlib_rep1` | 85.3 | -6.3 | PASS | `LEGACY_UNVERIFIED` |

这张表只能描述“在这一组旧产物和旧评委调用中，四个 variant 的读数低于旧 baseline”。它不能证明所有机制都有正向贡献，也不能证明 -6.3 与 -1.7 的差异来自相应组件本身。

### 2.2 旧六维读数

| 旧维度 | 基线 | no_consult | no_innov | no_judge | no_methodlib |
|---|---:|---:|---:|---:|---:|
| 模型合理性 (20) | 18.0 | 17.3 | 17.7 | 17.3 | 16.3 |
| 求解正确性 (20) | 19.3 | 19.3 | 18.7 | 19.3 | 18.0 |
| 创新性 (20) | 17.0 | 17.0 | 16.3 | 16.3 | 16.7 |
| 写作清晰度 (15) | 13.3 | 13.3 | 13.7 | 13.3 | 13.0 |
| 结果说服力 (15) | 13.7 | 13.3 | 13.7 | 13.3 | 13.0 |
| 灵敏度分析 (10) | 9.7 | 9.7 | 9.0 | 8.7 | 8.3 |

这些维度属于旧 schema，其中“求解正确性”与当前数学 / 执行硬有效性门存在语义冲突，不能映射到当前“模型呈现 / 求解叙事 / 敏感性与局限”质量轴后继续计算效应。

## 3. 可保留的假设，不是结论

### 3.1 方法库

旧 `no_methodlib` 读数最低，可作为“方法库可能影响方法选择、模型呈现和敏感性覆盖”的待检验假设。也可能由生成随机性、结构预检失败、所选方法不同或旧评委偏好造成。

### 3.2 评委循环

旧 `no_judge` 同时移除了评委判断和评委触发的 reopen，无法区分“没有评审反馈”和“没有二次修订”两个处理效应。后续必须拆成至少两个独立消融。

### 3.3 创新保护

旧 `no_innov` 的创新性维度低 0.7，但单个生成样本不足以判断该差异是否由 `PROTECTED` 规则造成。它只能支持继续设计针对创新假设丢失的可执行检查。

### 3.4 文献检索

旧 `no_consult` 总分差值最小，不能推出文献检索“非瓶颈”或可按题弱化。开放题、数据题和不同年份可能有完全不同的依赖关系。

## 4. 为什么不能做统计推断

| 项目 | 旧中位数 | 旧 min-max spread |
|---|---:|---:|
| baseline | 91.6 | 88.5–92.4 |
| no_consult | 89.9 | 89.9–92.7 |
| no_innov | 88.9 | 88.6–89.1 |
| no_judge | 88.2 | 88.2–88.2 |
| no_methodlib | 85.3 | 85.3–85.3 |

旧报告曾把区间不重叠解释为“统计意义”，该解释现撤回，原因是：

1. 每个条件只有一个论文生成样本，无法估计流水线生成方差；
2. K=3 是同模型、同 prompt、同 packet、temperature=0 的重复调用，不是独立实验单位；
3. 零 spread 可能来自确定性解码或分数锚定，不代表真实不确定性为零；
4. baseline 与 variant 没有跨题、配对 seed 或随机化区组；
5. 旧评委 schema、硬有效性和校准状态不满足当前可比条件；
6. 部分原始 run 为空或失败，历史 JSON 不能保证可从完整原始证据重建。

因此，旧 Δ 值没有置信区间、p 值或可推广的效应量。

## 5. 重做实验的最低契约

### P0：先保证评分有效

- 每个最终论文在 Step 15 后重评，并验证最终提交输入指纹；
- 数学和执行角色均 PASS；任一 FAIL / INDETERMINATE 时质量总分为 `null`；
- 只接受当前 `judge-role-v1` / `judge-aggregate-v1`；
- 校准报告的模型、prompt、schema、packet builder 和输入模态身份匹配；
- 旧结果不参与当前 baseline 或 variant 聚合。

### P1：生成侧重复

- 覆盖多个年份和 A/B/C 不同题型；
- 每题每条件至少 3–5 个独立生成重复；
- baseline / variant 使用配对 seed、相同题目输入和相同资源预算；
- 保存每次生成的提交指纹和完整 provenance。

### P2：评委侧校准

- 使用独立人类盲评和仲裁建立留出真值；
- 报告 pairwise accuracy、Kendall / Spearman、ICC 或 weighted kappa；
- 对硬错误同时报告 sensitivity、specificity 和 precision；
- temperature=0 的 K 次调用只报告一致性，不冒充统计重复；需要不确定性时使用异构评委或明确的随机采样设计。

### P3：因果分析

- 分离 `no_judge` 与 `no_reopen`；
- 将生成方差、题目方差和评委方差分层；
- 报告配对效应量、bootstrap 区间或预注册的混合效应模型；
- 在独立题目留出集上复核后再形成工作流优先级建议。

## 6. 当前结论

本历史实验最多说明四个消融开关曾产生不同的论文产物和旧评委读数。它**不能确认**：

- 所有机制都提高论文质量；
- 方法库是影响最大的机制；
- 评委循环稳定提升 3.4 分；
- 创新保护产生可重复的创新性提升；
- 文献检索影响较小；
- 任一差异具有统计显著性。

这些陈述全部降级为待重做实验验证的假设。当前架构决策应依据硬工作流契约和新的多题、人类校准实验，而不是本页旧分数。

## 附录：历史产物与命令

旧评分卡路径：

- `evaluation/results/test_cumcm2024b_eval_run1.md`
- `evaluation/results/cumcm2024b_no_consult_rep1_eval_run1.md`
- `evaluation/results/cumcm2024b_no_innov_rep1_eval_run1.md`
- `evaluation/results/cumcm2024b_no_judge_rep1_eval_run1.md`
- `evaluation/results/cumcm2024b_no_methodlib_rep1_eval_run1.md`

部分历史 run 为空或只含 stderr；路径存在不代表结果可复现。

```bash
# 仅查看旧数据；当前 compare_ablations.py 应拒绝非 comparison-ready 结果。
python3 experiments/compare_ablations.py \
  --baseline test_cumcm2024b \
  --variant cumcm2024b_no_consult_rep1 \
  --variant cumcm2024b_no_innov_rep1 \
  --variant cumcm2024b_no_judge_rep1 \
  --variant cumcm2024b_no_methodlib_rep1
```

**历史数据来源**：`evaluation/results/*.json`

**当前报告定位**：档案与实验重设计依据，不是质量证明。
