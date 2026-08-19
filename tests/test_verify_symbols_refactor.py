import os, subprocess, sys
from conftest import FIXTURE, REPO_ROOT, SCRIPTS

GOLDEN = os.path.join(os.path.dirname(__file__), "golden", "verify_symbols_mini.txt")

def test_cli_output_byte_identical():
    out = subprocess.run(
        [sys.executable, os.path.join(SCRIPTS, "verify_symbols.py"), FIXTURE, "mini"],
        capture_output=True, text=True,
    )
    combined = out.stdout + out.stderr
    with open(GOLDEN) as f:
        expected = (
            f.read()
            .replace("{FIXTURE}", FIXTURE)
            .replace("{TRAILING_BLANK_LINE}", "")
        )
    assert combined == expected

def test_collect_symbol_metrics_dict():
    from verify_symbols import collect_symbol_metrics
    m = collect_symbol_metrics(FIXTURE, "mini")
    assert m["symbols_undefined"] == 1   # \beta used, not in table
    assert m["symbols_used"] >= 2
    assert "use_before_def" in m


def test_display_macros_constants_functions_and_micrometre_units_are_not_symbols():
    from verify_symbols import extract_used_symbols_from_text

    used, _ = extract_used_symbols_from_text(
        r"""
        \newcommand{\HeadlineValue}{\ensuremath{7.8975}}
        \begin{document}
        $d=\HeadlineValue\,\mu\mathrm m$, $\arcsin(x)$, and $2\pi$.
        \end{document}
        """
    )

    assert used == {"d", "x"}


def test_standalone_mu_remains_auditable():
    from verify_symbols import extract_used_symbols_from_text

    used, _ = extract_used_symbols_from_text(
        r"\begin{document}$\mu+x$\end{document}"
    )

    assert used == {"\\mu", "x"}
