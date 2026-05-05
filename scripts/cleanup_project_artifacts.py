#!/usr/bin/env python3
"""Remove rebuildable project data while preserving source artifacts, code, and logs.

The cleanup policy is intentionally conservative for legacy projects whose raw and
derived files were sometimes mixed. For new projects, use the structured layout
documented in analysis_guide.md so this script can prune precisely.
"""

import argparse
import os
import re
import shutil
import sys
from pathlib import Path


STRUCTURED_DELETE_DIRS = {
    ("data", "intermediate"),
    ("data", "final"),
    ("analysis", "intermediate"),
    ("analysis", "final"),
    ("analysis", "unified"),
    ("replication", "intermediate"),
    ("replication", "final"),
    ("replication", "unified"),
}

TEMP_DIR_NAMES = {
    "temp",
    "tmp",
    ".tmp",
    ".audit_tmp",
}

TEMP_DIR_PREFIXES = (
    "tmp_",
    "tmp-table",
    "tmp_table_pages",
    ".table_page_previews",
)

SOURCE_DIR_NAMES = {
    "raw",
    "external",
    "nlrb",
    "osha",
    "raw_sqlite",
}

SOURCE_FILE_EXTS = {
    ".zip",
    ".xlsx",
    ".xls",
    ".pdf",
    ".html",
    ".json",
    ".txt",
}

CODE_FILE_EXTS = {
    ".py",
    ".do",
    ".sh",
    ".r",
    ".R",
    ".md",
}

SOURCE_NAME_MARKERS = (
    "_raw",
    "readme",
    "codebook",
    "dictionary",
    "download",
    "footnotes",
    "manual",
)

SOURCE_PREFIX_RE = re.compile(
    r"^(ext\d+_(fred|bls|bea|qcew|onet|unionstats|dol|cbp|osha|nlrb|reopening)|"
    r"(fred|bls|bea|qcew|onet|unionstats|osha|nlrb|cbp)_)"
)

TMP_FILE_RE = re.compile(r"(^|[_-])tmp([_.-]|$)")

class CleanupPlan(object):
    def __init__(self):
        self.delete_dirs = []
        self.delete_files = []
        self.keep_files = []


def is_structured_delete_dir(rel_parts):
    return len(rel_parts) >= 2 and tuple(rel_parts[:2]) in STRUCTURED_DELETE_DIRS


def is_temp_dir_name(name):
    return name in TEMP_DIR_NAMES or any(name.startswith(prefix) for prefix in TEMP_DIR_PREFIXES)


def is_source_dir_component(part):
    return (
        part in SOURCE_DIR_NAMES
        or part.startswith("raw_")
        or part.endswith("_raw")
    )


def file_size(path):
    if path.is_symlink():
        return 0
    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:
            return 0
    total = 0
    for child in path.rglob("*"):
        if child.is_symlink() or not child.is_file():
            continue
        try:
            total += child.stat().st_size
        except OSError:
            continue
    return total


def is_source_artifact(path, rel_parts):
    lower_name = path.name.lower()
    suffix = path.suffix.lower()

    if any(is_source_dir_component(part.lower()) for part in rel_parts):
        return True
    if suffix in CODE_FILE_EXTS:
        return True
    if suffix in SOURCE_FILE_EXTS:
        return True
    if any(marker in lower_name for marker in SOURCE_NAME_MARKERS):
        return True
    if SOURCE_PREFIX_RE.match(lower_name) and suffix != ".dta":
        return True
    return False


