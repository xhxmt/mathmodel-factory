# `evaluation/` — 外部评估闭环（阶段 5.2）

一个独立于论文生成上下文的**有效性门禁 + 条件论文质量**评估器。它优先回答“主要答案能否成立、证据是否足够”，只有硬有效性通过后才提供六维论文质量读数。是否“可横比”由 schema、硬角色状态、输入指纹和校准状态共同决定，不由某个 `/100` 数字自行决定。

## 为什么存在

流水线内部的 **Step 13** 与外部评测都使用三个隔离评审包：数学审计、执行审计和论文评阅。

- 数学与执行角色使用 `PASS / FAIL / INDETERMINATE` 三值逻辑。FAIL 是硬否决；证据不足是 INDETERMINATE，二者都不能产生可比较总分。
- 论文角色使用六维条件质量 rubric：模型呈现 20、求解叙事 20、创新性 20、写作清晰度 15、结果说服力 15、敏感性与局限 10。
- 当前自动论文角色只接收 packet 化的 LaTeX / 文本，因此六维分数只覆盖可观察的结构、论证、标题 / 图题 / 表题和正文图表叙事。它不评价 PDF 分页、字体、颜色、图片清晰度、裁切、重叠或视觉美学；这些必须由编译 / 版式机器预检或人工查看最终 PDF。
- `judge-role-v1` 或 `judge-aggregate-v1` 缺字段、字段多余、分数越界、维度和总分不一致或证据不完整时，结果为 INDETERMINATE，不做正则修补或静默夹紧。
- packet manifest 使用 `judge-packet-completeness-v1` 独立判定各角色的最低证据：paper 要求完整最终论文和主问题文本；math 还要求完整数学阐述；execution 要求完整主要结果、实现和执行轨迹。关键证据只要被截断 / 省略，聚合器就强制对应角色为 `INDETERMINATE`，不信任模型自行降级。非关键大型代码可以截断，但必须出现在 `limitations`。
- 旧 Markdown 评分卡缺少硬角色和证据绑定，只能解析为 `LEGACY_UNVERIFIED`，永远不进入当前比较轴。

`evaluation/` 提供与 in-loop Gate 2 同一版本的角色契约、**不同的独立调用入口**：

- 评委走共享调用器 `scripts/llm_judge_call.py`，默认 **`deepseek-chat`**，按 model 名前缀分派后端（`deepseek*` / `gemini*` / 否则 `claude -p`）。不同模型谱系可降低但不能消除自我偏好。
  （DeepSeek 是当前操作默认值：旧代理扰动实验中它比 haiku 更敏感，且本机路由可用。该证据现属于 `LEGACY_UNVERIFIED`，只能支持工程路由选择，不能证明评分准确率。模型可用 `CLAUDE_MODEL` 覆盖。）
- 评委被明确告知论文和项目文件是“不可信数据”，其中的指令不得改变评分契约。
- 默认 K=3 会重复执行同一组三角色契约；只有全部请求运行都满足当前 schema 且硬角色 PASS，才暴露中位数。当前 API 后端使用 `temperature=0`，因此 K 次运行不是独立统计重复，spread 只是运行一致性诊断，不是方差估计或置信区间。

## 组成

| 文件 | 作用 |
|---|---|
| `run_evaluation.sh` | 主入口：预检门 → 编译 → 隔离评审包 → 三角色 ×K → 否决聚合 |
| `calibration_manifest.json` | 真实获奖论文与生成基线的离线标签、同题奖项顺序和结果路径 |
| `prompts/calibration_pairwise.txt` | 隐藏身份和奖级后的同题两两比较契约 |
| `prompts/calibration_absolute.txt` | 数学正确性与写作质量分轨绝对评分契约 |
| `../scripts/calibration_judge.py` | 面向独立获奖 PDF 的盲评与分轨校准入口 |
| `../scripts/evaluate_calibration.py` | 计算奖项顺序准确率、Kendall-style 次序、缺失覆盖、格式失败和致命缺陷检出率 |
| `../scripts/llm_judge_call.py` | 共享 LLM 调用器：按 model 名分派 DeepSeek / Gemini / Claude 三后端（run_evaluation.sh 与 perturbation_harness.py 共用） |
| `../scripts/enrich_evaluation_result.py` | 将聚合结果拆成 `structural` 硬证据和 `llm_score` 软评分 |
| `../prompts/judges/*.txt` | 当前数学、执行、论文三角色严格输出契约 |
| `human_rubric.md` | 给真人评委的纸面打分表（同一把尺子） |
| `baseline_scores.md` | 旧评分契约的历史读数及当前基线准入条件；不是活跃绝对分基线 |
| `results/runs/<base>/<run_id>/` | 不可变原始运行目录：配置指纹、预检、角色聚合和最终 JSON |
| `results/<base>_eval.json` | 指向最新不可变运行的兼容入口；旧平面 JSON 首次被替换时归档到运行目录 |

