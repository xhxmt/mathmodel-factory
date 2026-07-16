# Evaluation Calibration Report

## Paper Coverage

| Paper | Problem | Award tier | Status | Correctness | Writing |
|---|---|---|---|---:|---:|
| national1_2024b | CUMCM-2024-B | national_first | AVAILABLE | 10 | 55 |
| provincial1_2024b | CUMCM-2024-B | provincial_first | AVAILABLE | 20 | 55 |
| provincial3_2024b | CUMCM-2024-B | provincial_third | AVAILABLE | 20 | 35 |
| generated_2024b | CUMCM-2024-B |  | AVAILABLE | 35 | 94 |
| generated_2024a_known_fatal | CUMCM-2024-A |  | AVAILABLE | 100 | 98 |

## Metrics

- Pairwise award-order accuracy: 0.667 (3/3 pairs evaluated)
- Kendall-style ordering: 0.333
- Malformed-output rate: 0 (0/24)
- Fatal-flaw detection rate: 0 (0/1)
- Direct blind-pair coverage: 1
- Split correctness/writing coverage: 1
- Step 13 score reliability: NOT READY

## Blind Pairwise Results

| Expected higher | Expected lower | Result | Source | Complete |
|---|---|---|---|---|
| national1_2024b | provincial1_2024b | CORRECT | BLIND_PAIRWISE | True |
| provincial1_2024b | provincial3_2024b | CORRECT | BLIND_PAIRWISE | True |
| provincial3_2024b | generated_2024b | REVERSED | BLIND_PAIRWISE | True |

## Reliability Checks

- PASS: all_papers_scored
- PASS: split_axis_coverage
- PASS: direct_pair_coverage
- FAIL: pairwise_accuracy
- PASS: malformed_output_rate
- FAIL: fatal_flaw_detection

## Missing Results

- None
