from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from scripts.evidence_grounding import validate_grounding, validate_grounding_bytes


REPO_ROOT = Path(__file__).resolve().parents[1]
DIMENSIONS = (
    "model_presentation",
    "solution_narrative",
    "innovation",
    "writing_clarity",
    "result_persuasiveness",
    "sensitivity_limitations",
)


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _write_json(path: Path, value: object) -> Path:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return path


def _packet(
    tmp_path: Path,
    *,
    role: str = "math",
    path: str = "evidence.txt",
    content: str = "intro\nunique evidence quote\noutro\n",
    source_line_start: int = 1,
    status: str = "included",
) -> dict[str, object]:
    included = content.encode("utf-8")
    included_sha256 = _sha256(included)
    chunk_id = _sha256(f"{role}\0{path}\0{included_sha256}".encode("utf-8"))
    context_text = f"\n----- FILE: {path} -----\n{content}\n"
    context_path = tmp_path / "context.txt"
    context_path.write_text(context_text, encoding="utf-8", newline="\n")
    files = [
        {
            "path": path,
            "status": status,
            "included_sha256": included_sha256,
            "included_bytes": len(included),
            "chunk_id": chunk_id,
            "source_line_start": source_line_start,
        }
    ]
    manifest = {
        "role": role,
        "files": files,
        "context": {
            "sha256": _sha256(context_path.read_bytes()),
            "size": context_path.stat().st_size,
        },
    }
    manifest_path = _write_json(tmp_path / f"{role}.manifest.json", manifest)
    return {
        "role": role,
        "role_path": tmp_path / f"{role}.md",
        "manifest_path": manifest_path,
        "context_path": context_path,
        "manifest": manifest,
        "path": path,
        "content": content,
        "chunk_id": chunk_id,
    }


def _write_hard_role(packet: dict[str, object], evidence: list[dict[str, str]]) -> Path:
    role = str(packet["role"])
    payload = {
        "schema_version": "judge-hard-role-v2",
        "role": role,
        "verdict": "PASS",
        "fatal_flaws": 0,
        "evidence": evidence,
        "limitations": [],
        "conclusion": "grounded conclusion",
    }
    role_path = Path(packet["role_path"])
    role_path.write_text(
        f"VERDICT: PASS\n{json.dumps(payload, ensure_ascii=False)}\n",
        encoding="utf-8",
    )
    return role_path


def _hard_reference(
    packet: dict[str, object],
    *,
    ref_id: str = "math-ref-1",
    chunk_id: str | None = None,
    quote: str = "unique evidence quote",
    quote_sha256: str | None = None,
) -> dict[str, str]:
    reference = {
        "ref_id": ref_id,
        "claim": "a grounded claim",
        "chunk_id": chunk_id or str(packet["chunk_id"]),
        "quote": quote,
        "finding": "the quote supports the claim",
        "severity": "support",
    }
    if quote_sha256 is not None:
        reference["quote_sha256"] = quote_sha256
    return reference


def _validate(packet: dict[str, object], *, role: str | None = None) -> dict:
    return validate_grounding(
        Path(packet["role_path"]),
        Path(packet["manifest_path"]),
        Path(packet["context_path"]),
        role=role or str(packet["role"]),
    )


def _save_manifest(packet: dict[str, object]) -> None:
    _write_json(Path(packet["manifest_path"]), packet["manifest"])


def _refresh_context_declaration(packet: dict[str, object]) -> None:
    context_path = Path(packet["context_path"])
    manifest = packet["manifest"]
    assert isinstance(manifest, dict)
    manifest["context"] = {
        "sha256": _sha256(context_path.read_bytes()),
        "size": context_path.stat().st_size,
    }
    _save_manifest(packet)


def _packet_error_code(report: dict) -> str:
    assert report["valid"] is False
    assert report["errors"][0]["ref_id"] == "__packet__"
    return report["errors"][0]["code"]


