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

- Pairwise award-order accuracy: 0.909 (11/11 readiness pairs; 0 diagnostic pairs excluded)
- Kendall-style ordering: 0.818
- Malformed-output rate: 0.042 (3/72)
- Fatal-flaw detection rate: 1 (4/4)
- Direct blind-pair coverage: 1
- Split correctness/writing coverage: 1
- Step 13 score reliability: NOT READY
- Proxy A/B reliability: READY
- Correctness/writing axis reliability: NOT READY
- Human calibration: NOT READY
- Award prediction: NOT READY

## Blind Pairwise Results

| Expected higher | Expected lower | Result | Source | Complete |
|---|---|---|---|---|
| n1a_clean | n1a_no_symbols | CORRECT | BLIND_PAIRWISE | True |
| n1a_clean | n1a_numeric_contradiction | CORRECT | BLIND_PAIRWISE | True |
| n1a_clean | n1a_unsupported_optimality | CORRECT | BLIND_PAIRWISE | True |
| n1a_clean | n1a_robotic_repetition | CORRECT | BLIND_PAIRWISE | True |
| n1a_clean | n1a_missing_answers | TIE | BLIND_PAIRWISE | True |
| n1b_clean | n1b_no_sensitivity | CORRECT | BLIND_PAIRWISE | True |
| n1b_clean | n1b_no_symbols | CORRECT | BLIND_PAIRWISE | True |
| n1b_clean | n1b_numeric_contradiction | TIE | BLIND_PAIRWISE | True |
| n1b_clean | n1b_unsupported_optimality | CORRECT | BLIND_PAIRWISE | True |
| n1b_clean | n1b_robotic_repetition | CORRECT | BLIND_PAIRWISE | True |
| n1b_clean | n1b_missing_answers | CORRECT | BLIND_PAIRWISE | True |

## Reliability Checks

- PASS: all_papers_scored
- PASS: split_axis_coverage
- PASS: direct_pair_coverage
- PASS: pairwise_accuracy
- PASS: malformed_output_rate
- PASS: fatal_flaw_detection
- FAIL: correctness_pairwise_accuracy
- FAIL: writing_pairwise_accuracy

## Missing Results

- None