def build_plan(project):
    plan = CleanupPlan()
    skip_roots = set()

    for root, dirs, files in os.walk(project, topdown=True):
        root_path = Path(root)
        try:
            rel_root = root_path.relative_to(project)
        except ValueError:
            continue

        # Stop descending into directories that will be removed wholesale.
        kept_dirs = []
        for dirname in dirs:
            child_rel = rel_root / dirname if rel_root != Path(".") else Path(dirname)
            child_parts = child_rel.parts
            if is_structured_delete_dir(child_parts) or is_temp_dir_name(dirname):
                plan.delete_dirs.append(project / child_rel)
                skip_roots.add(project / child_rel)
                continue
            kept_dirs.append(dirname)
        dirs[:] = kept_dirs

        if root_path in skip_roots:
            continue

        rel_parts = tuple(part.lower() for part in rel_root.parts if part != ".")
        in_data_tree = rel_parts[:1] == ("data",)
        in_analysis_tree = rel_parts[:1] == ("analysis",)

        for filename in files:
            path = root_path / filename
            rel_path = path.relative_to(project)
            rel_file_parts = tuple(part.lower() for part in rel_path.parts)

            if in_analysis_tree:
                # Preserve only explicitly raw/source artifacts inside legacy analysis trees.
                if is_source_artifact(path, rel_file_parts):
                    plan.keep_files.append(path)
                else:
                    plan.delete_files.append(path)
                continue

            if not in_data_tree:
                continue

            lower_name = filename.lower()
            if lower_name.endswith(".lock") or TMP_FILE_RE.search(lower_name):
                plan.delete_files.append(path)
                continue

            if is_source_artifact(path, rel_file_parts):
                plan.keep_files.append(path)
            else:
                plan.delete_files.append(path)

    return plan


def human_bytes(num_bytes):
    units = ["B", "K", "M", "G", "T"]
    value = float(num_bytes)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f}{unit}"
        value /= 1024
    return f"{num_bytes}B"


def dedupe_descendants(paths):
    result = []
    for path in sorted(paths):
        if any(parent == path or parent in path.parents for parent in result):
            continue
        result.append(path)
    return result


def execute_plan(plan, dry_run):
    dirs = dedupe_descendants(plan.delete_dirs)
    dir_set = set(dirs)
    files = [path for path in sorted(set(plan.delete_files)) if not any(parent in path.parents for parent in dir_set)]
    bytes_removed = sum(file_size(path) for path in dirs) + sum(file_size(path) for path in files)
    count_removed = len(dirs) + len(files)

    if dry_run:
        for path in dirs:
            print(f"DRY-RUN DIR  {path}")
        for path in files:
            print(f"DRY-RUN FILE {path}")
        return count_removed, bytes_removed, False

    had_errors = False
    for path in files:
        try:
            path.unlink()
            print(f"REMOVED FILE {path}")
        except FileNotFoundError:
            continue
        except OSError as exc:
            print(f"WARNING FILE {path}: {exc}", file=sys.stderr)
            had_errors = True
    for path in sorted(dirs, reverse=True):
        try:
            shutil.rmtree(path)
            print(f"REMOVED DIR  {path}")
        except FileNotFoundError:
            continue
        except OSError as exc:
            print(f"WARNING DIR  {path}: {exc}", file=sys.stderr)
            had_errors = True
    return count_removed, bytes_removed, had_errors


def prune_empty_dirs(project):
    for root, dirs, _files in os.walk(project, topdown=False):
        for dirname in dirs:
            path = Path(root) / dirname
            try:
                path.rmdir()
                print(f"REMOVED EMPTY {path}")
            except OSError:
                continue


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("projects", nargs="+", help="Project directories to clean")
    parser.add_argument("--dry-run", action="store_true", help="Report what would be deleted without deleting it")
    args = parser.parse_args(argv)

    total_count = 0
    total_bytes = 0
    exit_code = 0

    for project_arg in args.projects:
        project = Path(project_arg).resolve()
        if not project.is_dir():
            print(f"SKIP {project}: not a directory", file=sys.stderr)
            exit_code = 1
            continue

        print(f"==> Cleaning {project}")
        plan = build_plan(project)
        count_removed, bytes_removed, had_errors = execute_plan(plan, args.dry_run)
        if not args.dry_run:
            prune_empty_dirs(project)
        print(
            f"SUMMARY {project}: removed {count_removed} paths, "
            f"freed approximately {human_bytes(bytes_removed)}"
        )
        if had_errors:
            exit_code = 1
        total_count += count_removed
        total_bytes += bytes_removed

    print(
        f"TOTAL: removed {total_count} paths across {len(args.projects)} project(s), "
        f"freed approximately {human_bytes(total_bytes)}"
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
