# Evaluation Calibration Report

Models: gemini-3.1-pro-preview

## Paper Coverage

| Paper | Problem | Award tier | Status | Correctness | Writing |
|---|---|---|---|---:|---:|
| n1a_clean | CUMCM-2024-A |  | MISSING | N/A | N/A |
| n1a_no_symbols | CUMCM-2024-A |  | MISSING | N/A | N/A |
| n1a_numeric_contradiction | CUMCM-2024-A |  | MISSING | N/A | N/A |
| n1a_unsupported_optimality | CUMCM-2024-A |  | MISSING | N/A | N/A |
| n1a_robotic_repetition | CUMCM-2024-A |  | MISSING | N/A | N/A |
| n1a_missing_answers | CUMCM-2024-A |  | MISSING | N/A | N/A |
| n1b_clean | CUMCM-2024-B |  | MISSING | N/A | N/A |
| n1b_no_sensitivity | CUMCM-2024-B |  | MISSING | N/A | N/A |
| n1b_no_symbols | CUMCM-2024-B |  | MISSING | N/A | N/A |
| n1b_numeric_contradiction | CUMCM-2024-B |  | MISSING | N/A | N/A |
| n1b_unsupported_optimality | CUMCM-2024-B |  | MISSING | N/A | N/A |
| n1b_robotic_repetition | CUMCM-2024-B |  | MISSING | N/A | N/A |
| n1b_missing_answers | CUMCM-2024-B |  | MISSING | N/A | N/A |

## Metrics

- Pairwise award-order accuracy: 0.864 (11/11 readiness pairs; 0 diagnostic pairs excluded)
- Kendall-style ordering: 0.727
- Malformed-output rate: 0.03 (1/33)
- Fatal-flaw detection rate: N/A (0/0)
- Fatal sensitivity: N/A; specificity: 1; precision: N/A; false-positive rate: 0
- Fatal confusion counts: TP=0, FN=0, TN=9, FP=0
- Direct blind-pair coverage: 0.909
- Split correctness/writing coverage: 0
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
| n1a_clean | n1a_no_symbols | CORRECT | BLIND_PAIRWISE | N/A | primary_or_legacy | True |
| n1a_clean | n1a_numeric_contradiction | CORRECT | BLIND_PAIRWISE | N/A | primary_or_legacy | True |
| n1a_clean | n1a_unsupported_optimality | TIE | BLIND_PAIRWISE | N/A | primary_or_legacy | False |
| n1a_clean | n1a_robotic_repetition | CORRECT | BLIND_PAIRWISE | N/A | primary_or_legacy | True |
| n1a_clean | n1a_missing_answers | TIE | BLIND_PAIRWISE | N/A | primary_or_legacy | True |
| n1b_clean | n1b_no_sensitivity | CORRECT | BLIND_PAIRWISE | N/A | primary_or_legacy | True |
| n1b_clean | n1b_no_symbols | CORRECT | BLIND_PAIRWISE | N/A | primary_or_legacy | True |
| n1b_clean | n1b_numeric_contradiction | TIE | BLIND_PAIRWISE | N/A | primary_or_legacy | True |
| n1b_clean | n1b_unsupported_optimality | CORRECT | BLIND_PAIRWISE | N/A | primary_or_legacy | True |
| n1b_clean | n1b_robotic_repetition | CORRECT | BLIND_PAIRWISE | N/A | primary_or_legacy | True |
| n1b_clean | n1b_missing_answers | CORRECT | BLIND_PAIRWISE | N/A | primary_or_legacy | True |

## Reliability Checks

- FAIL: all_papers_scored
- FAIL: split_axis_coverage
- FAIL: direct_pair_coverage
- FAIL: pairwise_accuracy
- PASS: malformed_output_rate
- FAIL: fatal_flaw_detection
- FAIL: fatal_sensitivity
- PASS: fatal_specificity
- FAIL: fatal_precision
- PASS: fatal_false_positive_rate
- FAIL: calibration_freshness
- FAIL: model_config_match
- FAIL: schema_match
- FAIL: runtime_explicitly_validated
- FAIL: runtime_evaluator_schema_match
- FAIL: runtime_packet_modality_match
- FAIL: correctness_pairwise_accuracy
- FAIL: writing_pairwise_accuracy

## Missing Results

