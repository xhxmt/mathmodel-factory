#!/usr/bin/env bash
set -euo pipefail

PROJECT="${1:?Usage: $0 <project_dir> <base_name>}"
BASE="${2:?Usage: $0 <project_dir> <base_name>}"

cd "$PROJECT"

ENGINE="pdflatex"
if grep -qE '\\documentclass\s*(\[[^]]*\])?\s*\{(ctex|cumcmthesis|mcmthesis)' "${BASE}_paper.tex" 2>/dev/null \
   || grep -q '\\usepackage{xeCJK}' "${BASE}_paper.tex" 2>/dev/null; then
    ENGINE="xelatex"
fi

COMPILE_LOG="${BASE}_compile.log"

"$ENGINE" -interaction=nonstopmode "${BASE}_paper.tex" >"$COMPILE_LOG" 2>&1

if [[ -f "${BASE}_paper.aux" ]] && grep -q '\\bibdata' "${BASE}_paper.aux" 2>/dev/null; then
    bibtex "${BASE}_paper" >>"$COMPILE_LOG" 2>&1
fi

"$ENGINE" -interaction=nonstopmode "${BASE}_paper.tex" >>"$COMPILE_LOG" 2>&1
"$ENGINE" -interaction=nonstopmode "${BASE}_paper.tex" >>"$COMPILE_LOG" 2>&1
