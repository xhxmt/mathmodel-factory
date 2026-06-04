#!/usr/bin/env bash
# experiments/ablation_no_judge.sh — disable the Step 13 Gate-2 judge simulation.
# Sets ABLATE_NO_JUDGE=1 so run_step_13() writes a VERDICT: PASS stub and skips
# the real judge AND its Gate-2 reopen -> Step 12 second revision pass. Measures
# the marginal value of in-loop judging (note: the delta conflates the two; see
# experiments/README.md).
set -uo pipefail
ABLATION_VAR=ABLATE_NO_JUDGE
ABLATION_TAG=no_judge
# shellcheck source=experiments/_ablation_common.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_ablation_common.sh"
al_main "$@"
