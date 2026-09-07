"""Frozen native judge calls. A response alone is never reusable evidence."""
from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path


class JudgeBatchError(ValueError):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def record(path):
    if path.is_symlink() or not path.is_file():
        raise JudgeBatchError(f"required regular batch input missing: {path}")
    data = path.read_bytes()
    return {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}


def descriptor(project, root, step_id, role, prompt):
    from scripts.submission_fingerprint import evaluator_contract_payload

    paths = ["judge_packets/objective_evidence.json"]
    paths += [f"judge_packets/{r}/{name}" for r in ("math", "execution", "paper")
              for name in ("context.txt", "manifest.json")]
    return {"schema": "judge-batch-v1", "project": str(project.resolve()), "factory_root": str(root.resolve()),
            "execution_step_id": step_id, "template_step_id": 13, "role": role,
            "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
            "inputs": {p: record(project / p) for p in paths},
            "evaluator": evaluator_contract_payload(project.name, root, execution_step_id=step_id)}


def begin(project, expected):
    call_id = uuid.uuid4().hex
    relative = f"judge_outputs/batches/{digest(expected)}/{call_id}"
    folder = project / relative
    folder.mkdir(parents=True, exist_ok=False)
    (folder / "request.json").write_text(json.dumps(expected, sort_keys=True) + "\n")
    for index, (path, info) in enumerate(sorted(expected["inputs"].items())):
        data = (project / path).read_bytes()
        if {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)} != info:
            raise JudgeBatchError(f"batch input changed before call: {path}")
        (folder / f"input-{index}").write_bytes(data)
    return {"schema": "judge-call-v1", "batch_id": digest(expected),
            "call_id": call_id, "archive": relative}


def commit(project, binding, expected, *, exit_code, prompt_path, response_path, metadata_path):
    if exit_code != 0:
        raise JudgeBatchError(f"judge call exited {exit_code}; evidence is not committed")
    for path, info in expected["inputs"].items():
        if record(project / path) != info:
            raise JudgeBatchError(f"batch input changed during call: {path}")
    if record(prompt_path)["sha256"] != expected["prompt_sha256"]:
        raise JudgeBatchError("rendered prompt changed during call")
    metadata = json.loads(metadata_path.read_text())
    if (metadata.get("response_sha256") != record(response_path)["sha256"]
            or metadata.get("execution_step_id") != expected["execution_step_id"]
            or metadata.get("template_step_id") != 13):
        raise JudgeBatchError("response receipt or execution identity mismatch")
    folder = project / binding["archive"]
    files = {}
    for name, path in (("response", response_path), ("prompt", prompt_path), ("metadata", metadata_path)):
        data = path.read_bytes()
        files[name] = {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        with (folder / name).open("xb") as handle:
            handle.write(data)
    seal = {"binding": binding, "request": expected, "exit_code": 0, "files": files}
    # The exclusive final marker is written last. Partial attempts have no seal.
    with (folder / "committed.json").open("x") as handle:
        json.dump(seal, handle, sort_keys=True)
    return {**binding, "seal_sha256": record(folder / "committed.json")["sha256"]}


def verify(project, binding, expected=None):
    """Validate frozen bytes and current inputs before any reuse or aggregation."""
    try:
        relative = f"judge_outputs/batches/{binding['batch_id']}/{binding['call_id']}"
        if binding["archive"] != relative or not all(c in "0123456789abcdef" for c in binding["call_id"]):
            raise JudgeBatchError("invalid call archive identity")
        folder = project / relative
        if folder.resolve().is_relative_to(project.resolve()) is False:
            raise JudgeBatchError("call archive escapes project")
        if record(folder / "committed.json")["sha256"] != binding["seal_sha256"]:
            raise JudgeBatchError("call seal changed")
        seal = json.loads((folder / "committed.json").read_text())
        request = seal["request"]
        if descriptor(project, Path(request["factory_root"]), request["execution_step_id"],
                      request["role"], (folder / "prompt").read_text()) != request:
            raise JudgeBatchError("current evaluator/input differs from frozen batch")
        if digest(request) != binding["batch_id"] or seal["exit_code"] != 0:
            raise JudgeBatchError("batch identity or exit status mismatch")
        if expected is not None and request != expected:
            raise JudgeBatchError("request/configuration belongs to a different batch")
        for name, info in seal["files"].items():
            if name not in ("response", "prompt", "metadata") or record(folder / name) != info:
                raise JudgeBatchError("frozen call file changed")
        if set(seal["files"]) != {"response", "prompt", "metadata"}:
            raise JudgeBatchError("incomplete call seal")
        for index, (path, info) in enumerate(sorted(request["inputs"].items())):
            if record(project / path) != info or record(folder / f"input-{index}") != info:
                raise JudgeBatchError(f"batch input mismatch: {path}")
        return (folder / "response").read_bytes(), json.loads((folder / "metadata").read_text())
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise JudgeBatchError(f"invalid judge batch evidence: {exc}") from exc
