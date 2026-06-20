#!/usr/bin/env python3
"""Extract numbers from a .tex paper and Stata logs, flag mismatches.

Usage:
    python3 verify_numbers.py <project_dir> <base_name>

Outputs a report to stdout listing every number found in the paper prose
and whether a matching number exists in the log files (within rounding
tolerance). Numbers that appear in the logs are marked OK; numbers with
no log match are flagged for manual review.

Designed as a helper for code-review agents — not a replacement for
careful reading, but a way to catch obvious mismatches.
"""

import re
import sys
import os
import glob


def extract_tex_numbers(tex_path):
    """Extract numbers from paper prose, skipping LaTeX preamble/commands."""
    with open(tex_path, 'r') as f:
        text = f.read()

    # Remove preamble (before \begin{document})
    doc_start = text.find(r'\begin{document}')
    if doc_start >= 0:
        text = text[doc_start:]

    # Remove LaTeX commands that contain non-prose numbers
    # Remove \label{...}, \ref{...}, \input{...}, \includegraphics{...}
    text = re.sub(r'\\(?:label|ref|input|includegraphics|cite[tp]?|citealp|hypersetup|bibliographystyle|bibliography)\{[^}]*\}', '', text)
    # Remove \begin{...} and \end{...}
    text = re.sub(r'\\(?:begin|end)\{[^}]*\}', '', text)
    # Remove figure/table float environments entirely (captions are ok but
    # the filenames and labels inside aren't prose numbers)
    # Remove tabular content (numbers inside tables are from esttab, not prose)
    text = re.sub(r'\\begin\{tabular\}.*?\\end\{tabular\}', '', text, flags=re.DOTALL)

    results = []
    lines = text.split('\n')
    for i, line in enumerate(lines, 1):
        # Skip pure LaTeX command lines
        if line.strip().startswith('%'):
            continue
        if re.match(r'^\s*\\(setcounter|renewcommand|newcommand|def\\)', line):
            continue

        # Find numbers: integers, decimals, percentages, negatives
        # Match patterns like: 0.037, -0.067, 13.8, 48,539, 2,600, 0.435%
        for m in re.finditer(r'-?\d[\d,]*\.?\d*%?', line):
            num_str = m.group()
            # Skip years (1900-2099) unless they have decimals
            if re.match(r'^(19|20)\d{2}$', num_str):
                continue
            # Skip very small integers that are likely enumerators (1, 2, 3...)
            # but keep anything with decimals or commas
            clean = num_str.replace(',', '').rstrip('%')
            try:
                val = float(clean)
            except ValueError:
                continue
            if val == 0:
                continue
            # Skip integers 1-20 (likely enumerators, footnote numbers, etc.)
            if val == int(val) and 1 <= val <= 20 and '.' not in num_str:
                continue

            # Get surrounding context (30 chars each side)
            start = max(0, m.start() - 40)
            end = min(len(line), m.end() + 40)
            context = line[start:end].strip()

            results.append({
                'number': num_str,
                'value': val,
                'is_pct': num_str.endswith('%'),
                'context': context,
            })

    return results


def extract_log_numbers(log_dir):
    """Extract all numbers from Stata log files."""
    numbers = set()
    log_files = glob.glob(os.path.join(log_dir, '*.log'))

    for lf in log_files:
        try:
            with open(lf, 'r') as f:
                text = f.read()
        except (IOError, UnicodeDecodeError):
            continue

        for m in re.finditer(r'-?\d[\d,]*\.?\d*', text):
            clean = m.group().replace(',', '')
            try:
                val = float(clean)
            except ValueError:
                continue
            if val != 0:
                numbers.add(val)

    return numbers


