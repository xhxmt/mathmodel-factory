#!/usr/bin/env python3
"""
check_page_count.py - Check PDF page count and suggest appendix trimming

For MCM/ICM papers with 25-page limit.

Usage:
  python3 check_page_count.py <pdf_file> <limit>

Returns:
  0 if page count <= limit
  1 if page count > limit (and prints trimming suggestions)
"""

import sys
import subprocess
from pathlib import Path


def get_page_count(pdf_file: Path) -> int:
    """Get page count using pdfinfo."""
    try:
        result = subprocess.run(
            ['pdfinfo', str(pdf_file)],
            capture_output=True,
            text=True,
            check=True
        )
        for line in result.stdout.split('\n'):
            if line.startswith('Pages:'):
                return int(line.split(':')[1].strip())
    except Exception as e:
        print(f"Error: Failed to get page count: {e}", file=sys.stderr)
        sys.exit(2)

    return 0


def suggest_trimming(page_count: int, limit: int, project_dir: Path) -> None:
    """Suggest which code files to trim from appendix."""
    excess = page_count - limit

    print(f"⚠ Page count ({page_count}) exceeds limit ({limit}) by {excess} pages.")
    print()
    print("Suggested actions to reduce page count:")
    print()

    # Check for code appendix
    models_dir = project_dir / "models"
    if models_dir.exists():
        code_files = []
        for subdir in sorted(models_dir.iterdir()):
            if not subdir.is_dir():
                continue
            for script in sorted(subdir.glob("*.py")):
                size_lines = len(script.read_text().split('\n'))
                code_files.append((script, size_lines))

        if code_files:
            print("1. **Trim code appendix** (keep only core solve scripts):")
            print()

            # Sort by size descending
            code_files.sort(key=lambda x: x[1], reverse=True)

            # Suggest keeping only 03_solve.py
            for script, lines in code_files:
                if '03_solve' in script.name or '02_model' in script.name:
                    print(f"   ✓ KEEP: {script.relative_to(project_dir)} ({lines} lines)")
                else:
                    print(f"   ✗ TRIM: {script.relative_to(project_dir)} ({lines} lines)")

            print()

    print("2. **Reduce figure count** (keep only key results and sensitivity plots)")
    print("3. **Compress derivations** (move detailed proofs to brief statements)")
    print("4. **Reduce table row count** (show top N rows + summary statistics)")
    print()

    # Estimate pages from code
    total_code_lines = sum(lines for _, lines in code_files) if code_files else 0
    code_pages_estimate = total_code_lines / 50  # ~50 lines per page with listings

    if code_pages_estimate > 5:
        trim_target = min(excess, code_pages_estimate - 3)
        print(f"Estimated code appendix: ~{code_pages_estimate:.1f} pages")
        print(f"Trimming to 3 pages would save ~{trim_target:.1f} pages")


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)

    pdf_file = Path(sys.argv[1])
    limit = int(sys.argv[2])

    if not pdf_file.exists():
        print(f"Error: {pdf_file} not found", file=sys.stderr)
        sys.exit(1)

    page_count = get_page_count(pdf_file)

    if page_count <= limit:
        print(f"✓ Page count ({page_count}) is within limit ({limit})")
        sys.exit(0)
    else:
        project_dir = pdf_file.parent
        suggest_trimming(page_count, limit, project_dir)
        sys.exit(1)


if __name__ == "__main__":
    main()