def test_valid_hard_role_binds_packet_and_maps_source_and_context_lines(tmp_path):
    packet = _packet(
        tmp_path,
        content="intro\nunique evidence quote\ncontinued evidence\noutro\n",
        source_line_start=10,
        status="truncated",
    )
    quote = "unique evidence quote\ncontinued evidence"
    _write_hard_role(
        packet,
        [
            _hard_reference(
                packet,
                quote=quote,
                quote_sha256=_sha256(quote.encode("utf-8")),
            )
        ],
    )

    report = _validate(packet)

    assert report["schema_version"] == "evidence-grounding-v1"
    assert report["role"] == "math"
    assert report["valid"] is True
    assert report["errors"] == []
    assert report["context"]["sha256"] == _sha256(
        Path(packet["context_path"]).read_bytes()
    )
    assert report["context"]["size"] == Path(packet["context_path"]).stat().st_size
    assert report["refs"] == [
        {
            "ref_id": "math-ref-1",
            "chunk_id": packet["chunk_id"],
            "quote_sha256": _sha256(quote.encode("utf-8")),
            "resolved_path": "evidence.txt",
            "line_start": 11,
            "line_end": 12,
            "source_line_start": 11,
            "source_line_end": 12,
            "context_line_start": 4,
            "context_line_end": 5,
        }
    ]


def test_exact_bytes_api_matches_grounding_without_reading_source_paths(tmp_path):
    packet = _packet(tmp_path)
    _write_hard_role(packet, [_hard_reference(packet)])

    report = validate_grounding_bytes(
        Path(packet["role_path"]).read_bytes(),
        Path(packet["manifest_path"]).read_bytes(),
        Path(packet["context_path"]).read_bytes(),
        role="math",
    )
    expected = _validate(packet)

    assert report == {
        **expected,
        "manifest": {**expected["manifest"], "path": "manifest"},
        "context": {**expected["context"], "path": "context"},
    }
    Path(packet["role_path"]).unlink()
    Path(packet["manifest_path"]).unlink()
    Path(packet["context_path"]).unlink()
    assert report["valid"] is True


def test_quote_leading_and_trailing_whitespace_is_identity_bearing(tmp_path):
    quote = "  exact quote bytes  "
    packet = _packet(
        tmp_path,
        content=f"intro\n{quote}\noutro\n",
    )
    _write_hard_role(
        packet,
        [
            _hard_reference(
                packet,
                quote=quote,
                quote_sha256=_sha256(quote.encode("utf-8")),
            )
        ],
    )

    report = validate_grounding_bytes(
        Path(packet["role_path"]).read_bytes(),
        Path(packet["manifest_path"]).read_bytes(),
        Path(packet["context_path"]).read_bytes(),
        role="math",
    )

    assert report["valid"] is True
    assert report["refs"][0]["quote_sha256"] == _sha256(quote.encode("utf-8"))
    assert report["refs"][0]["quote_sha256"] != _sha256(
        quote.strip().encode("utf-8")
    )


def test_exact_bytes_api_returns_structured_invalid_report_for_non_bytes():
    report = validate_grounding_bytes(  # type: ignore[arg-type]
        "not-bytes", b"{}", b"", role="math"
    )

    assert report["valid"] is False
    assert report["errors"][0]["code"] == "GROUNDING_INPUT_INVALID"


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    (("unknown_chunk", "UNKNOWN_CHUNK"), ("quote_mismatch", "QUOTE_NOT_FOUND")),
)
def test_unknown_chunk_or_quote_mismatch_is_invalid(tmp_path, mutation, expected_code):
    packet = _packet(tmp_path)
    reference = _hard_reference(packet)
    if mutation == "unknown_chunk":
        reference["chunk_id"] = "f" * 64
    else:
        reference["quote"] = "quote absent from the active chunk"
    _write_hard_role(packet, [reference])

    report = _validate(packet)

    assert report["valid"] is False
    assert expected_code in {item["code"] for item in report["errors"]}


def test_duplicate_ref_id_is_invalid_after_normalization(tmp_path):
    packet = _packet(tmp_path)
    _write_hard_role(
        packet,
        [
            _hard_reference(packet, ref_id="shared-ref"),
            _hard_reference(packet, ref_id=" shared-ref "),
        ],
    )

    report = _validate(packet)

    assert report["valid"] is False
    assert {item["code"] for item in report["errors"]} == {"DUPLICATE_REF_ID"}


def test_optional_quote_hash_must_match_exact_quote(tmp_path):
    packet = _packet(tmp_path)
    _write_hard_role(
        packet,
        [_hard_reference(packet, quote_sha256="0" * 64)],
    )

    report = _validate(packet)

    assert report["valid"] is False
    assert report["errors"][0]["code"] == "QUOTE_HASH_MISMATCH"


