"""Focused contract tests for the independent objective-evidence layer."""

from __future__ import annotations

import hashlib
import os
import sys
import time
from pathlib import Path

import pytest

from scripts.objective_evidence import (
    EvidenceBundle,
    EvidenceRef,
    ObjectiveEvidenceError,
    UnsafeCommandError,
    build_bundle,
    command_finding,
    evidence_ref,
    fingerprint_paths,
    make_finding,
    run_safe_command,
    validate_finding,
)


def _script(root: Path, name: str, source: str) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return path


def _finding(root: Path, finding_id: str = "f1", **kwargs):
    return make_finding(
        finding_id=finding_id,
        claim_id=kwargs.pop("claim_id", "claim-1"),
        checker_id=kwargs.pop("checker_id", "test.checker"),
        checker_version=kwargs.pop("checker_version", "1"),
        root=root,
        **kwargs,
    )


def test_fingerprint_is_content_deterministic_and_mtime_independent(tmp_path):
    source = tmp_path / "paper.txt"
    source.write_text("alpha\n", encoding="utf-8")
    first = fingerprint_paths(tmp_path)

    # Metadata changes must not turn into a new input snapshot.
    now = time.time() + 3600
    os.utime(source, (now, now))
    assert fingerprint_paths(tmp_path) == first

    source.write_text("beta\n", encoding="utf-8")
    assert fingerprint_paths(tmp_path) != first


def test_evidence_ref_contains_relative_path_hash_and_locator(tmp_path):
    source = tmp_path / "results" / "numbers.json"
    source.parent.mkdir()
    payload = b'{"answer": 42}\n'
    source.write_bytes(payload)

    ref = evidence_ref(tmp_path, source, locator={"line": 1, "claim": "answer"})
    assert ref.path == "results/numbers.json"
    assert ref.sha256 == hashlib.sha256(payload).hexdigest()
    assert ref.locator["line"] == 1

    missing = evidence_ref(tmp_path, "missing.txt")
    assert missing.sha256 is None
    with pytest.raises(ObjectiveEvidenceError):
        evidence_ref(tmp_path, ".")


def test_path_traversal_and_external_symlink_are_rejected(tmp_path):
    with pytest.raises(ObjectiveEvidenceError):
        evidence_ref(tmp_path, "../outside.txt")
    with pytest.raises(ObjectiveEvidenceError):
        evidence_ref(tmp_path, "nested/../inside.txt")
    for serialized_path in (
        "",
        ".",
        "../outside.txt",
        "folder/../outside.txt",
        "folder/./file.txt",
        "folder//file.txt",
        "folder/",
        "/etc/passwd",
        "folder\\escape.txt",
        "folder/evil\x00.txt",
    ):
        with pytest.raises(ObjectiveEvidenceError):
            EvidenceRef.from_dict({"path": serialized_path, "sha256": None})

    outside = tmp_path.parent / "objective-evidence-outside.txt"
    outside.write_text("outside", encoding="utf-8")
    link = tmp_path / "escape.txt"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    with pytest.raises(ObjectiveEvidenceError):
        evidence_ref(tmp_path, link)


def test_only_exact_trusted_contradiction_can_be_hard_veto(tmp_path):
    finding = _finding(
        tmp_path,
        status="FAIL",
        severity="hard_veto",
        trust_level="factory_oracle",
        exact_contradiction=True,
        observed=0,
        expected=1,
    )
    assert finding.status == "FAIL"
    assert finding.severity == "hard_veto"
    assert finding.downgraded_from is None
    assert validate_finding(finding) == finding

    # A caller cannot promote a heuristic or project-owned assertion.
    for trust in ("heuristic", "project_test", "self_report"):
        downgraded = _finding(
            tmp_path,
            finding_id=f"{trust}-fail",
            status="FAIL",
            severity="hard_veto",
            trust_level=trust,
            exact_contradiction=True,
        )
        assert (downgraded.status, downgraded.severity) == ("WARN", "soft_alert")
        assert downgraded.downgraded_from == {"status": "FAIL", "severity": "hard_veto"}


@pytest.mark.parametrize("reason", ["missing", "ambiguous", "stale", "error"])
def test_missing_ambiguous_stale_or_error_is_unknown(tmp_path, reason):
    finding = _finding(
        tmp_path,
        finding_id=reason,
        status="FAIL",
        severity="hard_veto",
        trust_level="factory_oracle",
        exact_contradiction=True,
        reason=reason,
    )
    assert finding.status == "UNKNOWN"
    assert finding.severity == "soft_alert"


def test_not_applicable_is_skip(tmp_path):
    finding = _finding(
        tmp_path,
        status="FAIL",
        severity="hard_veto",
        trust_level="factory_oracle",
        applicability="not_applicable",
        exact_contradiction=True,
    )
    assert finding.status == "SKIP"
    assert finding.severity == "info"
    assert finding.reason == "not_applicable"


