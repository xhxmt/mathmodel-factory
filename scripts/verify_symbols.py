#!/usr/bin/env python3
"""Extract math symbols from a .tex paper and check them against symbol_table.md.

Usage:
    python3 verify_symbols.py <project_dir> <base_name>
    python3 verify_symbols.py --paper <paper.tex_or_full.md> --table <symbol_table.md>

A deterministic structural check that complements the LLM judge: LLM scoring of
"mathematical rigor" is noisy (see evaluation/baseline_scores.md), but symbol
consistency is mechanical. This script flags:

  - UNDEFINED symbols: used in the paper body but never registered in
    symbol_table.md (the "用了未定义" defect, cf. the Rcons signal in
    csa_1543941.pdf).
  - USE-BEFORE-DEF: a symbol's first appearance in the body precedes the
    symbol table (weak signal; the table is usually a fixed early section).

Output mirrors verify_numbers.py:
    SYMBOLS_DEFINED   = N    (rows parsed from symbol_table.md)
    SYMBOLS_USED      = M    (distinct base symbols in body math)
    UNDEFINED_SYMBOLS = K    (used but not defined) -> the hard signal

Exit code: 0 if no undefined symbols, 1 otherwise.
"""

import re
import sys
import os


# ── symbol normalization ──────────────────────────────────────────────────────

# LaTeX commands that are operators/structure, NOT user symbols
_LATEX_NOISE = {
    'frac', 'sqrt', 'sum', 'prod', 'int', 'lim', 'inf', 'sup', 'min', 'max',
    'sin', 'cos', 'tan', 'log', 'ln', 'exp', 'arg', 'det', 'dim', 'mod',
    'left', 'right', 'big', 'Big', 'bigg', 'Bigg', 'quad', 'qquad',
    'cdot', 'cdots', 'ldots', 'dots', 'vdots', 'ddots', 'times', 'div',
    'leq', 'geq', 'neq', 'approx', 'equiv', 'sim', 'simeq', 'cong', 'propto',
    'in', 'notin', 'subset', 'supset', 'subseteq', 'cup', 'cap', 'emptyset',
    'forall', 'exists', 'nabla', 'partial', 'infty', 'rightarrow', 'leftarrow',
    'Rightarrow', 'Leftarrow', 'mapsto', 'to', 'gets', 'iff',
    'text', 'mathrm', 'mathbf', 'mathcal', 'mathbb', 'boldsymbol', 'bm',
    'mathit', 'mathsf', 'mathtt', 'operatorname', 'displaystyle', 'limits',
    'begin', 'end', 'label', 'ref', 'tag', 'notag', 'nonumber',
    'star', 'ast', 'circ', 'bullet', 'oplus', 'otimes', 'wedge', 'vee',
    'alpha', 'beta', 'gamma', 'delta', 'epsilon', 'varepsilon', 'zeta', 'eta',
    'theta', 'vartheta', 'iota', 'kappa', 'lambda', 'mu', 'nu', 'xi', 'pi',
    'varpi', 'rho', 'varrho', 'sigma', 'varsigma', 'tau', 'upsilon', 'phi',
    'varphi', 'chi', 'psi', 'omega',
    'Gamma', 'Delta', 'Theta', 'Lambda', 'Xi', 'Pi', 'Sigma', 'Upsilon',
    'Phi', 'Psi', 'Omega',
    'hat', 'bar', 'tilde', 'vec', 'dot', 'ddot', 'overline', 'underline',
    'overbrace', 'underbrace', 'overrightarrow', 'prime',
    'mathop', 'nolimits', 'substack', 'stackrel', 'binom', 'choose',
    'leftrightarrow', 'longrightarrow', 'Longrightarrow', 'implies',
    'land', 'lor', 'lnot', 'setminus', 'mid', 'parallel', 'perp', 'angle',
    'leqslant', 'geqslant', 'll', 'gg', 'pm', 'mp', 'ge', 'le', 'ne',
    # Structural / formatting / float commands (not math symbols)
    'section', 'subsection', 'subsubsection', 'paragraph', 'subparagraph',
    'caption', 'centering', 'includegraphics', 'item', 'itemize', 'enumerate',
    'toprule', 'midrule', 'bottomrule', 'cmidrule', 'hline', 'tabcolsep',
    'textbf', 'textit', 'texttt', 'textsf', 'textrm', 'emph', 'underline',
    'small', 'footnotesize', 'scriptsize', 'tiny', 'large', 'Large', 'huge',
    'normalsize', 'textwidth', 'linewidth', 'columnwidth', 'setlength',
    'appendix', 'bibliography', 'bibliographystyle', 'cite', 'citep', 'citet',
    'lstinputlisting', 'lstlisting', 'verbatim', 'boxed', 'varnothing',
    'tfrac', 'dfrac', 'frac', 'hspace', 'vspace', 'noindent', 'indent',
    'newpage', 'clearpage', 'pagebreak', 'linebreak', 'par', 'newline',
    'Longleftrightarrow', 'Longleftarrow', 'Longrightarrow', 'longleftarrow',
    'colon', 'ldotp', 'cdotp', 'rule', 'multicolumn', 'multirow', 'arraystretch',
    'mathstrut', 'phantom', 'hphantom', 'vphantom', 'qedhere', 'square',
    'color', 'textcolor', 'rowcolor', 'cellcolor', 'arraybackslash',
}