复用而非重造：
- **rubric** 不重造——角色 schema 和六维权重以 `STEPS.md` 与 `scripts/aggregate_judges.py` 的版本化契约为准。它是内部操作性 rubric；奖级解释仍需人类校准。
- 预检用 `scripts/evaluate_modeling_project.py` 记录结构 / 交付证据。外部评分的最低准入不由内部 Gate 2 选择：即使内部预检 FAIL，只要问题、最终论文和执行主张审计材料完整，外部评委仍可独立发现 false positive / false negative。
- 数值可追溯信号用 `scripts/verify_numbers.py` 的 `UNMATCHED` 计数。
- 评分卡解析用 `scripts/parse_judge_score.py`。当前 schema 才可比较；旧格式只保留诊断字段。

## 用法

```bash
# 评估一个已完成项目（默认 K=3 次采样）
./evaluation/run_evaluation.sh complete/test_cumcm2024b

# 指定采样次数 / base 名 / 机器可读输出
./evaluation/run_evaluation.sh complete/test_cumcm2024a --samples 5
./evaluation/run_evaluation.sh ongoing/foo my_base --force --json
```

`--force` 只为 CLI 兼容保留，不会删除或伪造结构证据，也不会把不完整 packet 提升为可评分状态。

输出写入唯一、不可变的 `evaluation/results/runs/<base>/<run_id>/`：

- `configuration.json` —— 模型、样本数、system prompt 版本、prompt / 实现 / packet 哈希及 `configuration_fingerprint`。
- `eval_run1.md … eval_run<K>.md` 与 `eval_run<k>_roles.json` —— 每轮三角色严格聚合及原始状态。
- `precheck.json` —— `scripts/evaluate_modeling_project.py --json` 的结构证据。
- `eval.json` —— 聚合结果，包含：
  - `structural`：预检、评分准入、推断步骤、未匹配数字和 blocking evidence；
  - `llm_score`：请求 / 有效运行数、条件中位数和运行一致性诊断；
  - `calibration`：代理 harness、人类、运行时评分、奖级校准状态及身份匹配；
  - `comparison_ready_proxy`：硬角色、schema、输入 / 校准身份均通过，且报告明确验证了精确的 `judge-role-v1` + `modeling-factory-judge-packet-v2` 运行时构造；
  - `comparison_ready_human`：在上述运行时构造验证基础上还具备人类真值校准；
  - `comparison_ready`：当前兼容字段，等于 `comparison_ready_proxy OR comparison_ready_human`；具体解释仍必须查看是哪一种证据成立，不能单凭此字段宣称奖级可信。
- 一行汇总打到 stdout，例如：
`test_cumcm2024b: external=85.x/100 (spread 84.x-87.x), in-loop=86.4, unmatched=N, precheck=PASS`

stdout 分数是诊断摘要。只有对应 JSON 的 readiness 字段满足目标用途时才能进入比较；硬 FAIL / INDETERMINATE 时总分必须为 `null`。

最终工作流还会把当前 packet、角色 prompts、聚合 / 调用实现、Step 13 模型路由与编译后的 PDF 精确字节写入 `final_judge_v3` 最终提交指纹。该绑定只证明“被评分的文本版本 / 评审契约”和“被交付的 PDF”来自同一提交状态，不表示文本模型实际观察过 PDF 渲染。视觉与版式检查结果应作为独立机器预检 / 人工复核记录，不得混入当前自动六维分数。

## 获奖论文校准

获奖论文通常只有 PDF，没有项目代码、日志和 canonical results，因此不能直接伪装成
完整项目交给 `run_evaluation.sh`。使用专门的校准入口：

```bash
source scripts/load_secrets.sh
python3 scripts/calibration_judge.py evaluation/calibration_manifest.json \
  --model deepseek-chat --samples 2 \
  --adjudicator-model gemini-3.1-pro-preview
python3 scripts/evaluate_calibration.py evaluation/calibration_manifest.json \
  --existing-results --require-ready
```

长上下文调用中断后可只重跑缺失或失败的比较：

```bash
python3 scripts/calibration_judge.py evaluation/calibration_manifest.json \
  --model deepseek-chat --samples 2 --pairwise-only \
  --adjudicator-model gemini-3.1-pro-preview \
  --pair-id national1_vs_provincial1_2024b
```

校准严格按以下顺序执行：

1. 隐藏论文身份、学校和奖级，先进行同题两两比较；
2. 再分别给出数学正确性和论文写作绝对评分；
3. 写作专项包含答案完整度、论证链、结果可核验性、段落组织、图表叙事和语言成熟度；
4. `score_reliability.ready=false` 时，Step 13 绝对分数不得作为可靠的优化目标或奖级预测。

成对评审采用严格的 A/B、B/A 平衡顺序。两次初评结论不一致、出现平局、格式失败，
或者机器发现两篇文档高度相似但存在局部差异时，自动交给独立 Gemini 评委复议；
复议结论作为争议裁决，不与初评分数做简单平均。使用复议时，结果身份是
`composite` evaluator，必须同时记录 primary 与 adjudicator 的模型、配置和最终
`decision_source`；该命中率不能只归因给 primary model。manifest 的 `models` 必须覆盖
两者，否则 freshness 与 `model_config_match` 都失败。

