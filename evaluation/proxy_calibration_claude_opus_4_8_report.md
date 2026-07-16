# Evaluation Calibration Report

Models: claude-opus-4-8

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

- Pairwise award-order accuracy: 0.545 (11/11 pairs evaluated)
- Kendall-style ordering: 0.091
- Malformed-output rate: 0.556 (40/72)
- Fatal-flaw detection rate: 0 (0/4)
- Direct blind-pair coverage: 0.909
- Split correctness/writing coverage: 0
- Step 13 score reliability: NOT READY
- Proxy A/B reliability: NOT READY
- Human calibration: NOT READY
- Award prediction: NOT READY

## Blind Pairwise Results

| Expected higher | Expected lower | Result | Source | Complete |
|---|---|---|---|---|
| n1a_clean | n1a_no_symbols | TIE | BLIND_PAIRWISE | True |
| n1a_clean | n1a_numeric_contradiction | TIE | BLIND_PAIRWISE | True |
| n1a_clean | n1a_unsupported_optimality | TIE | BLIND_PAIRWISE | True |
| n1a_clean | n1a_robotic_repetition | TIE | BLIND_PAIRWISE | False |
| n1a_clean | n1a_missing_answers | TIE | BLIND_PAIRWISE | True |
| n1b_clean | n1b_no_sensitivity | TIE | BLIND_PAIRWISE | True |
| n1b_clean | n1b_no_symbols | TIE | BLIND_PAIRWISE | True |
| n1b_clean | n1b_numeric_contradiction | TIE | BLIND_PAIRWISE | True |
| n1b_clean | n1b_unsupported_optimality | TIE | BLIND_PAIRWISE | True |
| n1b_clean | n1b_robotic_repetition | CORRECT | BLIND_PAIRWISE | True |
| n1b_clean | n1b_missing_answers | TIE | BLIND_PAIRWISE | True |

## Reliability Checks

- FAIL: all_papers_scored
- FAIL: split_axis_coverage
- FAIL: direct_pair_coverage
- FAIL: pairwise_accuracy
- FAIL: malformed_output_rate
- FAIL: fatal_flaw_detection
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
