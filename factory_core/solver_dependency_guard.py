"""Run a local Python producer with an audited project-file input closure.

This covers Python's audited file opens; it is not an OS sandbox or an
attestation of arbitrary native-library I/O. The receipt names that scope.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import runpy
import sys


def execute(project, script, inputs, outputs, args, report):
    project, script = project.resolve(), script.resolve()
    declared = {Path(p).resolve() for p in inputs} | {script}
    output_set = {(project / p).resolve() for p in outputs}
    for path in declared | output_set:
        if not path.is_relative_to(project):
            raise ValueError(f"solver dependency escapes project: {path}")
    hashes = {str(p.relative_to(project)): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in sorted(declared)}
    observed, violations = set(), set()
    active = True

    def audit(event, values):
        if not active or event != "open" or isinstance(values[0], int):
            return
        path = Path(os.fsdecode(values[0])).resolve()
        if not path.is_relative_to(project):
            return
        readable = (values[2] & os.O_ACCMODE) != os.O_WRONLY
        if readable and path not in declared and path not in output_set:
            relative = path.relative_to(project).as_posix()
            violations.add(relative)
            raise PermissionError(f"UNDECLARED_SOLVER_INPUT: {relative}; declare --input")
        if readable and path in declared:
            observed.add(path.relative_to(project).as_posix())

    sys.addaudithook(audit)
    code = 0
    try:
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
    changed = [p for p, sha in hashes.items()
               if not (project / p).is_file() or hashlib.sha256((project / p).read_bytes()).hexdigest() != sha]
    code = code or (2 if violations or changed else 0)
    payload = {"schema": "python-audit-open-v1", "exit_code": code,
               "status": "COMPLETE" if code == 0 else "FAILED",
               "declared_sha256": hashes, "observed_inputs": sorted(observed),
               "undeclared_inputs": sorted(violations), "changed_inputs": changed,
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
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    return execute(args.project, args.script, args.input, args.output,
                   args.args[1:] if args.args[:1] == ["--"] else args.args, args.report)


if __name__ == "__main__":
    raise SystemExit(main())
