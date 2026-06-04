#!/usr/bin/env bash
# experiments/ablation_no_innovation_protect.sh — disable the PROTECTED non-downgrade rule.
# Sets ABLATE_NO_INNOVATION_PROTECT=1 so render_prompt strips the PROTECTED
# enforcement lines from Steps 4/6/7/10/11/12/13/14, leaving downstream review and
# revision free to weaken or drop the innovative claims. Measures the marginal
# value of protecting differentiating mechanisms (expected to hit 创新性 most).
set -uo pipefail
ABLATION_VAR=ABLATE_NO_INNOVATION_PROTECT
ABLATION_TAG=no_innov
# shellcheck source=experiments/_ablation_common.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_ablation_common.sh"
al_main "$@"
