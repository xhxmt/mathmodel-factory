#!/usr/bin/env bash
#
# fetch_benchmark.sh — download CUMCM 国赛 past problems for testing the
# modeling factory.  Pulls the official zips from mcm.edu.cn, extracts
# A/B/C/D (本科组), and skips E (高职高专).  Idempotent: re-runs only
# fetch what is missing.
#
# Sources confirmed 2026-05-20:
#   2024: https://www.mcm.edu.cn/upload_cn/node/725/pmkWxf8H9cfe9984c1a1a5b1263e5dd3b5596ed5.zip
#   2025: https://www.mcm.edu.cn/upload_cn/node/759/SvpohSGacdffe718bcaa3b6e835c03ae3461cab1.zip
#
# Usage:
#   ./scripts/fetch_benchmark.sh           # fetch any year missing locally
#   ./scripts/fetch_benchmark.sh 2024      # force-refetch a specific year(s)
#   ./scripts/fetch_benchmark.sh --list    # show what is downloaded
#
# Layout produced:
#   benchmark/cumcm_<year>/{A题,B题,C题,D题,format<year>.doc}
#

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BENCH_DIR="${REPO_ROOT}/benchmark"

declare -A YEAR_URL=(
  [2024]="https://www.mcm.edu.cn/upload_cn/node/725/pmkWxf8H9cfe9984c1a1a5b1263e5dd3b5596ed5.zip"
  [2025]="https://www.mcm.edu.cn/upload_cn/node/759/SvpohSGacdffe718bcaa3b6e835c03ae3461cab1.zip"
)

list_state() {
  printf '%s\n' "Benchmark root: ${BENCH_DIR}"
  if [[ ! -d "${BENCH_DIR}" ]]; then
    echo "  (empty)"
    return
  fi
  for year_dir in "${BENCH_DIR}"/cumcm_*; do
    [[ -d "${year_dir}" ]] || continue
    printf '  %s\n' "$(basename "${year_dir}")"
    for d in "${year_dir}"/*; do
      [[ -e "${d}" ]] || continue
      printf '    %-30s %s\n' "$(basename "${d}")" "$(du -sh "${d}" 2>/dev/null | cut -f1)"
    done
  done
}

fetch_year() {
  local year="$1"
  local url="${YEAR_URL[$year]:-}"
  if [[ -z "${url}" ]]; then
    echo "fetch_benchmark.sh: unknown year ${year} (known: ${!YEAR_URL[*]})" >&2
    return 1
  fi
  local out_dir="${BENCH_DIR}/cumcm_${year}"
  if [[ -d "${out_dir}/A题" && -d "${out_dir}/B题" && -d "${out_dir}/C题" && -d "${out_dir}/D题" ]]; then
    echo "[skip] cumcm_${year} already present"
    return 0
  fi

  mkdir -p "${out_dir}"
  local zip_path
  zip_path="$(mktemp --tmpdir "cumcm${year}.XXXXXX.zip")"
  trap 'rm -f "${zip_path}"' RETURN

  echo "[fetch] cumcm_${year}: ${url}"
  if ! curl -fsSL --retry 3 -o "${zip_path}" "${url}"; then
    echo "fetch_benchmark.sh: download failed for ${year}" >&2
    return 1
  fi

  local stage
  stage="$(mktemp -d)"
  unzip -q -o "${zip_path}" -d "${stage}"

  # Some archives wrap content in CUMCM<year>Problems/, others don't.
  local src="${stage}"
  if [[ -d "${stage}/CUMCM${year}Problems" ]]; then
    src="${stage}/CUMCM${year}Problems"
  fi

  for letter in A B C D; do
    if [[ -d "${src}/${letter}题" ]]; then
      cp -a "${src}/${letter}题" "${out_dir}/"
    else
      echo "  [warn] ${letter}题 not found in ${year} archive" >&2
    fi
  done

  # Format spec (LaTeX/Word reference) is small and useful.
  for f in "${src}"/format*.doc "${src}"/format*.docx; do
    [[ -f "${f}" ]] && cp -a "${f}" "${out_dir}/"
  done

  # E题 deliberately skipped — user (主攻国赛本科组) excluded high-vocational track.
  rm -rf "${stage}"
  echo "[done] cumcm_${year} → ${out_dir}"
}

if [[ "${1:-}" == "--list" ]]; then
  list_state
  exit 0
fi

mkdir -p "${BENCH_DIR}"

if [[ $# -gt 0 ]]; then
  for y in "$@"; do
    fetch_year "${y}"
  done
else
  for y in "${!YEAR_URL[@]}"; do
    fetch_year "${y}"
  done
fi
