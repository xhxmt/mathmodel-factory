# Evaluation Calibration Report

Models: deepseek-chat

## Paper Coverage

| Paper | Problem | Award tier | Status | Correctness | Writing |
|---|---|---|---|---:|---:|
| n1a_clean | CUMCM-2024-A |  | AVAILABLE | 55 | 70 |
| n1a_no_symbols | CUMCM-2024-A |  | AVAILABLE | 55 | 70 |
| n1a_numeric_contradiction | CUMCM-2024-A |  | AVAILABLE | 60 | 71 |
| n1a_unsupported_optimality | CUMCM-2024-A |  | AVAILABLE | 65 | 72 |
| n1a_robotic_repetition | CUMCM-2024-A |  | AVAILABLE | 55 | 70 |
| n1a_missing_answers | CUMCM-2024-A |  | AVAILABLE | 65 | 72 |
| n1b_clean | CUMCM-2024-B |  | AVAILABLE | 25 | 45 |
| n1b_no_sensitivity | CUMCM-2024-B |  | AVAILABLE | 25 | 45 |
| n1b_no_symbols | CUMCM-2024-B |  | AVAILABLE | 25 | 40 |
| n1b_numeric_contradiction | CUMCM-2024-B |  | AVAILABLE | 35 | 55 |
| n1b_unsupported_optimality | CUMCM-2024-B |  | AVAILABLE | 25 | 55 |
| n1b_robotic_repetition | CUMCM-2024-B |  | AVAILABLE | 25 | 40 |
| n1b_missing_answers | CUMCM-2024-B |  | AVAILABLE | 25 | 45 |

## Metrics

- Pairwise award-order accuracy: 0.5 (11/11 readiness pairs; 0 diagnostic pairs excluded)
- Kendall-style ordering: 0
- Malformed-output rate: 0.028 (2/72)
- Fatal-flaw detection rate: 1 (4/4)
- Fatal sensitivity: 1; specificity: 0; precision: 0.308; false-positive rate: 1
- Fatal confusion counts: TP=4, FN=0, TN=0, FP=9
- Direct blind-pair coverage: 1
- Split correctness/writing coverage: 1
- Step 13 score reliability: NOT READY
- Proxy A/B reliability: NOT READY
- Pair evaluator composition: primary-only=0, composite=0, adjudicator-decided=0, legacy/unidentified=11
- Runtime score reliability: NOT READY
- Correctness/writing axis reliability: NOT READY
- Human calibration: NOT READY
- Award prediction: NOT READY
- Calibration freshness: STALE

## Blind Pairwise Results

| Expected higher | Expected lower | Result | Source | Evaluator | Decision owner | Complete |
|---|---|---|---|---|---|---|
| n1a_clean | n1a_no_symbols | TIE | BLIND_PAIRWISE | N/A | primary_or_legacy | True |
| n1a_clean | n1a_numeric_contradiction | TIE | BLIND_PAIRWISE | N/A | primary_or_legacy | True |
| n1a_clean | n1a_unsupported_optimality | TIE | BLIND_PAIRWISE | N/A | primary_or_legacy | True |
| n1a_clean | n1a_robotic_repetition | TIE | BLIND_PAIRWISE | N/A | primary_or_legacy | True |
| n1a_clean | n1a_missing_answers | TIE | BLIND_PAIRWISE | N/A | primary_or_legacy | True |
| n1b_clean | n1b_no_sensitivity | TIE | BLIND_PAIRWISE | N/A | primary_or_legacy | True |
| n1b_clean | n1b_no_symbols | TIE | BLIND_PAIRWISE | N/A | primary_or_legacy | True |
| n1b_clean | n1b_numeric_contradiction | TIE | BLIND_PAIRWISE | N/A | primary_or_legacy | True |
| n1b_clean | n1b_unsupported_optimality | TIE | BLIND_PAIRWISE | N/A | primary_or_legacy | True |
| n1b_clean | n1b_robotic_repetition | TIE | BLIND_PAIRWISE | N/A | primary_or_legacy | True |
| n1b_clean | n1b_missing_answers | TIE | BLIND_PAIRWISE | N/A | primary_or_legacy | True |

## Reliability Checks

- PASS: all_papers_scored
- PASS: split_axis_coverage
- PASS: direct_pair_coverage
- FAIL: pairwise_accuracy
- PASS: malformed_output_rate
- PASS: fatal_flaw_detection
- PASS: fatal_sensitivity
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

- None

## Calibration Identity

