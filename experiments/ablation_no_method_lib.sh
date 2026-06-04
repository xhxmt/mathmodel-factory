#!/usr/bin/env bash
# experiments/ablation_no_method_lib.sh — disable the HMML-lite method-library gate.
# Sets ABLATE_NO_METHOD_LIB=1 so check_method_citations() early-returns 0 and
# unregistered methods are no longer blocked at Steps 0/1. Measures the marginal
# value of constraining the pipeline to the curated method library.
set -uo pipefail
ABLATION_VAR=ABLATE_NO_METHOD_LIB
ABLATION_TAG=no_methodlib
# shellcheck source=experiments/_ablation_common.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_ablation_common.sh"
al_main "$@"