# Greek command names are *symbols* when used as variables; keep a separate set
_GREEK = {
    'alpha', 'beta', 'gamma', 'delta', 'epsilon', 'varepsilon', 'zeta', 'eta',
    'theta', 'vartheta', 'kappa', 'lambda', 'mu', 'nu', 'xi', 'pi', 'varpi',
    'rho', 'varrho', 'sigma', 'varsigma', 'tau', 'upsilon', 'phi', 'varphi',
    'chi', 'psi', 'omega',
    'Gamma', 'Delta', 'Theta', 'Lambda', 'Xi', 'Pi', 'Sigma', 'Upsilon',
    'Phi', 'Psi', 'Omega',
}


def normalize_symbol(raw: str) -> str:
    """Reduce a math token to its base symbol identity.

    Strips subscripts/superscripts, decorations (hat/bar/vec), and font
    wrappers, so that x_i, x^2, \\hat{x}, \\boldsymbol{x} all map to 'x'.
    Returns '' if the token is not a real symbol (operator, number, etc.).
    """
    s = raw.strip()
    if not s:
        return ''
    # Strip a leading backslash command name
    m = re.match(r'\\([A-Za-z]+)', s)
    if m:
        cmd = m.group(1)
        if cmd in _GREEK:
            return '\\' + cmd  # keep greek letters as symbols
        if cmd in _LATEX_NOISE:
            return ''
        # Unknown command: treat its name as the symbol base (e.g. custom macro)
        return '\\' + cmd
    # Single Latin letter (possibly with following sub/superscript already stripped)
    m = re.match(r'([A-Za-z])', s)
    if m:
        return m.group(1)
    return ''


def extract_defined_symbols(table_path: str) -> set:
    """Parse symbol_table.md: collect base symbols from the first column of
    every markdown table row or HTML <td> that contains $...$."""
    defined = set()
    if not os.path.exists(table_path):
        return defined
    with open(table_path, encoding='utf-8') as f:
        text = f.read()

    # Parse markdown tables: | ... | ... |
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith('|'):
            continue
        cells = [c.strip() for c in line.strip('|').split('|')]
        if not cells:
            continue
        first = cells[0]
        # Skip header/separator rows
        if not first or set(first) <= set('-: '):
            continue
        # Extract every $...$ symbol expression in the first cell
        for mexpr in re.findall(r'\$([^$]+)\$', first):
            for tok in _split_math_tokens(mexpr):
                base = normalize_symbol(tok)
                if base:
                    defined.add(base)

    # Parse HTML tables: <tr><td>$...$</td>...
    # OCR from PDF often emits HTML tables instead of markdown
    for row_m in re.finditer(r'<tr>(.*?)</tr>', text, flags=re.DOTALL | re.IGNORECASE):
        row_html = row_m.group(1)
        cells = re.findall(r'<td[^>]*>(.*?)</td>', row_html, flags=re.DOTALL | re.IGNORECASE)
        if not cells:
            continue
        first = cells[0].strip()
        # Skip header rows
        if not first or re.match(r'^符号|symbol|notation', first, re.IGNORECASE):
            continue
        # Extract $...$ from first cell
        for mexpr in re.findall(r'\$([^$]+)\$', first):
            for tok in _split_math_tokens(mexpr):
                base = normalize_symbol(tok)
                if base:
                    defined.add(base)

    # Fallback for OCR symbol tables rendered as $$...$$ display-math blocks
    # (one symbol per block) rather than table rows. Only triggers when the
    # table/HTML parse found little — avoids polluting from prose math.
    if len(defined) < 3:
        for mexpr in re.findall(r'\$\$(.+?)\$\$', text, flags=re.DOTALL):
            for tok in _split_math_tokens(mexpr):
                base = normalize_symbol(tok)
                if base:
                    defined.add(base)
        # Also single-$ in a symbol-table file
        for mexpr in re.findall(r'(?<!\$)\$(?!\$)([^$]+)\$(?!\$)', text):
            for tok in _split_math_tokens(mexpr):
                base = normalize_symbol(tok)
                if base:
                    defined.add(base)

    return defined