- Freshness: STALE
- paper n1a_clean: STALE (schema_mismatch, prompt_schema_mismatch, model_config_mismatch, evaluator_identity_missing, prompt_hash_missing, prompt_template_hash_missing, input_fingerprint_missing, result_hash_unpinned)
- paper n1a_no_symbols: STALE (schema_mismatch, prompt_schema_mismatch, model_config_mismatch, evaluator_identity_missing, prompt_hash_missing, prompt_template_hash_missing, input_fingerprint_missing, result_hash_unpinned)
- paper n1a_numeric_contradiction: STALE (schema_mismatch, prompt_schema_mismatch, model_config_mismatch, evaluator_identity_missing, prompt_hash_missing, prompt_template_hash_missing, input_fingerprint_missing, result_hash_unpinned)
- paper n1a_unsupported_optimality: STALE (schema_mismatch, prompt_schema_mismatch, model_config_mismatch, evaluator_identity_missing, prompt_hash_missing, prompt_template_hash_missing, input_fingerprint_missing, result_hash_unpinned)
- paper n1a_robotic_repetition: STALE (schema_mismatch, prompt_schema_mismatch, model_config_mismatch, evaluator_identity_missing, prompt_hash_missing, prompt_template_hash_missing, input_fingerprint_missing, result_hash_unpinned)
- paper n1a_missing_answers: STALE (schema_mismatch, prompt_schema_mismatch, model_config_mismatch, evaluator_identity_missing, prompt_hash_missing, prompt_template_hash_missing, input_fingerprint_missing, result_hash_unpinned)
- paper n1b_clean: STALE (schema_mismatch, prompt_schema_mismatch, model_config_mismatch, evaluator_identity_missing, prompt_hash_missing, prompt_template_hash_missing, input_fingerprint_missing, result_hash_unpinned)
- paper n1b_no_sensitivity: STALE (schema_mismatch, prompt_schema_mismatch, model_config_mismatch, evaluator_identity_missing, prompt_hash_missing, prompt_template_hash_missing, input_fingerprint_missing, result_hash_unpinned)
- paper n1b_no_symbols: STALE (schema_mismatch, prompt_schema_mismatch, model_config_mismatch, evaluator_identity_missing, prompt_hash_missing, prompt_template_hash_missing, input_fingerprint_missing, result_hash_unpinned)
- paper n1b_numeric_contradiction: STALE (schema_mismatch, prompt_schema_mismatch, model_config_mismatch, evaluator_identity_missing, prompt_hash_missing, prompt_template_hash_missing, input_fingerprint_missing, result_hash_unpinned)
- paper n1b_unsupported_optimality: STALE (schema_mismatch, prompt_schema_mismatch, model_config_mismatch, evaluator_identity_missing, prompt_hash_missing, prompt_template_hash_missing, input_fingerprint_missing, result_hash_unpinned)
- paper n1b_robotic_repetition: STALE (schema_mismatch, prompt_schema_mismatch, model_config_mismatch, evaluator_identity_missing, prompt_hash_missing, prompt_template_hash_missing, input_fingerprint_missing, result_hash_unpinned)
- paper n1b_missing_answers: STALE (schema_mismatch, prompt_schema_mismatch, model_config_mismatch, evaluator_identity_missing, prompt_hash_missing, prompt_template_hash_missing, input_fingerprint_missing, result_hash_unpinned)
- pair n1a_no_symbols: STALE (schema_mismatch, prompt_schema_mismatch, model_config_mismatch, evaluator_identity_missing, prompt_hash_missing, prompt_template_hash_missing, input_fingerprint_missing, result_hash_unpinned)
- pair n1a_numeric_contradiction: STALE (schema_mismatch, prompt_schema_mismatch, model_config_mismatch, evaluator_identity_missing, prompt_hash_missing, prompt_template_hash_missing, input_fingerprint_missing, result_hash_unpinned)
- pair n1a_unsupported_optimality: STALE (schema_mismatch, prompt_schema_mismatch, model_config_mismatch, evaluator_identity_missing, prompt_hash_missing, prompt_template_hash_missing, input_fingerprint_missing, result_hash_unpinned)
- pair n1a_robotic_repetition: STALE (schema_mismatch, prompt_schema_mismatch, model_config_mismatch, evaluator_identity_missing, prompt_hash_missing, prompt_template_hash_missing, input_fingerprint_missing, result_hash_unpinned)
- pair n1a_missing_answers: STALE (schema_mismatch, prompt_schema_mismatch, model_config_mismatch, evaluator_identity_missing, prompt_hash_missing, prompt_template_hash_missing, input_fingerprint_missing, result_hash_unpinned)
- pair n1b_no_sensitivity: STALE (schema_mismatch, prompt_schema_mismatch, model_config_mismatch, evaluator_identity_missing, prompt_hash_missing, prompt_template_hash_missing, input_fingerprint_missing, result_hash_unpinned)
- pair n1b_no_symbols: STALE (schema_mismatch, prompt_schema_mismatch, model_config_mismatch, evaluator_identity_missing, prompt_hash_missing, prompt_template_hash_missing, input_fingerprint_missing, result_hash_unpinned)
- pair n1b_numeric_contradiction: STALE (schema_mismatch, prompt_schema_mismatch, model_config_mismatch, evaluator_identity_missing, prompt_hash_missing, prompt_template_hash_missing, input_fingerprint_missing, result_hash_unpinned)
- pair n1b_unsupported_optimality: STALE (schema_mismatch, prompt_schema_mismatch, model_config_mismatch, evaluator_identity_missing, prompt_hash_missing, prompt_template_hash_missing, input_fingerprint_missing, result_hash_unpinned)
- pair n1b_robotic_repetition: STALE (schema_mismatch, prompt_schema_mismatch, model_config_mismatch, evaluator_identity_missing, prompt_hash_missing, prompt_template_hash_missing, input_fingerprint_missing, result_hash_unpinned)
- pair n1b_missing_answers: STALE (schema_mismatch, prompt_schema_mismatch, model_config_mismatch, evaluator_identity_missing, prompt_hash_missing, prompt_template_hash_missing, input_fingerprint_missing, result_hash_unpinned)
