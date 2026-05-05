#!/usr/bin/env bash
set -euo pipefail

detect_stata_bin() {
    if [[ -n "${STATA_BIN:-}" ]]; then
        echo "$STATA_BIN"
        return 0
    fi

    local candidate
    for candidate in \
        "$(command -v stata-mp 2>/dev/null || true)" \
        "$(command -v stata-se 2>/dev/null || true)" \
        "$(command -v stata 2>/dev/null || true)" \
        /usr/local/stata18/stata-mp \
        /usr/local/stata18/stata-se \
        /usr/local/stata18/stata \
        /opt/stata18/stata-mp \
        /opt/stata18/stata-se \
        /opt/stata18/stata \
        /Applications/Stata/StataMP.app/Contents/MacOS/stata-mp \
        /Applications/Stata/StataSE.app/Contents/MacOS/stata-se \
        /Applications/Stata/Stata.app/Contents/MacOS/stata
    do
        [[ -n "$candidate" && -x "$candidate" ]] || continue
        echo "$candidate"
        return 0
    done

    return 1
}

if ! STATA_BIN_PATH="$(detect_stata_bin)"; then
    echo "ERROR: Could not find a local Stata binary." >&2
    echo "Set STATA_BIN to the full path of your Stata executable." >&2
    exit 1
fi

exec "$STATA_BIN_PATH" "$@"
