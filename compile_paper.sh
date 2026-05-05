#!/usr/bin/env bash
set -euo pipefail

PROJECT="${1:?Usage: $0 <project_dir> <base_name>}"
BASE="${2:?Usage: $0 <project_dir> <base_name>}"

cd "$PROJECT"

pdflatex -interaction=nonstopmode "${BASE}_paper.tex" >/dev/null 2>&1

if [[ -f "${BASE}_paper.aux" ]] && grep -q '\\bibdata' "${BASE}_paper.aux" 2>/dev/null; then
    bibtex "${BASE}_paper" >/dev/null 2>&1
fi

pdflatex -interaction=nonstopmode "${BASE}_paper.tex" >/dev/null 2>&1
pdflatex -interaction=nonstopmode "${BASE}_paper.tex" >/dev/null 2>&1
