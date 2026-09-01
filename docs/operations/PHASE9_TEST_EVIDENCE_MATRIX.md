# Phase9 test evidence matrix

The audit package's `evidence/FINAL_TEST_SUMMARY.json` owns exact collected,
passed, failed, error, skipped, xfailed, xpassed and warning counts. Every row
below must have a raw log and exact command record for both source and fresh
execution when marked paired.

| Suite | Contract | Source/fresh |
| --- | --- | --- |
| Phase9 focused | generation, evidence binding, A2_0016 state machine, migration, CLI, rollback/fault injection | Paired |
| Phase9 entry / AR-007 | all nine P0 receipts, no-judge terminal, workflow/release rejection | Paired |
| Phase1–8 continuous | durable Phase3→8 identity/current chain | Paired |
| Phase7+8 regression | versioned exact bootstrap contract and enabled E2E | Paired |
| Full repository | all feasible repository tests, with every warning/non-pass retained | Paired |
| Archive verification | one root, canonical paths, fixed timestamps/modes, manifest/checksum closure, CRC | Final package only |

Source/fresh consistency means exact equality of collected outcome categories
for each paired suite, not merely equal exit codes or matching summary text.
Warnings are reported separately and never silently counted as passes.
