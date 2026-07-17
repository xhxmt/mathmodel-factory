# Evaluation Calibration Report

Models: gemini-3.1-pro-preview

## Paper Coverage

| Paper | Problem | Award tier | Status | Correctness | Writing |
|---|---|---|---|---:|---:|
| national1_2024b | CUMCM-2024-B | national_first | AVAILABLE | 10 | 55 |
| provincial1_2024b | CUMCM-2024-B | provincial_first | AVAILABLE | 20 | 55 |
| provincial3_2024b | CUMCM-2024-B | provincial_third | AVAILABLE | 20 | 35 |
| generated_2024b | CUMCM-2024-B |  | AVAILABLE | 35 | 94 |
| generated_2024a_repaired | CUMCM-2024-A |  | MISSING | N/A | N/A |

## Metrics

- Pairwise award-order accuracy: 1 (2/2 readiness pairs; 1 diagnostic pairs excluded)
- Kendall-style ordering: 1
- Malformed-output rate: 0 (0/21)
- Fatal-flaw detection rate: N/A (0/0)
- Fatal sensitivity: N/A; specificity: 0.5; precision: 0; false-positive rate: 0.5
- Fatal confusion counts: TP=0, FN=0, TN=1, FP=1
- Direct blind-pair coverage: 1
- Split correctness/writing coverage: 0.8
- Step 13 score reliability: NOT READY
- Proxy A/B reliability: NOT READY
- Pair evaluator composition: primary-only=0, composite=0, adjudicator-decided=0, legacy/unidentified=3
- Runtime score reliability: NOT READY
- Correctness/writing axis reliability: NOT READY
- Human calibration: NOT READY
- Award prediction: NOT READY
- Calibration freshness: STALE

## Blind Pairwise Results

| Expected higher | Expected lower | Result | Source | Evaluator | Decision owner | Complete |
|---|---|---|---|---|---|---|
| national1_2024b | provincial1_2024b | CORRECT | BLIND_PAIRWISE | N/A | primary_or_legacy | True |
| provincial1_2024b | provincial3_2024b | CORRECT | BLIND_PAIRWISE | N/A | primary_or_legacy | True |
| provincial3_2024b | generated_2024b | DIAGNOSTIC_REVERSED | BLIND_PAIRWISE | N/A | primary_or_legacy | True |

## Reliability Checks

- FAIL: all_papers_scored
- FAIL: split_axis_coverage
- PASS: direct_pair_coverage
- PASS: pairwise_accuracy
- PASS: malformed_output_rate
- FAIL: fatal_flaw_detection
- FAIL: fatal_sensitivity
- FAIL: fatal_specificity
- FAIL: fatal_precision
- FAIL: fatal_false_positive_rate
- FAIL: calibration_freshness
- FAIL: model_config_match
- FAIL: schema_match
- FAIL: runtime_explicitly_validated
- FAIL: runtime_evaluator_schema_match
- FAIL: runtime_packet_modality_match
- FAIL: correctness_pairwise_accuracy
- FAIL: writing_pairwise_accuracy

## Missing Results

- generated_2024a_repaired: MISSING

## Calibration Identity

- Freshness: STALE
- paper national1_2024b: STALE (schema_mismatch, prompt_schema_mismatch, model_config_mismatch, evaluator_identity_missing, prompt_hash_missing, prompt_template_hash_missing, input_fingerprint_missing, result_hash_unpinned)
- paper provincial1_2024b: STALE (schema_mismatch, prompt_schema_mismatch, model_config_mismatch, evaluator_identity_missing, prompt_hash_missing, prompt_template_hash_missing, input_fingerprint_missing, result_hash_unpinned)
- paper provincial3_2024b: STALE (schema_mismatch, prompt_schema_mismatch, model_config_mismatch, evaluator_identity_missing, prompt_hash_missing, prompt_template_hash_missing, input_fingerprint_missing, result_hash_unpinned)
- paper generated_2024b: STALE (schema_mismatch, prompt_schema_mismatch, model_config_mismatch, evaluator_identity_missing, prompt_hash_missing, prompt_template_hash_missing, input_fingerprint_missing, result_hash_unpinned)
- paper generated_2024a_repaired: STALE (missing_result)
- pair national1_vs_provincial1_2024b: STALE (schema_mismatch, prompt_schema_mismatch, model_config_mismatch, evaluator_identity_missing, prompt_hash_missing, prompt_template_hash_missing, input_fingerprint_missing, result_hash_unpinned)
- pair provincial1_vs_provincial3_2024b: STALE (schema_mismatch, prompt_schema_mismatch, model_config_mismatch, evaluator_identity_missing, prompt_hash_missing, prompt_template_hash_missing, input_fingerprint_missing, result_hash_unpinned)
- pair provincial3_vs_generated_2024b: STALE (schema_mismatch, prompt_schema_mismatch, model_config_mismatch, evaluator_identity_missing, prompt_hash_missing, prompt_template_hash_missing, input_fingerprint_missing, result_hash_unpinned)
