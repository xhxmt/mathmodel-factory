# 无人工真值的评委能力校准与 Shadow 准入

`scripts/capability_harness.py` 和 `scripts/shadow_cutover.py` 实现 P2/P3 的离线核心。
它们不调用模型、不修改生产路由，也不把模型共识当成真值。

能力报告只回答：指定的 `model + backend + prompt hash + schema hash` 在一组具有机器
oracle 的、实际 judge packet 模态上，能否检出指定 mutation family，并对中性变换保持
稳定。它不能回答论文对应什么奖级，也不能证明 `/100` 分数与人工评委一致。

## 1. 准备 mutation packet

manifest 使用 `judge-capability-manifest-v1`：

```json
{
  "schema": "judge-capability-manifest-v1",
  "evaluator": {
    "model": "model-name",
    "backend": "backend-name",
    "prompt_path": "runtime/math_prompt.txt",
    "schema_path": "runtime/judge_role_schema.json",
    "prompt_sha256": "<64 lowercase hex>",
    "schema_sha256": "<64 lowercase hex>"
  },
  "holdout_axes": ["project_id", "problem_id", "mutation_family"],
  "cases": [
    {
      "id": "p1-unit-error",
      "project_id": "p1",
      "problem_id": "2025A",
      "mutation_family": "unit_error",
      "role": "math",
      "kind": "hard_defect",
      "split": "test",
      "source_packet": "packets/p1/math",
      "mutation": {
        "type": "text_replace",
        "path": "context.txt",
        "old": "10 m/s",
        "new": "10 km/s",
        "count": 1
      },
      "oracles": {
        "preconditions": [
          {"type": "text_contains", "path": "context.txt", "value": "10 m/s"}
        ],
        "postconditions": [
          {"type": "text_contains", "path": "context.txt", "value": "10 km/s"},
          {"type": "text_not_contains", "path": "context.txt", "value": "10 m/s"}
        ]
      }
    }
  ]
}
```

`prompt_path` 与 `schema_path` 相对 manifest 所在目录解析；prepare 会重算文件 hash，声明
值与当前字节不一致时立即拒绝，因此旧 prompt/schema 结果不能冒充当前 runtime。

支持的 mutation 为 `delete_file`、`text_replace`、`text_append`、`json_set`、
`json_delete`、`json_reorder_keys`、`normalize_whitespace` 和 `sort_lines`。这些原语可表达
单位/符号替换、约束或 replay evidence 删除、结果/provenance 篡改、无证据主张注入、答案
删减，以及 JSON/空白/无关行序变化。所有 case 必须同时有非空的 precondition 与
postcondition oracle。`neutral_transform` 还必须带
`json_semantically_equal_to_source`、`whitespace_equivalent_to_source`、
`line_multiset_equal_to_source` 或 `redaction_equivalent_to_source`，否则拒绝进入分母。

```bash
python3 scripts/capability_harness.py prepare evaluation/capability_manifest.json \
  --output-dir evaluation/capability_runs/run-001
```

输出 packet 的 tree hash 会写入每个 case 的 exact runtime identity。之后必须使用该 packet、
相同 prompt/schema 和声明的实际 model/backend 运行评委。

## 2. 写入绑定观测并生成能力报告

每个 prepared case 默认对应 `observations/<case-id>.json`：

```json
{
  "schema": "judge-capability-observation-v1",
  "case_id": "p1-unit-error",
  "runtime_identity": {
    "model": "model-name",
    "backend": "backend-name",
    "prompt_sha256": "<prepared manifest 中的值>",
    "schema_sha256": "<prepared manifest 中的值>",
    "packet_sha256": "<prepared manifest 中的值>"
  },
  "decision": "FAIL",
  "findings": [
    {
      "mutation_family": "unit_error",
      "ref_id": "ref-1"
    }
  ],
  "grounding_receipt": {
    "path": "grounding/p1-unit-error.json",
    "sha256": "<receipt-file-sha256>"
  },
  "position_trials": [
    {"pair_id": "p1-unit-error", "orientation": "AB", "winner": "A"},
    {"pair_id": "p1-unit-error", "orientation": "BA", "winner": "B"}
  ]
}
```

