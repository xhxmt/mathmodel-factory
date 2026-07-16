# Evaluation Calibration Report

Models: deepseek-chat, gemini-3.1-pro-preview

## Paper Coverage

| Paper | Problem | Award tier | Status | Correctness | Writing |
|---|---|---|---|---:|---:|
| national1_2024b | CUMCM-2024-B | national_first | AVAILABLE | 10 | 55 |
| provincial1_2024b | CUMCM-2024-B | provincial_first | AVAILABLE | 20 | 55 |
| provincial3_2024b | CUMCM-2024-B | provincial_third | AVAILABLE | 20 | 35 |
| generated_2024b | CUMCM-2024-B |  | AVAILABLE | 35 | 94 |
| generated_2024a_repaired | CUMCM-2024-A |  | AVAILABLE | 100 | 98 |

## Metrics

- Pairwise award-order accuracy: 1 (2/2 readiness pairs; 1 diagnostic pairs excluded)
- Kendall-style ordering: 1
- Malformed-output rate: 0 (0/24)
- Fatal-flaw detection rate: N/A (0/0)
- Direct blind-pair coverage: 1
- Split correctness/writing coverage: 1
- Step 13 score reliability: NOT READY
- Proxy A/B reliability: NOT READY
- Correctness/writing axis reliability: NOT READY
- Human calibration: NOT READY
- Award prediction: NOT READY

## Blind Pairwise Results

| Expected higher | Expected lower | Result | Source | Complete |
|---|---|---|---|---|
| national1_2024b | provincial1_2024b | CORRECT | BLIND_PAIRWISE | True |
| provincial1_2024b | provincial3_2024b | CORRECT | BLIND_PAIRWISE | True |
| provincial3_2024b | generated_2024b | DIAGNOSTIC_REVERSED | BLIND_PAIRWISE | True |

## Reliability Checks

- PASS: all_papers_scored
- PASS: split_axis_coverage
- PASS: direct_pair_coverage
- PASS: pairwise_accuracy
- PASS: malformed_output_rate
- FAIL: fatal_flaw_detection
- FAIL: correctness_pairwise_accuracy
- FAIL: writing_pairwise_accuracy

## Missing Results

- None