def extract_table_numbers(tables_dir):
    """Extract numbers from .tex table files."""
    numbers = set()
    table_files = glob.glob(os.path.join(tables_dir, '*.tex'))

    for tf in table_files:
        try:
            with open(tf, 'r') as f:
                text = f.read()
        except (IOError, UnicodeDecodeError):
            continue

        for m in re.finditer(r'-?\d[\d,]*\.?\d*', text):
            clean = m.group().replace(',', '')
            try:
                val = float(clean)
            except ValueError:
                continue
            if val != 0:
                numbers.add(val)

    return numbers


def number_matches(val, reference_numbers, tolerance=0.02):
    """Check if val matches any reference number within tolerance."""
    # Exact match
    if val in reference_numbers:
        return True

    # Check with rounding tolerance (relative for large, absolute for small)
    for ref in reference_numbers:
        if ref == 0:
            continue
        # Relative tolerance
        if abs(val - ref) / abs(ref) <= tolerance:
            return True
        # Also check if paper reports a scaled version (x100 for percentages)
        if abs(val - ref * 100) / abs(ref * 100) <= tolerance:
            return True
        if ref != 0 and abs(val - ref / 100) / abs(ref / 100) <= tolerance:
            return True
        # Check x1000 scaling
        if abs(val - ref * 1000) / abs(ref * 1000) <= tolerance:
            return True

    return False


def collect_number_metrics(project_dir, base_name):
    """返回数值一致性指标 dict；不打印。paper 缺失返回 None。"""
    tex_path = os.path.join(project_dir, f'{base_name}_paper.tex')
    if not os.path.exists(tex_path):
        return None
    log_dir = os.path.join(project_dir, 'logs')
    tables_dir = os.path.join(project_dir, 'tables')
    paper_numbers = extract_tex_numbers(tex_path)
    log_numbers = extract_log_numbers(log_dir)
    table_numbers = extract_table_numbers(tables_dir)
    reference_numbers = log_numbers | table_numbers
    matched, unmatched = [], []
    for entry in paper_numbers:
        (matched if number_matches(entry['value'], reference_numbers) else unmatched).append(entry)
    return {
        "numbers_matched": len(matched),
        "numbers_unmatched": len(unmatched),
        "_matched": matched,
        "_unmatched": unmatched,
        "_paper_numbers": paper_numbers,
        "_reference_count": len(reference_numbers),
        "_log_count": len(log_numbers),
        "_table_count": len(table_numbers),
    }


def main():
    if len(sys.argv) < 3:
        print("Usage: verify_numbers.py <project_dir> <base_name>")
        sys.exit(1)

    project_dir = sys.argv[1]
    base_name = sys.argv[2]

    tex_path = os.path.join(project_dir, f'{base_name}_paper.tex')

    if not os.path.exists(tex_path):
        print(f"ERROR: {tex_path} not found")
        sys.exit(1)

    metrics = collect_number_metrics(project_dir, base_name)
    matched = metrics['_matched']
    unmatched = metrics['_unmatched']

    print(f"=== Number Verification Report ===")
    print(f"Paper: {tex_path}")
    print(f"Log numbers found: {metrics['_log_count']}")
    print(f"Table numbers found: {metrics['_table_count']}")
    print(f"Paper prose numbers found: {len(metrics['_paper_numbers'])}")
    print()

    print(f"MATCHED (found in logs/tables): {len(matched)}")
    print(f"UNMATCHED (no source found):    {len(unmatched)}")
    print()

    if unmatched:
        print("=" * 70)
        print("UNMATCHED NUMBERS — review these manually:")
        print("=" * 70)
        for entry in unmatched:
            print(f"  {entry['number']:>12s}  ...{entry['context']}...")
        print()

    if matched:
        print("=" * 70)
        print("MATCHED NUMBERS (OK):")
        print("=" * 70)
        for entry in matched:
            print(f"  {entry['number']:>12s}  ...{entry['context']}...")

    # Exit code: 0 if all matched, 1 if any unmatched
    sys.exit(0 if not unmatched else 1)


if __name__ == '__main__':
    main()