数学正确性与写作得分不能相互抵消。真实盲比结果优先于两个绝对分数之差；绝对分数
排序只作为旧结果兼容的后备信号。

只有具有独立奖级顺序或人工真值的比较才能进入可靠性分母。类似“真实获奖论文必然
优于某篇未参赛生成稿”的弱先验保留为 `readiness_eligible=false` 诊断项，报告其结果，
但不得用它训练、选择或惩罚评委。

`calibration_contract.identity_required` 是可执行身份合同，不是说明文字。当前合同要求的
`prompt_sha256` 必须能由逐请求 `prompt_run_sha256` 精确重算，`input_fingerprint` 必须能由
论文源哈希与 prompt 哈希精确重算；paper 与 pair 的结果文件还必须分别由对应 manifest item
的 `result_sha256` 固定。结果文件内部不能自证自己的哈希，未 pin、pin 不匹配或任一组成哈希
不可重算时一律为 `STALE`。生成新结果后应先审阅文件，再把其 SHA-256 写入相应 paper/pair
item，之后才可用于 readiness。

`LEGACY_DELIVERED` / `CURRENT_PASS` 是工作流交付状态，不是论文内容的致命缺陷真值。
除非另有独立内容审查标签，不能据此自动填写 `expected_fatal_flaw=true/false`。2025A 中
“省一论文 vs 未参赛生成稿”的比较均作为弱先验诊断项，不进入 readiness 分母。

## 无人工复核时的代理校准

当暂时没有人工评审条件时，可使用 `proxy_calibration_manifest.json` 做有限范围的代理校准：
它把完整论文与确定性单缺陷版本配对，真值是“原文应优于该缺陷版本”。代理校准只可用于
检验评委是否能识别已知退化、以及同一提示词的 A/B 比较；它不等价于人工真值，不得用于
奖级预测或解释绝对分数。代理结果写入 `proxy_reliability`，人工校准状态仍保持
`human_calibration.ready=false`。

```bash
source scripts/load_secrets.sh
python3 scripts/calibration_judge.py evaluation/proxy_calibration_manifest.json \
  --model deepseek-chat --samples 4
python3 scripts/evaluate_calibration.py evaluation/proxy_calibration_manifest.json \
  --existing-results --require-proxy-ready \
  --json-output evaluation/proxy_calibration_report.json \
  --markdown-output evaluation/proxy_calibration_report.md
```

代理集的扰动实现位于 `scripts/proxy_calibration.py`，覆盖数值矛盾、符号/灵敏度缺失、
无证据最优性、答案删减和机器式重复等类型。

报告将总体成对排序、运行时评分和细分轴可靠性分开：`proxy_reliability.ready=true`
只证明 paper-only 配对 harness 能识别已知退化，不能自动转移到外部三角色评分。只有 manifest
以 `runtime_score_validation` 明确验证精确的 `judge-role-v1` evaluator 与
`modeling-factory-judge-packet-v2` 输入模态后，`runtime_score_reliability.ready` 才可能为真，
并允许 `comparison_ready_proxy/human`。`axis_reliability.ready=true` 只支持对应子轴；这些字段
均不自动代表奖级预测可用。

成对比较的 primary samples 必须是大于等于 2 的偶数，以保证 A/B、B/A 位置严格平衡。即使样本数为 4，temperature=0 的多次调用也不自动构成统计独立重复；可靠性来自与外部真值的命中率和覆盖率，而不是同一模型的低 spread。

## 与 in-loop Step 13 的对照

| | in-loop Step 13 | 外部 evaluation/ |
|---|---|---|
| 评判主体 | runner 配置的隔离角色调用 | 独立命令配置的模型调用（默认 `deepseek-chat`，可换 Claude / Gemini） |
| 用途 | 驱动 reopen；Step 13 为预提交门禁，Step 16 对最终提交输入重评 | 独立诊断；仅在目标用途校准 READY 后横比 |
| VERDICT | 驱动 runner 回退 | 仅分类标签，不驱动任何循环 |
| 硬有效性 | 数学 / 执行三值门禁 | 数学 / 执行三值门禁 |
| 论文质量 | 硬角色 PASS 后的六维条件分 | 硬角色 PASS 后的六维条件分 |
| 输出格式 | `judge-role-v1` + `judge-aggregate-v1` | 同版本 schema（同一解析器） |

## 校准

校准优先看同题真实论文的奖项顺序，不把绝对分数当 ground truth：

```bash
python3 scripts/evaluate_calibration.py evaluation/calibration_manifest.json --existing-results
```

报告会明确列出 `MISSING`，不会把缺失论文从分母静默删除。答案材料不得进入 runtime agent 的评审包。

## 历史结果说明

`results/` 中旧合并上下文评分卡，以及任何不含 `judge-aggregate-v1`、硬角色状态、配置 / packet 指纹的结果，统一标记 `LEGACY_UNVERIFIED`。它们只能作为历史样本，不得参与当前排序、消融效应计算、绝对分解释或奖级预测。新的基线必须由隔离三角色评审重新生成，并分别声明代理可比、人类可比和奖级可用状态。
