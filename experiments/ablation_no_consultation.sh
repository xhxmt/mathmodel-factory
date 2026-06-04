#!/usr/bin/env bash
# experiments/ablation_no_consultation.sh — disable Step 1 external web consultation.
# Sets ABLATE_NO_CONSULTATION=1 so render_prompt strips the web-search instruction
# from the Step 1 prompt. Measures the marginal value of literature consultation.
set -uo pipefail
ABLATION_VAR=ABLATE_NO_CONSULTATION
ABLATION_TAG=no_consult
# shellcheck source=experiments/_ablation_common.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_ablation_common.sh"
al_main "$@"
