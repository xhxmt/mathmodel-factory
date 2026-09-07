"""Run a local Python producer with an audited project-file input closure.

This covers Python's audited file opens; it is not an OS sandbox or an
attestation of arbitrary native-library I/O. The receipt names that scope.
"""
from __future__ import annotations

import argparse
import builtins
import io
import hashlib
import json
import os
from pathlib import Path
import runpy
import sys


def execute(project, script, inputs, outputs, args, report, submission_receipt=None):
    project, script = project.resolve(), script.resolve()
    declared = {Path(p).resolve() for p in inputs} | {script}
    output_set = {(project / p).resolve() for p in outputs}
    for path in declared | output_set:
        if not path.is_relative_to(project):
            raise ValueError(f"solver dependency escapes project: {path}")
    hashes = {p.relative_to(project).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in sorted(declared)}
    initial_outputs = {p.relative_to(project).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                       for p in sorted(output_set) if p.is_file()}
    overlap = sorted(p.relative_to(project).as_posix() for p in declared & output_set)
    preflight_errors = []
    request_hash = None
    if submission_receipt is not None:
        from scripts.solver_job_receipt import read_receipt, SUBMISSION_SCHEMA, initial_input_records, _records_unchanged
        submitted = read_receipt(submission_receipt, SUBMISSION_SCHEMA)
        request_hash = submitted["request_sha256"]
        expected = {r["path"]: r["sha256"] for r in [submitted["script"], *submitted["inputs"]]}
        initial = {r["path"]: r["sha256"] for r in submitted.get("output_initial_state", []) if r["exists"]}
        if submitted.get("dependency_enforcement") != "python-audit-open-v2":
            preflight_errors.append("UNSUPPORTED_INPUT_LIFECYCLE")
        if hashes != expected or initial_outputs != initial:
            preflight_errors.append("INITIAL_INPUT_OR_OUTPUT_CHANGED_BEFORE_EXECUTION")
        if sorted(outputs) != sorted(submitted["declared_outputs"]):
            preflight_errors.append("OUTPUT_SET_CHANGED_BEFORE_EXECUTION")
        if not _records_unchanged(project, initial_input_records(submitted)):
            preflight_errors.append("INITIAL_INPUT_SNAPSHOT_CHANGED")
    observed, violations, generated_reads = set(), set(), set()
    generated = set()
    active = True

    def project_path(value):
        if isinstance(value, int):
            return None
        path = Path(os.fsdecode(value)).resolve()
        return path if path.is_relative_to(project) else None

    def audit(event, values):
        if not active or event != "open":
            return
        path = project_path(values[0])
        if path is None:
            return
        flags = values[2]
        readable = (flags & os.O_ACCMODE) != os.O_WRONLY
        relative = path.relative_to(project).as_posix()
        if path in output_set:
            # Non-truncating access to an old output (including append) can
            # consume its initial bytes. It must be an explicitly declared
            # input, whose immutable snapshot is bound by the submission.
            initial_access = relative in initial_outputs and path not in generated and not flags & os.O_TRUNC
            if initial_access and path not in declared:
                violations.add(relative)
                raise PermissionError(f"UNDECLARED_INITIAL_OUTPUT_INPUT: {relative}; declare --input and --output")
            if readable and path in generated:
                generated_reads.add(relative)
            if initial_access and path in declared:
                observed.add(relative)
        elif readable and path not in declared:
            violations.add(relative)
            raise PermissionError(f"UNDECLARED_SOLVER_INPUT: {relative}; declare --input")
        if readable and path in declared:
            observed.add(relative)

    # Mark a fresh intermediate only after a successful truncating/create open.
    # An attempted open that raises cannot retire an initial input obligation.
    original_open, original_io_open, original_os_open = builtins.open, io.open, os.open
    original_replace, original_rename = os.replace, os.rename

    def text_open(original):
        def wrapped(file, *positional, **kwargs):
            handle = original(file, *positional, **kwargs)
            path = project_path(file)
            mode = kwargs.get("mode", positional[0] if positional else "r")
            opener = kwargs.get("opener", positional[6] if len(positional) > 6 else None)
            if path is not None and opener is None and isinstance(mode, str):
                if "w" in mode or "x" in mode or ("a" in mode and path.relative_to(project).as_posix() not in initial_outputs):
                    generated.add(path)
            return handle
        return wrapped

    def fd_open(path, flags, *positional, **kwargs):
        fd = original_os_open(path, flags, *positional, **kwargs)
        resolved = project_path(path)
        if resolved is not None and (flags & os.O_TRUNC or
                (flags & os.O_CREAT and resolved.relative_to(project).as_posix() not in initial_outputs)):
            generated.add(resolved)
        return fd

    def move(original):
        def wrapped(src, dst, *positional, **kwargs):
            source, destination = project_path(src), project_path(dst)
            result = original(src, dst, *positional, **kwargs)
            if source in generated and destination is not None:
                generated.add(destination)
            return result
        return wrapped

    sys.addaudithook(audit)
    builtins.open, io.open, os.open = text_open(original_open), text_open(original_io_open), fd_open
    os.replace, os.rename = move(original_replace), move(original_rename)
    code = 0
    try:
        if preflight_errors:
            raise ValueError(", ".join(preflight_errors))
        sys.argv = [str(script), *args]
        sys.path.insert(0, str(script.parent))
        runpy.run_path(str(script), run_name="__main__")
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1 if exc.code else 0
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        code = 2
    finally:
        active = False
        builtins.open, io.open, os.open = original_open, original_io_open, original_os_open
        os.replace, os.rename = original_replace, original_rename
    changed = [p for p, sha in hashes.items() if p not in overlap
               and (not (project / p).is_file() or hashlib.sha256((project / p).read_bytes()).hexdigest() != sha)]
    code = code or (2 if violations or changed else 0)
    payload = {"schema": "python-audit-open-v2", "exit_code": code,
               "status": "COMPLETE" if code == 0 else "FAILED",
               "declared_sha256": hashes, "observed_inputs": sorted(observed),
               "undeclared_inputs": sorted(violations), "changed_inputs": changed,
               "submission_request_sha256": request_hash, "initial_output_sha256": initial_outputs,
               "input_output_paths": overlap, "generated_output_reads": sorted(generated_reads),
               "preflight_errors": preflight_errors,
               "scope": "PYTHON_AUDITED_PROJECT_FILE_OPENS"}
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(payload, sort_keys=True) + "\n")
    return code


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--script", type=Path, required=True)
    parser.add_argument("--input", type=Path, action="append", default=[])
    parser.add_argument("--output", action="append", default=[])
    parser.add_argument("--submission-receipt", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    return execute(args.project, args.script, args.input, args.output,
                   args.args[1:] if args.args[:1] == ["--"] else args.args, args.report, args.submission_receipt)


if __name__ == "__main__":
    raise SystemExit(main())