@pytest.mark.parametrize(
    ("duplicate_kind", "expected_code"),
    (
        ("path", "DUPLICATE_ACTIVE_PATH"),
        ("chunk_id", "DUPLICATE_ACTIVE_CHUNK_ID"),
    ),
)
def test_duplicate_active_path_or_chunk_id_is_explicitly_invalid(
    tmp_path, duplicate_kind, expected_code
):
    packet = _packet(tmp_path)
    manifest = packet["manifest"]
    assert isinstance(manifest, dict)
    duplicate = dict(manifest["files"][0])
    if duplicate_kind == "path":
        duplicate["chunk_id"] = "f" * 64
    else:
        duplicate["path"] = "other-evidence.txt"
    manifest["files"].append(duplicate)
    _save_manifest(packet)
    _write_hard_role(packet, [_hard_reference(packet)])

    assert _packet_error_code(_validate(packet)) == expected_code


@pytest.mark.parametrize(
    ("case", "expected_code"),
    (
        ("duplicate", "DUPLICATE_CONTEXT_SECTION"),
        ("missing", "MISSING_CONTEXT_SECTION"),
        ("undeclared", "UNDECLARED_CONTEXT_SECTION"),
    ),
)
def test_context_sections_reject_duplicate_missing_and_undeclared(
    tmp_path, case, expected_code
):
    packet = _packet(tmp_path)
    context_path = Path(packet["context_path"])
    if case == "duplicate":
        context_path.write_text(
            context_path.read_text(encoding="utf-8")
            + f"\n----- FILE: {packet['path']} -----\n{packet['content']}\n",
            encoding="utf-8",
            newline="\n",
        )
    elif case == "undeclared":
        context_path.write_text(
            context_path.read_text(encoding="utf-8")
            + "\n----- FILE: undeclared.txt -----\nundeclared content\n",
            encoding="utf-8",
            newline="\n",
        )
    else:
        context_path.write_text("", encoding="utf-8", newline="\n")
    _refresh_context_declaration(packet)
    _write_hard_role(packet, [_hard_reference(packet)])

    assert _packet_error_code(_validate(packet)) == expected_code


@pytest.mark.parametrize(
    ("case", "expected_code"),
    (("hash", "CONTEXT_HASH_MISMATCH"), ("size", "CONTEXT_SIZE_MISMATCH")),
)
def test_context_hash_and_raw_byte_size_are_both_bound(tmp_path, case, expected_code):
    packet = _packet(tmp_path)
    manifest = packet["manifest"]
    assert isinstance(manifest, dict)
    if case == "hash":
        context_path = Path(packet["context_path"])
        context_path.write_text(
            context_path.read_text(encoding="utf-8") + "changed",
            encoding="utf-8",
            newline="\n",
        )
    else:
        manifest["context"]["size"] += 1
        _save_manifest(packet)
    _write_hard_role(packet, [_hard_reference(packet)])

    assert _packet_error_code(_validate(packet)) == expected_code


@pytest.mark.parametrize(
    ("field", "bad_value", "expected_code"),
    (
        ("path", "bad\npath", "ACTIVE_CHUNK_PATH_INVALID"),
        ("chunk_id", "A" * 64, "ACTIVE_CHUNK_ID_INVALID"),
        ("included_sha256", "A" * 64, "ACTIVE_CHUNK_HASH_INVALID"),
        ("included_bytes", True, "ACTIVE_CHUNK_BYTES_INVALID"),
        ("source_line_start", True, "ACTIVE_CHUNK_SOURCE_LINE_INVALID"),
    ),
)
def test_active_chunk_fields_are_strictly_validated(
    tmp_path, field, bad_value, expected_code
):
    packet = _packet(tmp_path)
    manifest = packet["manifest"]
    assert isinstance(manifest, dict)
    manifest["files"][0][field] = bad_value
    _save_manifest(packet)
    _write_hard_role(packet, [_hard_reference(packet)])

    assert _packet_error_code(_validate(packet)) == expected_code


@pytest.mark.parametrize(
    ("field", "bad_value", "expected_code"),
    (
        ("included_sha256", "f" * 64, "CONTEXT_SECTION_HASH_MISMATCH"),
        ("included_bytes", 999, "CONTEXT_SECTION_SIZE_MISMATCH"),
    ),
)
def test_included_section_hash_and_byte_count_must_both_match(
    tmp_path, field, bad_value, expected_code
):
    packet = _packet(tmp_path)
    manifest = packet["manifest"]
    assert isinstance(manifest, dict)
    manifest["files"][0][field] = bad_value
    _save_manifest(packet)
    _write_hard_role(packet, [_hard_reference(packet)])

    assert _packet_error_code(_validate(packet)) == expected_code


