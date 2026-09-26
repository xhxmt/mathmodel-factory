#!/usr/bin/env python3
"""Remove explicitly-declared rebuildable project data.

Fail-closed policy
==================

A path is removed only when *both* of these hold:

1. **It is an explicit deletion candidate.**  Either it lives under one of the
   directories in :data:`REBUILDABLE_DIRS`, or it lives under a directory whose
   name is declared temporary by :data:`TEMP_DIR_NAMES` /
   :data:`TEMP_DIR_PREFIXES`.  Nothing else is ever a candidate.

2. **No authoritative source protects it.**  Contract artifacts, machine
   evidence, canonical results and anything referenced by a persisted receipt
   are protected.

Anything this module does not positively recognise is kept.  In particular
``data/final``, ``analysis/final`` and ``replication/final`` hold canonical
results that feed the final input manifest, so they are deliberately **not**
rebuildable.  An earlier revision listed those directories for wholesale
removal by name alone, which could delete deliverable data before the manifest
that was supposed to prove it existed.

Deletion is additionally refused for a directory unless a recursive walk proves
that no member is protected.  A declared-rebuildable directory therefore can
never be removed "whole" while it still carries evidence or a referenced file.

The default mode only reports; pass ``--execute`` to delete.  Every run writes
``.factory/cleanup_report.json`` so the decision (policy fingerprint, candidates
and the paths that were protected) stays auditable after the fact.

Usage::

    python scripts/cleanup_project_artifacts.py PROJECT [PROJECT ...]
    python scripts/cleanup_project_artifacts.py --execute PROJECT
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path, PurePosixPath

# Keep the repository importable when the script is invoked by absolute path
# from an arbitrary working directory (the delivery runner does exactly that).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from factory_core.artifacts import ArtifactLayer, classify_artifact  # noqa: E402
from factory_core.cleanup_policy import (  # noqa: E402
    PROTECTED_NAMES,
    PROTECTED_TOP_LEVEL,
    REBUILDABLE_DIRS,
    TEMP_DIR_NAMES,
    TEMP_DIR_PREFIXES,
    TEMP_FILE_RE,
    policy_fingerprint,
)

REPORT_RELATIVE_PATH = PurePosixPath(".factory") / "cleanup_report.json"

# Receipts and manifests whose string values name files that must survive.
REFERENCE_SCAN_ROOTS = (".factory", "judge_outputs")
REFERENCE_SCAN_MAX_FILES = 200


class CleanupPlan(object):
    def __init__(self):
        self.delete_dirs = []
        self.delete_files = []
        self.keep_files = []
        # Effective protection set used to build this plan.  Removal re-verifies
        # against the same set, never against a weaker one.
        self.references = set()


def normalize_parts(parts):
    return tuple(part.lower() for part in parts if part not in ("", "."))


def in_rebuildable_dir(rel_parts):
    parts = normalize_parts(rel_parts)
    return len(parts) >= 2 and tuple(parts[:2]) in REBUILDABLE_DIRS


def is_temp_dir_name(name):
    lowered = name.lower()
    return lowered in TEMP_DIR_NAMES or any(
        lowered.startswith(prefix) for prefix in TEMP_DIR_PREFIXES
    )


def in_temp_dir(rel_parts):
    return any(is_temp_dir_name(part) for part in rel_parts)


def is_candidate_file(rel_parts):
    """A file is only ever considered for removal via an explicit declaration."""

    return in_temp_dir(rel_parts) or in_rebuildable_dir(rel_parts)


def _collect_strings(value, sink):
    if isinstance(value, str):
        text = value.strip()
        if not text or "://" in text or len(text) > 400:
            return
        sink.add(text)
        sink.add(text.lstrip("./"))
    elif isinstance(value, dict):
        for item in value.values():
            _collect_strings(item, sink)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _collect_strings(item, sink)


def receipt_referenced_paths(project, max_files=REFERENCE_SCAN_MAX_FILES):
    """Relative paths named by persisted receipts, manifests and decisions."""

    references = set()
    for relative in REFERENCE_SCAN_ROOTS:
        root = project / relative
        if not root.is_dir():
            continue
        for index, path in enumerate(sorted(root.rglob("*.json"))):
            if index >= max_files:
                break
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            _collect_strings(data, references)
    return references


def _relative_posix(project, path):
    try:
        return Path(path).resolve().relative_to(project).as_posix()
    except (OSError, ValueError):
        return None


def authoritative_protected_paths(project):
    """Exactly the paths the final input manifest would include.

    The ownership contract marks ``data/final/**`` as ``final_input=True``
    canonical results, while the broad ``data/intermediate/**`` rule is
    explicitly ``final_input=False`` so scratch data stays outside both
    manifests.  Hash-pinned intermediate files a solver declares are promoted
    back in by the solver coverage layer.  Asking those same APIs means cleanup
    cannot disagree with delivery about what is deliverable.

    Returns ``None`` when ownership cannot be evaluated; the caller must then
    keep everything.
    """

    project = Path(project).resolve()
    protected = set()
    try:
        from factory_core.artifact_policy import iter_policy_artifacts

        owned = list(iter_policy_artifacts(project, final_input_only=True))
    except Exception:
        return None
    for path in owned:
        relative = _relative_posix(project, path)
        if relative is not None:
            protected.add(relative)
    try:
        from factory_core.solver_input_coverage import solver_declared_input_coverage

        coverage = solver_declared_input_coverage(project)
        for path in tuple(coverage.included_paths) + tuple(coverage.evidence_paths):
            relative = _relative_posix(project, path)
            if relative is not None:
                protected.add(relative)
    except Exception:
        # Absent solver coverage adds no protection; candidates are unaffected.
        pass
    try:
        from factory_core.submission_bundle import submission_bundle_paths

        for path in submission_bundle_paths(project, project.name, require_pdf=False):
            relative = _relative_posix(project, path)
            if relative is not None:
                protected.add(relative)
    except Exception:
        # No safe LaTeX dependency graph (or no paper yet).  Its files live in
        # the paper tree, never in a rebuildable tree, so nothing is lost.
        pass
    return protected


def is_protected(rel_path, references):
    """Return True when an authoritative source protects ``rel_path``."""

    parts = normalize_parts(rel_path.parts)
    if not parts:
        return True
    if parts[0] in PROTECTED_TOP_LEVEL:
        return True
    lowered_name = rel_path.name.lower()
    if lowered_name in PROTECTED_NAMES:
        return True
    posix = PurePosixPath(*rel_path.parts).as_posix()
    if posix in references or posix.lstrip("./") in references:
        return True
    if lowered_name in references:
        return True
    # Receipts, snapshots and fingerprints are machine evidence regardless of
    # where they are parked.
    return classify_artifact(posix) is ArtifactLayer.MACHINE_EVIDENCE


def first_protected_member(project, directory, references):
    """First protected path inside ``directory``, or None when the tree is clear."""

    try:
        relative = directory.relative_to(project)
    except ValueError:
        return directory
    if is_protected(relative, references):
        return directory
    try:
        children = sorted(directory.rglob("*"))
    except OSError:
        return directory
    for child in children:
        if child.is_symlink():
            continue
        try:
            child_relative = child.relative_to(project)
        except ValueError:
            return child
        if is_protected(child_relative, references):
            return child
    return None


def build_plan(project, references=None):
    project = Path(project).resolve()
    authoritative = authoritative_protected_paths(project)
    if authoritative is None:
        # Fail closed: ownership could not be evaluated, so nothing may be
        # removed.  The caller still receives an explicit empty plan.
        return CleanupPlan()
    combined = set(authoritative)
    combined.update(references or receipt_referenced_paths(project))
    references = combined
    plan = CleanupPlan()
    plan.references = references

    for root, dirs, files in os.walk(project, topdown=True):
        root_path = Path(root)
        try:
            rel_root = root_path.relative_to(project)
        except ValueError:
            continue

        kept_dirs = []
        for dirname in sorted(dirs):
            child_rel = (rel_root / dirname) if rel_root != Path(".") else Path(dirname)
            child_parts = normalize_parts(child_rel.parts)
            if not (in_rebuildable_dir(child_parts) or is_temp_dir_name(dirname)):
                kept_dirs.append(dirname)
                continue
            child_path = project / child_rel
            if child_path.is_symlink():
                kept_dirs.append(dirname)
                continue
            blocker = first_protected_member(project, child_path, references)
            if blocker is not None:
                # Never remove a directory whole while it still carries
                # evidence, a canonical result or a referenced file.
                plan.keep_files.append(child_path)
                kept_dirs.append(dirname)
                continue
            plan.delete_dirs.append(child_path)
        dirs[:] = kept_dirs

        rel_root_parts = normalize_parts(rel_root.parts)
        for filename in sorted(files):
            path = root_path / filename
            rel_path = path.relative_to(project)
            if not is_candidate_file(rel_root_parts):
                continue
            if is_protected(rel_path, references):
                plan.keep_files.append(path)
                continue
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


def dedupe_descendants(paths):
    result = []
    for path in sorted(paths):
        if any(parent == path or parent in path.parents for parent in result):
            continue
        result.append(path)
    return result


def plan_payload(plan):
    return {
        "policy_sha256": policy_fingerprint(),
        "delete_dirs": [str(path) for path in dedupe_descendants(plan.delete_dirs)],
        "delete_files": [str(path) for path in sorted(set(plan.delete_files))],
        "protected": [str(path) for path in sorted(set(plan.keep_files))],
    }


def write_report(project, plan, executed):
    report = plan_payload(plan)
    report["executed"] = bool(executed)
    path = project / REPORT_RELATIVE_PATH
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    except OSError as exc:
        print(f"WARNING REPORT {path}: {exc}", file=sys.stderr)
        return None
    return path


def execute_plan(plan, project, references, execute):
    """Apply ``plan``.  Directories are re-verified immediately before removal."""

    # Re-verify against the protection set the plan was built with, so removal
    # can never run with a weaker veto than the one that authorised it.
    references = set(getattr(plan, "references", ()) or references)
    dirs = dedupe_descendants(plan.delete_dirs)
    dir_set = set(dirs)
    files = [
        path
        for path in sorted(set(plan.delete_files))
        if not any(parent in path.parents for parent in dir_set)
    ]
    bytes_removed = sum(file_size(path) for path in dirs) + sum(
        file_size(path) for path in files
    )
    count_removed = len(dirs) + len(files)

    if not execute:
        for path in dirs:
            print(f"WOULD REMOVE DIR  {path}")
        for path in files:
            print(f"WOULD REMOVE FILE {path}")
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
        # Re-check under the same predicate used to build the plan: a directory
        # that gained evidence since planning must not be removed.
        blocker = first_protected_member(Path(project), path, references)
        if blocker is not None:
            print(f"KEEP DIR {path}: protected member {blocker}", file=sys.stderr)
            had_errors = True
            continue
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
    """Remove empty directories only inside the declared rebuildable trees."""

    roots = [
        project.joinpath(*parts)
        for parts in sorted(REBUILDABLE_DIRS)
    ]
    for root in roots:
        if not root.is_dir():
            continue
        for walk_root, dirs, _files in os.walk(root, topdown=False):
            for dirname in dirs:
                path = Path(walk_root) / dirname
                try:
                    path.rmdir()
                    print(f"REMOVED EMPTY {path}")
                except OSError:
                    continue


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("projects", nargs="+", help="Project directories to clean")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually delete; without this flag the run only reports",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report only (default; kept for backward compatibility)",
    )
    args = parser.parse_args(argv)
    execute = args.execute and not args.dry_run

    total_count = 0
    total_bytes = 0
    exit_code = 0

    print(f"POLICY_SHA256 {policy_fingerprint()}")

    for project_arg in args.projects:
        project = Path(project_arg).resolve()
        if not project.is_dir():
            print(f"SKIP {project}: not a directory", file=sys.stderr)
            exit_code = 1
            continue

        mode = "execute" if execute else "report"
        print(f"==> Cleaning {project} ({mode})")
        references = receipt_referenced_paths(project)
        plan = build_plan(project, references=references)
        count_removed, bytes_removed, had_errors = execute_plan(
            plan, project, references, execute
        )
        if execute:
            prune_empty_dirs(project)
        report = write_report(project, plan, execute)
        if report is not None:
            print(f"REPORT {report}")
        print(
            f"SUMMARY {project}: {'removed' if execute else 'would remove'} "
            f"{count_removed} paths, "
            f"freed approximately {human_bytes(bytes_removed)}"
        )
        if had_errors:
            exit_code = 1
        total_count += count_removed
        total_bytes += bytes_removed

    print(
        f"TOTAL: {'removed' if execute else 'would remove'} {total_count} paths "
        f"across {len(args.projects)} project(s), "
        f"freed approximately {human_bytes(total_bytes)}"
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