def _split_math_tokens(expr: str) -> list:
    """Split a math expression into candidate symbol tokens.

    Cuts on operators, braces, sub/superscripts; keeps backslash-commands and
    single letters as tokens.
    """
    # Pull out backslash-commands first
    tokens = []
    # Tokenize: \command  OR  single letter  OR  digit-run
    for m in re.finditer(r'\\[A-Za-z]+|[A-Za-z]|\d+', expr):
        tokens.append(m.group(0))
    return tokens


def extract_used_symbols(paper_path: str):
    """Extract distinct base symbols from all math in the paper body.

    Handles both LaTeX ($...$, \\[...\\], equation envs) and OCR markdown
    (also $...$ and $$...$$). Returns (used_set, first_line_of_use dict).
    """
    with open(paper_path, encoding='utf-8') as f:
        text = f.read()

    # For .tex: drop preamble before \begin{document}
    doc_start = text.find(r'\begin{document}')
    if doc_start >= 0:
        text = text[doc_start:]

    # Strip environments whose "$...$" content is not prose math:
    # tabular/table cells, code listings, and figure floats. This prevents the
    # $...$ matcher from bleeding across LaTeX markup (e.g. a stray $ in a table
    # row pairing with a $ many lines later, swallowing \caption, \toprule, ...).
    for env in ('tabular', 'lstlisting', 'verbatim', 'minted', 'figure',
                'table', 'tabularx', 'longtable'):
        text = re.sub(rf'\\begin\{{{env}\*?\}}.*?\\end\{{{env}\*?\}}',
                      ' ', text, flags=re.DOTALL)
    # Strip HTML <table>...</table> blocks (OCR markdown often emits these)
    text = re.sub(r'<table>.*?</table>', ' ', text, flags=re.DOTALL | re.IGNORECASE)
    # Strip fenced code blocks (```...```)
    text = re.sub(r'```.*?```', ' ', text, flags=re.DOTALL)

    used = set()
    first_use_line = {}

    def line_of(pos: int) -> int:
        return text.count('\n', 0, pos) + 1

    # Math span patterns: $$...$$, $...$, \[...\], \(...\), equation/align envs
    math_patterns = [
        r'\$\$(.+?)\$\$',
        r'(?<!\$)\$(?!\$)(.+?)(?<!\$)\$(?!\$)',
        r'\\\[(.+?)\\\]',
        r'\\\((.+?)\\\)',
        r'\\begin\{(?:equation|align|aligned|gather|multline)\*?\}(.+?)\\end\{(?:equation|align|aligned|gather|multline)\*?\}',
    ]
    for pat in math_patterns:
        for m in re.finditer(pat, text, flags=re.DOTALL):
            expr = m.group(1)
            ln = line_of(m.start())
            for tok in _split_math_tokens(expr):
                base = normalize_symbol(tok)
                if base:
                    used.add(base)
                    if base not in first_use_line:
                        first_use_line[base] = ln
    return used, first_use_line


def find_symbol_table_line(paper_path: str) -> int:
    """Return the line number where the symbol-table section starts in the
    paper (for use-before-def heuristic), or -1."""
    with open(paper_path, encoding='utf-8') as f:
        for i, line in enumerate(f, 1):
            if re.search(r'符号\s*说明|符号\s*表|变量\s*说明|notation|nomenclature',
                         line, re.IGNORECASE):
                return i
    return -1