def test_paper_dimension_evidence_and_issues_share_grounding_path(tmp_path):
    quotes = [f"paper evidence quote {dimension}" for dimension in DIMENSIONS]
    issue_quote = "paper issue exact quote"
    packet = _packet(
        tmp_path,
        role="paper",
        path="paper.tex",
        content="\n".join([*quotes, issue_quote]) + "\n",
    )
    chunk_id = str(packet["chunk_id"])
    dimensions = {
        dimension: {
            "score": 1,
            "evidence": [
                {
                    "ref_id": f"paper-{dimension}",
                    "chunk_id": chunk_id,
                    "quote": quote,
                    "finding": "dimension evidence",
                }
            ],
        }
        for dimension, quote in zip(DIMENSIONS, quotes)
    }
    payload = {
        "schema_version": "judge-paper-role-v3",
        "role": "paper",
        "verdict": "PASS",
        "dimensions": dimensions,
        "overall_score": 6,
        "issues": [
            {
                "ref_id": "paper-issue-1",
                "severity": "minor",
                "chunk_id": chunk_id,
                "quote": issue_quote,
                "finding": "minor issue",
                "recommendation": "clarify the sentence",
            }
        ],
        "limitations": [],
        "recommendations": [],
        "conclusion": "paper conclusion",
    }
    Path(packet["role_path"]).write_text(
        f"VERDICT: PASS\n{json.dumps(payload, ensure_ascii=False)}\n",
        encoding="utf-8",
    )

    report = _validate(packet)

    assert report["valid"] is True
    assert len(report["refs"]) == 7
    assert {item["resolved_path"] for item in report["refs"]} == {"paper.tex"}
    assert {item["ref_id"] for item in report["refs"]} == {
        *(f"paper-{dimension}" for dimension in DIMENSIONS),
        "paper-issue-1",
    }


@pytest.mark.parametrize(
    ("case", "expected_code"),
    (
        ("manifest_json", "MANIFEST_JSON_INVALID"),
        ("files", "MANIFEST_FILES_INVALID"),
        ("envelope", "ROLE_ENVELOPE_INVALID"),
        ("schema", "ROLE_SCHEMA_MISMATCH"),
    ),
)
def test_input_errors_return_structured_invalid_reports(tmp_path, case, expected_code):
    packet = _packet(tmp_path)
    _write_hard_role(packet, [_hard_reference(packet)])
    if case == "manifest_json":
        Path(packet["manifest_path"]).write_text("{", encoding="utf-8")
    elif case == "files":
        manifest = packet["manifest"]
        assert isinstance(manifest, dict)
        manifest["files"] = {}
        _save_manifest(packet)
    elif case == "envelope":
        Path(packet["role_path"]).write_text("PASS\n{}\n", encoding="utf-8")
    else:
        role_path = Path(packet["role_path"])
        lines = role_path.read_text(encoding="utf-8").splitlines()
        payload = json.loads(lines[1])
        payload["schema_version"] = "judge-role-v1"
        role_path.write_text(
            f"VERDICT: PASS\n{json.dumps(payload)}\n", encoding="utf-8"
        )

    report = _validate(packet)

    assert report["schema_version"] == "evidence-grounding-v1"
    assert _packet_error_code(report) == expected_code


def test_cli_exit_codes_and_atomic_report_output(tmp_path):
    packet = _packet(tmp_path)
    _write_hard_role(packet, [_hard_reference(packet)])
    output = tmp_path / "grounding.json"
    command = [
        sys.executable,
        "scripts/evidence_grounding.py",
        "--role-output",
        str(packet["role_path"]),
        "--manifest",
        str(packet["manifest_path"]),
        "--context",
        str(packet["context_path"]),
        "--role",
        "math",
        "--output",
        str(output),
    ]

    valid = subprocess.run(
        command,
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert valid.returncode == 0, valid.stderr
    assert json.loads(output.read_text(encoding="utf-8"))["valid"] is True

    _write_hard_role(
        packet,
        [_hard_reference(packet, chunk_id="f" * 64)],
    )
    invalid_output = tmp_path / "invalid-grounding.json"
    invalid_command = [*command[:-1], str(invalid_output)]
    invalid = subprocess.run(
        invalid_command,
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert invalid.returncode == 1, invalid.stderr
    assert json.loads(invalid_output.read_text(encoding="utf-8"))["valid"] is False

    output_directory = tmp_path / "not-a-report"
    output_directory.mkdir()
    write_failure_command = [*command[:-1], str(output_directory)]
    write_failure = subprocess.run(
        write_failure_command,
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert write_failure.returncode == 2
    assert "ERROR:" in write_failure.stderr