- n1a_clean: MISSING
- n1a_missing_answers: MISSING
- n1a_no_symbols: MISSING
- n1a_numeric_contradiction: MISSING
- n1a_robotic_repetition: MISSING
- n1a_unsupported_optimality: MISSING
- n1b_clean: MISSING
- n1b_missing_answers: MISSING
- n1b_no_sensitivity: MISSING
- n1b_no_symbols: MISSING
- n1b_numeric_contradiction: MISSING
- n1b_robotic_repetition: MISSING
- n1b_unsupported_optimality: MISSING

## Calibration Identity

- Freshness: STALE
- paper n1a_clean: STALE (missing_result)
- paper n1a_no_symbols: STALE (missing_result)
- paper n1a_numeric_contradiction: STALE (missing_result)
- paper n1a_unsupported_optimality: STALE (missing_result)
- paper n1a_robotic_repetition: STALE (missing_result)
- paper n1a_missing_answers: STALE (missing_result)
- paper n1b_clean: STALE (missing_result)
- paper n1b_no_sensitivity: STALE (missing_result)
- paper n1b_no_symbols: STALE (missing_result)
- paper n1b_numeric_contradiction: STALE (missing_result)
- paper n1b_unsupported_optimality: STALE (missing_result)
- paper n1b_robotic_repetition: STALE (missing_result)
- paper n1b_missing_answers: STALE (missing_result)
- pair n1a_no_symbols: STALE (schema_mismatch, prompt_schema_mismatch, model_mismatch, model_config_mismatch, evaluator_identity_missing, prompt_hash_missing, prompt_template_hash_missing, input_fingerprint_missing, result_hash_unpinned)
- pair n1a_numeric_contradiction: STALE (schema_mismatch, prompt_schema_mismatch, model_mismatch, model_config_mismatch, evaluator_identity_missing, prompt_hash_missing, prompt_template_hash_missing, input_fingerprint_missing, result_hash_unpinned)
- pair n1a_unsupported_optimality: STALE (schema_mismatch, prompt_schema_mismatch, model_mismatch, model_config_mismatch, evaluator_identity_missing, prompt_hash_missing, prompt_template_hash_missing, input_fingerprint_missing, result_hash_unpinned)
- pair n1a_robotic_repetition: STALE (schema_mismatch, prompt_schema_mismatch, model_mismatch, model_config_mismatch, evaluator_identity_missing, prompt_hash_missing, prompt_template_hash_missing, input_fingerprint_missing, result_hash_unpinned)
- pair n1a_missing_answers: STALE (schema_mismatch, prompt_schema_mismatch, model_mismatch, model_config_mismatch, evaluator_identity_missing, prompt_hash_missing, prompt_template_hash_missing, input_fingerprint_missing, result_hash_unpinned)
- pair n1b_no_sensitivity: STALE (schema_mismatch, prompt_schema_mismatch, model_mismatch, model_config_mismatch, evaluator_identity_missing, prompt_hash_missing, prompt_template_hash_missing, input_fingerprint_missing, result_hash_unpinned)
- pair n1b_no_symbols: STALE (schema_mismatch, prompt_schema_mismatch, model_mismatch, model_config_mismatch, evaluator_identity_missing, prompt_hash_missing, prompt_template_hash_missing, input_fingerprint_missing, result_hash_unpinned)
- pair n1b_numeric_contradiction: STALE (schema_mismatch, prompt_schema_mismatch, model_mismatch, model_config_mismatch, evaluator_identity_missing, prompt_hash_missing, prompt_template_hash_missing, input_fingerprint_missing, result_hash_unpinned)
- pair n1b_unsupported_optimality: STALE (schema_mismatch, prompt_schema_mismatch, model_mismatch, model_config_mismatch, evaluator_identity_missing, prompt_hash_missing, prompt_template_hash_missing, input_fingerprint_missing, result_hash_unpinned)
- pair n1b_robotic_repetition: STALE (schema_mismatch, prompt_schema_mismatch, model_mismatch, model_config_mismatch, evaluator_identity_missing, prompt_hash_missing, prompt_template_hash_missing, input_fingerprint_missing, result_hash_unpinned)
- pair n1b_missing_answers: STALE (schema_mismatch, prompt_schema_mismatch, model_mismatch, model_config_mismatch, evaluator_identity_missing, prompt_hash_missing, prompt_template_hash_missing, input_fingerprint_missing, result_hash_unpinned)