def collect_symbol_metrics(project_dir, base_name):
    """返回符号覆盖指标 dict；不打印。paper 缺失返回 None。"""
    paper_path = os.path.join(project_dir, f'{base_name}_paper.tex')
    table_path = os.path.join(project_dir, 'symbol_table.md')
    if not os.path.exists(paper_path):
        return None
    defined = extract_defined_symbols(table_path)
    used, first_use = extract_used_symbols(paper_path)
    table_line = find_symbol_table_line(paper_path)
    undefined = sorted(used - defined)
    use_before_def = []
    if table_line > 0:
        use_before_def = sorted(
            s for s in (used & defined)
            if first_use.get(s, 10**9) < table_line - 5
        )
    return {
        "symbols_defined": len(defined),
        "symbols_used": len(used),
        "symbols_undefined": len(undefined),
        "use_before_def": len(use_before_def),
        "_undefined_list": undefined,
        "_use_before_def_list": use_before_def,
        "_first_use": first_use,
        "_table_line": table_line,
        "_table_path": table_path,
        "_paper_path": paper_path,
    }


def _print_report(paper_path, table_path, defined_count, used_count,
                  undefined, use_before_def, first_use, table_line):
    """Print the Symbol Verification Report exactly as the CLI always has."""
    print("=== Symbol Verification Report ===")
    print(f"Paper:        {paper_path}")
    print(f"Symbol table: {table_path}{'  (MISSING)' if not os.path.exists(table_path) else ''}")
    print()
    print(f"SYMBOLS_DEFINED   = {defined_count}")
    print(f"SYMBOLS_USED      = {used_count}")
    print(f"UNDEFINED_SYMBOLS = {len(undefined)}")
    print(f"USE_BEFORE_DEF    = {len(use_before_def)}")
    print()

    if undefined:
        print("=" * 60)
        print("UNDEFINED SYMBOLS (used in body, not in symbol table):")
        print("=" * 60)
        for s in undefined:
            print(f"  {s:<16s}  first used at line {first_use.get(s, '?')}")
        print()

    if use_before_def:
        print("=" * 60)
        print("USE-BEFORE-DEFINITION (appears well before symbol table):")
        print("=" * 60)
        for s in use_before_def:
            print(f"  {s:<16s}  line {first_use.get(s)} (< table @ {table_line})")
        print()


def main():
    args = sys.argv[1:]
    if len(args) >= 4 and args[0] == '--paper' and args[2] == '--table':
        # --paper/--table branch: direct computation + prints, unchanged.
        paper_path = args[1]
        table_path = args[3]

        if not os.path.exists(paper_path):
            print(f"ERROR: {paper_path} not found")
            sys.exit(3)

        defined = extract_defined_symbols(table_path)
        used, first_use = extract_used_symbols(paper_path)
        table_line = find_symbol_table_line(paper_path)

        undefined = sorted(used - defined)
        use_before_def = []
        if table_line > 0:
            use_before_def = sorted(
                s for s in (used & defined)
                if first_use.get(s, 10**9) < table_line - 5
            )

        _print_report(paper_path, table_path, len(defined), len(used),
                      undefined, use_before_def, first_use, table_line)
        sys.exit(0 if not undefined else 1)

    elif len(args) >= 2 and not args[0].startswith('--'):
        project_dir, base_name = args[0], args[1]
        metrics = collect_symbol_metrics(project_dir, base_name)
        if metrics is None:
            paper_path = os.path.join(project_dir, f'{base_name}_paper.tex')
            print(f"ERROR: {paper_path} not found")
            sys.exit(3)

        undefined = metrics["_undefined_list"]
        use_before_def = metrics["_use_before_def_list"]
        _print_report(
            metrics["_paper_path"], metrics["_table_path"],
            metrics["symbols_defined"], metrics["symbols_used"],
            undefined, use_before_def,
            metrics["_first_use"], metrics["_table_line"],
        )
        sys.exit(0 if not undefined else 1)

    else:
        print("Usage: verify_symbols.py <project_dir> <base_name>")
        print("   or: verify_symbols.py --paper <paper> --table <symbol_table.md>")
        sys.exit(2)


if __name__ == '__main__':
    main()