`grounding_receipt` 必须是同一 prepared run 下的独立 `evidence-grounding-v1` JSON 回执，
并用文件 SHA-256 绑定；回执中的每个 `ref_id` 必须绑定 packet manifest 的 `chunk_id`、精确
quote、quote SHA-256、行号和 packet 文件哈希。`grounded` 不再是模型可以自报的字段，而是
报告阶段根据回执验证结果重新计算。中性变换 observation 还必须记录 `baseline_decision`。
基线本就失败的中性 case 只进入 neutral flip 指标，不进入 specificity 或 false-reopen 分母。
硬缺陷只有在输出阻断动作且存在同 mutation family 的、回执验证通过的 finding 时才记为
true positive。

```bash
python3 scripts/capability_harness.py report \
  evaluation/capability_runs/run-001/prepared_manifest.json \
  --json-output evaluation/capability_runs/run-001/capability_report.json
```

报告包含 sensitivity、specificity、precision、neutral flip、AB/BA position bias、证据
grounding、indeterminate 和 false-reopen；每个比例都包含 95% Wilson 区间。训练/测试在
`project_id`、`problem_id` 或 `mutation_family` 任一声明轴上重叠时会直接失败，不会只打印
警告。capability matrix 只允许用于角色路由和 shadow 资格标注，`truth_claim` 固定为 `NONE`。

角色级 `FAIL/REVISE`、旧路由名 `REOPEN_MODEL/REOPEN_TEXT` 与当前 aggregate 的
`REOPEN_REVISION_MODEL/REOPEN_REVISION_TEXT` 都可读取。Shadow 阈值比较使用规范化后的
`BLOCK_MODEL/BLOCK_TEXT/PASS/INDETERMINATE` 动作，同时单独报告 raw-label agreement，避免
schema 标签迁移被误记成行为回归。

## 3. Shadow 比较与人工切换

shadow manifest 使用 `judge-shadow-manifest-v1`，必须固定 capability report 的文件 SHA-256，
声明目标 role/evaluator、实际 packet 路径、legacy/new decision，以及全部核心阈值。能力下限
使用 Wilson lower bound；错误率上限使用 Wilson upper bound。还必须设置最少能力 case、
shadow case 和 shadow project 数量。

```json
{
  "schema": "judge-shadow-manifest-v1",
  "capability_report_sha256": "<reviewed report file hash>",
  "target_route": {
    "role": "math",
    "evaluator": {
      "model": "model-name",
      "backend": "backend-name",
      "prompt_sha256": "<64 lowercase hex>",
      "schema_sha256": "<64 lowercase hex>"
    },
    "evaluator_identity_fingerprint": "<prepared report 中的值>"
  },
  "disjoint_from_capability_by": ["project_id"],
  "thresholds": {
    "minimum_test_cases": 50,
    "minimum_shadow_cases": 50,
    "minimum_shadow_projects": 3,
    "capability": {
      "sensitivity": 0.90,
      "specificity": 0.90,
      "precision": 0.90,
      "evidence_grounding_rate": 0.99,
      "neutral_flip_rate": 0.05,
      "position_bias_rate": 0.05,
      "indeterminate_rate": 0.05,
      "false_reopen_rate": 0.05
    },
    "shadow": {
      "agreement_rate_min": 0.90,
      "new_indeterminate_rate_max": 0.05,
      "relaxation_rate_max": 0.05
    }
  },
  "cases": [
    {
      "id": "shadow-project-1-math",
      "project_id": "shadow-project-1",
      "problem_id": "2026A",
      "role": "math",
      "packet_path": "shadow_packets/project-1/math",
      "new_runtime_identity": {
        "model": "model-name",
        "backend": "backend-name",
        "prompt_sha256": "<same prompt hash>",
        "schema_sha256": "<same schema hash>",
        "packet_sha256": "<actual packet tree hash>"
      },
      "legacy_decision": "PASS",
      "new_decision": "PASS"
    }
  ]
}
```

```bash
python3 scripts/shadow_cutover.py evaluation/shadow_manifest.json \
  --capability-report evaluation/capability_runs/run-001/capability_report.json \
  --json-output evaluation/shadow_runs/run-001/report.json \
  --require-ready
```

退出码 `0` 表示报告生成成功且（启用 `--require-ready` 时）阈值通过，`3` 表示报告有效但
尚未达到阈值，`2` 表示身份、hash、holdout、schema 或阈值合同无效。

即使 `cutover_ready=true`，输出仍固定包含：

```json
{
  "advisory_only": true,
  "automatic_switch_performed": false,
  "operator_authorization_required": true,
  "award_prediction": "UNAVAILABLE_WITHOUT_HUMAN_CALIBRATION"
}
```

因此该 CLI 不可能自动改写 `run_paper.sh`、模型路由或交付状态。