def test_bundle_decision_precedence_and_empty_semantics(tmp_path):
    passing = _finding(tmp_path, finding_id="pass", status="PASS")
    unknown = _finding(tmp_path, finding_id="unknown", status="UNKNOWN")
    hard_fail = _finding(
        tmp_path,
        finding_id="hard",
        status="FAIL",
        severity="hard_veto",
        trust_level="dual_impl",
        exact_contradiction=True,
    )

    assert build_bundle(tmp_path, [passing, unknown]).summary["decision"] == "INDETERMINATE"
    assert build_bundle(tmp_path, [passing, unknown, hard_fail]).summary["decision"] == "FAIL"
    assert build_bundle(tmp_path, [passing]).summary["decision"] == "PASS"
    assert build_bundle(tmp_path, []).summary["decision"] == "INDETERMINATE"
    skipped = _finding(tmp_path, finding_id="skip", status="SKIP", applicability="not_applicable")
    assert build_bundle(tmp_path, [skipped]).summary["decision"] == "INDETERMINATE"

    duplicate = build_bundle(tmp_path, [passing, passing])
    assert len(duplicate.findings) == 1


def test_safe_command_allowlist_and_command_finding(tmp_path):
    script = _script(tmp_path, "scripts/pass.py", "print('ok')\n")
    result = run_safe_command(
        [sys.executable, "scripts/pass.py"],
        cwd=tmp_path,
        allowed_roots=[tmp_path],
    )
    assert result.ok
    assert result.stdout.strip() == "ok"

    for argv in (
        [sys.executable, "-c", "print('bad')"],
        [sys.executable, "-m", "http.server"],
        ["sh", "-c", "echo bad"],
    ):
        with pytest.raises(UnsafeCommandError):
            run_safe_command(argv, cwd=tmp_path, allowed_roots=[tmp_path])

    fake_python = _script(tmp_path, "python3", "#!/bin/sh\necho fake\n")
    fake_python.chmod(0o755)
    with pytest.raises(UnsafeCommandError):
        run_safe_command(
            [str(fake_python), str(script)], cwd=tmp_path, allowed_roots=[tmp_path]
        )

    outside = _script(tmp_path.parent, "not-allowed.py", "print('no')\n")
    with pytest.raises(UnsafeCommandError):
        run_safe_command(
            [sys.executable, str(outside)],
            cwd=tmp_path,
            allowed_roots=[tmp_path],
        )
    with pytest.raises(UnsafeCommandError):
        run_safe_command(
            [sys.executable, str(script)],
            cwd=tmp_path.parent,
            allowed_roots=[tmp_path],
        )
    with pytest.raises(UnsafeCommandError):
        run_safe_command(
            [sys.executable, str(script)],
            cwd=tmp_path,
            allowed_roots=[tmp_path],
            timeout=True,
        )

    nonzero = _script(tmp_path, "scripts/fail.py", "raise SystemExit(7)\n")
    failed = run_safe_command(
        [sys.executable, str(nonzero)], cwd=tmp_path, allowed_roots=[tmp_path]
    )
    finding = command_finding(
        finding_id="cmd-fail",
        claim_id="claim-1",
        checker_id="oracle",
        checker_version="1",
        result=failed,
        root=tmp_path,
        input_paths=[nonzero],
    )
    assert finding.status == "UNKNOWN"
    assert finding.reason == "command_error"

    veto = command_finding(
        finding_id="cmd-veto",
        claim_id="claim-1",
        checker_id="oracle",
        checker_version="1",
        result=failed,
        root=tmp_path,
        input_paths=[nonzero],
        exact_contradiction=True,
    )
    assert (veto.status, veto.severity) == ("FAIL", "hard_veto")


def test_safe_command_timeout_and_output_truncation(tmp_path):
    slow = _script(tmp_path, "slow.py", "import time; time.sleep(1)\n")
    timed = run_safe_command(
        [sys.executable, str(slow)],
        cwd=tmp_path,
        allowed_roots=[tmp_path],
        timeout=0.05,
    )
    assert timed.timed_out
    assert timed.returncode == 124

    noisy = _script(tmp_path, "noisy.py", "print('x' * 5000)\n")
    clipped = run_safe_command(
        [sys.executable, str(noisy)],
        cwd=tmp_path,
        allowed_roots=[tmp_path],
        max_output_bytes=100,
    )
    assert clipped.output_truncated
    assert len(clipped.stdout.encode("utf-8")) <= 100

    unicode_noisy = _script(tmp_path, "unicode_noisy.py", "print('测' * 100)\n")
    unicode_clipped = run_safe_command(
        [sys.executable, str(unicode_noisy)],
        cwd=tmp_path,
        allowed_roots=[tmp_path],
        max_output_bytes=101,
    )
    assert unicode_clipped.output_truncated
    assert len(unicode_clipped.stdout.encode("utf-8")) <= 101


def test_bundle_round_trip_is_json_serializable(tmp_path):
    source = tmp_path / "evidence.txt"
    source.write_text("evidence", encoding="utf-8")
    finding = _finding(
        tmp_path,
        status="PASS",
        evidence=[EvidenceRef.from_dict(evidence_ref(tmp_path, source, "line 1").to_dict())],
        input_paths=[source],
        observed={"value": 1},
        expected={"value": 1},
    )
    bundle = build_bundle(tmp_path, [finding], metadata={"source": "test"})
    restored = EvidenceBundle.from_dict(bundle.to_dict())
    assert restored.to_dict() == bundle.to_dict()
