from __future__ import annotations

import base64
from dataclasses import fields
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import uuid

import pytest

import scripts.payload_secret_scan as payload_secret_scan
from scripts.payload_secret_scan import SecretFinding, scan_bytes, scan_frozen_payload
from scripts.evidence_payload_policy import payload_path_finding


ROOT = Path(__file__).resolve().parents[1]


def _base64url(value: dict[str, object]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(encoded).rstrip(b"=").decode()


def _runtime_only_jwt() -> bytes:
    header = _base64url({"alg": "HS512", "typ": "JWT"})
    payload = _base64url({"subject": "generated-test-value"})
    signature = "generated_signature_segment"
    return ".".join((header, payload, signature)).encode()


def _runtime_only_unsigned_jwt() -> bytes:
    header = _base64url({"alg": "none", "typ": "JWT"})
    payload = _base64url({"subject": "generated-test-value"})
    return ".".join((header, payload, "")).encode()


def _runtime_opaque() -> bytes:
    return f"generated-{uuid.uuid4().hex}".encode("ascii")


def test_scanner_validates_compact_jwt_without_storing_one_in_test_source() -> None:
    generated = _runtime_only_jwt()
    findings = scan_bytes("generated.bin", generated)

    assert [(finding.path, finding.rule) for finding in findings] == [
        ("generated.bin", "validated_compact_jwt")
    ]
    assert all(generated.decode() not in repr(finding) for finding in findings)


def test_scanner_validates_unsigned_compact_jwt_with_empty_third_segment() -> None:
    generated = _runtime_only_unsigned_jwt()
    assert [finding.rule for finding in scan_bytes("generated.bin", generated)] == [
        "validated_compact_jwt"
    ]


def test_scanner_rejects_hardcoded_bearer_but_allows_runtime_variables() -> None:
    hardcoded = b"".join((b"Authorization: ", b"Bearer ", _runtime_opaque()))
    safe_shell = b"".join((b"Authorization: ", b"Bearer ", b"${MINERU_TOKEN}"))
    safe_template = b"".join((b"Authorization: ", b"Bearer ", b"{API_KEY}"))
    unsafe_command = b"".join(
        (b"Authorization: ", b"Bearer ", b"$(", _runtime_opaque(), b")")
    )
    unsafe_gcloud_command = b"".join(
        (
            b"Authorization: ",
            b"Bearer ",
            b"$(gcloud auth print-identity-token ",
            _runtime_opaque(),
            b")",
        )
    )
    entropy = _runtime_opaque()
    placeholder = b"%" + bytes((ord("s") + entropy[0] - entropy[0],))
    unsafe_printf = b"".join((b"Authorization: ", b"Bearer ", placeholder, b"\\n"))

    assert [finding.rule for finding in scan_bytes("hardcoded", hardcoded)] == [
        "hardcoded_authorization_bearer"
    ]
    assert scan_bytes("shell", safe_shell) == ()
    assert scan_bytes("template", safe_template) == ()
    assert [finding.rule for finding in scan_bytes("command", unsafe_command)] == [
        "hardcoded_authorization_bearer"
    ]
    assert [
        finding.rule
        for finding in scan_bytes("gcloud-command", unsafe_gcloud_command)
    ] == ["hardcoded_authorization_bearer"]
    assert [finding.rule for finding in scan_bytes("printf", unsafe_printf)] == [
        "hardcoded_authorization_bearer"
    ]


def test_scanner_fails_closed_for_unclosed_dynamic_shape() -> None:
    generated = b"".join(
        (b"Authorization: ", b"Bearer ", b"$(", _runtime_opaque())
    )
    assert [finding.rule for finding in scan_bytes("unclosed.sh", generated)] == [
        "hardcoded_authorization_bearer"
    ]


def test_gcloud_command_substitution_with_literal_suffix_fails_closed() -> None:
    generated = b"".join(
        (
            b"Authorization: ",
            b"Bearer ",
            b"$(gcloud auth print-identity-token)",
            _runtime_opaque(),
        )
    )
    assert [finding.rule for finding in scan_bytes("generated.sh", generated)] == [
        "hardcoded_authorization_bearer"
    ]


@pytest.mark.parametrize(
    "suffix",
    (b'""generated-suffix', b"\\generated-suffix"),
)
def test_runtime_variable_with_adjacent_or_escaped_suffix_fails_closed(suffix) -> None:
    generated = b"".join(
        (b'curl -H "Authorization: ', b"Bearer ", b"${TOKEN}", suffix, b'"')
    )
    assert [finding.rule for finding in scan_bytes("generated.sh", generated)] == [
        "hardcoded_authorization_bearer"
    ]


@pytest.mark.parametrize(
    "suffix",
    (b"", b"''generated-suffix", b'""generated-suffix', b"\\generated-suffix"),
)
def test_shell_printf_bearer_placeholder_is_never_an_authorized_sink(suffix) -> None:
    source = b"".join(
        (
            b"printf 'Authorization: ",
            b"Bearer %s",
            suffix,
            b"' \"${TOKEN}\" | curl -H @- endpoint",
        )
    )
    assert [finding.rule for finding in scan_bytes("generated.sh", source)] == [
        "hardcoded_authorization_bearer"
    ]


def test_generic_non_shell_printf_placeholder_with_suffix_fails_closed() -> None:
    entropy = _runtime_opaque()
    placeholder = b"%" + bytes((ord("s") + entropy[0] - entropy[0],))
    source = b"".join(
        (b"Authorization: ", b"Bearer ", placeholder, entropy)
    )
    assert [finding.rule for finding in scan_bytes("extensionless-data", source)] == [
        "hardcoded_authorization_bearer"
    ]


def test_python_statically_foldable_bearer_literals_are_detected() -> None:
    opaque = _runtime_opaque()
    source = b"".join(
        (
            b"value = ",
            b'"Authorization: Bearer " ',
            b'"' + opaque + b'"',
        )
    )
    assert [finding.rule for finding in scan_bytes("generated.py", source)] == [
        "hardcoded_authorization_bearer"
    ]


def test_python_authorization_mapping_static_value_is_detected() -> None:
    opaque = _runtime_opaque()
    source = b"".join(
        (
            b"headers = {",
            b'"Authorization": ',
            b'"Bearer ' + opaque + b'"}',
        )
    )
    assert [finding.rule for finding in scan_bytes("mapping.py", source)] == [
        "hardcoded_authorization_bearer"
    ]


def test_python_authorization_mapping_resolves_credential_constant() -> None:
    opaque = _runtime_opaque()
    source = b"".join(
        (
            b'TOKEN = "' + opaque + b'"\n',
            b"headers = {",
            b'"Authorization": f"Bearer {TOKEN}"}',
        )
    )
    assert [finding.rule for finding in scan_bytes("mapping.py", source)] == [
        "hardcoded_authorization_bearer"
    ]


def test_python_authorization_mapping_resolves_arbitrary_static_constant() -> None:
    opaque = _runtime_opaque()
    source = b"".join(
        (
            b'opaque = "' + opaque + b'"\n',
            b"headers = {",
            b'"Authorization": f"Bearer {opaque}"}',
        )
    )
    assert [finding.rule for finding in scan_bytes("mapping.py", source)] == [
        "hardcoded_authorization_bearer"
    ]


def test_python_authorization_dict_constructor_resolves_annotated_constant() -> None:
    opaque = _runtime_opaque()
    source = b"".join(
        (
            b'opaque: str = "' + opaque + b'"\n',
            b"headers = dict(",
            b'Authorization=f"Bearer {opaque}")',
        )
    )
    assert [finding.rule for finding in scan_bytes("mapping.py", source)] == [
        "hardcoded_authorization_bearer"
    ]


def _scan_python_source_in_subprocess(source: bytes) -> subprocess.CompletedProcess:
    command = [
        sys.executable,
        "-c",
        (
            "import sys; from scripts.payload_secret_scan import scan_bytes; "
            "value=sys.stdin.buffer.read(); "
            "print(','.join(item.rule for item in scan_bytes('generated.py', value)))"
        ),
    ]
    return subprocess.run(
        command,
        cwd=ROOT,
        input=source,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=2,
        check=False,
    )


def test_python_conflicting_static_assignments_converge_and_fail_closed() -> None:
    source = b"".join(
        (
            b'opaque = "generated-static-one"\n',
            b'opaque = "generated-static-two"\n',
            b'headers = {"Authorization": f"Bearer {opaque}"}\n',
        )
    )
    completed = _scan_python_source_in_subprocess(source)
    assert completed.returncode == 0
    assert completed.stdout.strip() == b"hardcoded_authorization_bearer"


@pytest.mark.parametrize("case", ("chain", "cycle"))
def test_python_static_assignment_graph_is_bounded_and_conservative(case) -> None:
    if case == "chain":
        assignments = (
            b"first = second\n",
            b'second = "generated-static-value"\n',
        )
    else:
        assignments = (b"first = second\n", b"second = first\n")
    source = b"".join(
        (
            *assignments,
            b'headers = {"Authorization": f"Bearer {first}"}\n',
        )
    )
    completed = _scan_python_source_in_subprocess(source)
    assert completed.returncode == 0
    assert completed.stdout.strip() == b"hardcoded_authorization_bearer"


def test_shell_bearer_reference_to_static_credential_assignment_is_detected() -> None:
    source = b"\n".join(
        (
            b"TOKEN='generated-static-opaque-value'",
            b'curl -H "Authorization: Bearer ${TOKEN}" endpoint',
        )
    )
    assert [finding.rule for finding in scan_bytes("generated.sh", source)] == [
        "hardcoded_authorization_bearer"
    ]


def test_shell_static_escaped_credential_assignment_fails_closed() -> None:
    source = b"\n".join(
        (
            b"TOKEN='generated\\-static-opaque-value'",
            b'curl -H "Authorization: Bearer ${TOKEN}" endpoint',
        )
    )
    assert [finding.rule for finding in scan_bytes("generated.sh", source)] == [
        "hardcoded_authorization_bearer"
    ]


def test_shell_single_quoted_dollar_assignment_is_not_dynamic() -> None:
    source = b"\n".join(
        (
            b"TOKEN='$RUNTIME_SOURCE'",
            b'curl -H "Authorization: Bearer ${TOKEN}" endpoint',
        )
    )
    assert [finding.rule for finding in scan_bytes("generated.sh", source)] == [
        "hardcoded_authorization_bearer"
    ]


def test_shell_alias_assignment_is_conservatively_rejected_after_quote_removal() -> None:
    source = b"\n".join(
        (
            b'TOKEN="${RUNTIME_SOURCE}"',
            b'curl -H "Authorization: Bearer ${TOKEN}" endpoint',
        )
    )
    assert [finding.rule for finding in scan_bytes("generated.sh", source)] == [
        "hardcoded_authorization_bearer"
    ]


@pytest.mark.parametrize(
    "assignment",
    (
        b'TOKEN="literal-prefix-${RUNTIME_SOURCE}"',
        b"TOKEN=$(printf generated-static-value)",
        b"TOKEN=`printf generated-static-value`",
    ),
)
def test_shell_non_exact_dynamic_shapes_fail_closed(assignment) -> None:
    source = b"\n".join(
        (
            assignment,
            b'curl -H "Authorization: Bearer ${TOKEN}" endpoint',
        )
    )
    assert [finding.rule for finding in scan_bytes("generated.sh", source)] == [
        "hardcoded_authorization_bearer"
    ]


def test_shell_printf_command_substitution_is_not_treated_as_dynamic_safe() -> None:
    source = b"".join(
        (
            b"Authorization: ",
            b"Bearer ",
            b"$(printf ",
            _runtime_opaque(),
            b")",
        )
    )
    assert [finding.rule for finding in scan_bytes("generated.sh", source)] == [
        "hardcoded_authorization_bearer"
    ]


def test_jwt_shape_without_json_header_and_payload_is_not_accepted() -> None:
    non_jwt = b"not_json.also_not_json.signature_segment"
    assert scan_bytes("invalid", non_jwt) == ()


def test_findings_report_only_path_and_rule() -> None:
    generated = _runtime_only_jwt()
    findings = scan_frozen_payload({"one": generated, "two": b"ordinary"})

    assert [(finding.path, finding.rule) for finding in findings] == [
        ("one", "validated_compact_jwt")
    ]
    rendered = json.dumps(
        [{"path": finding.path, "rule": finding.rule} for finding in findings]
    )
    assert generated.decode() not in rendered


def test_malformed_python_is_an_auditable_fail_closed_finding() -> None:
    findings = scan_bytes("malformed.py", b"def unfinished(:\n    pass\n")

    assert [(finding.path, finding.rule) for finding in findings] == [
        ("malformed.py", "python_syntax_error")
    ]


def test_invalid_python_encoding_and_strict_decode_fail_closed() -> None:
    unknown = scan_bytes(
        "unknown-encoding.py",
        b"# coding: generated-unknown-codec\nvalue = 1\n",
    )
    invalid = scan_bytes(
        "invalid-encoding.py",
        b"# coding: ascii\nvalue = '\xff'\n",
    )

    assert [finding.rule for finding in unknown] == ["python_encoding_error"]
    assert [finding.rule for finding in invalid] == ["python_decode_error"]


def test_invalid_encoding_raw_fallback_still_detects_split_credential() -> None:
    source = b"".join(
        (
            b"# coding: ascii\n",
            b'token = "sk-" "',
            _runtime_opaque(),
            b'\xff"\n',
        )
    )

    assert {finding.rule for finding in scan_bytes("invalid-secret.py", source)} == {
        "openai_api_key",
        "python_decode_error",
    }


def test_malformed_python_raw_fallback_detects_split_bearer_and_credential() -> None:
    bearer_source = b"".join(
        (
            b'headers = {"Author" "ization": "Bear" "er ',
            _runtime_opaque(),
            b'"\n',
        )
    )
    credential_source = b"".join(
        (
            b'token = "sk-" "',
            _runtime_opaque(),
            b'"\n(',
        )
    )

    bearer_rules = {finding.rule for finding in scan_bytes("bearer.py", bearer_source)}
    credential_rules = {
        finding.rule for finding in scan_bytes("credential.py", credential_source)
    }

    assert bearer_rules == {
        "hardcoded_authorization_bearer",
        "python_syntax_error",
    }
    assert credential_rules == {
        "openai_api_key",
        "python_syntax_error",
    }


def test_internal_scanner_exception_is_a_redacted_fail_closed_finding(
    monkeypatch,
) -> None:
    def fail_internal_scan(_value: bytes) -> bool:
        raise RuntimeError("generated internal detail must stay private")

    monkeypatch.setattr(
        payload_secret_scan,
        "_contains_validated_compact_jwt",
        fail_internal_scan,
    )

    findings = scan_bytes("ordinary.txt", b"ordinary public documentation\n")

    assert [(finding.path, finding.rule) for finding in findings] == [
        ("ordinary.txt", "secret_scanner_internal_error")
    ]


def test_secret_scan_cli_returns_nonzero_for_scanner_findings_and_read_errors(
    tmp_path,
) -> None:
    clean = tmp_path / "clean.txt"
    malformed = tmp_path / "malformed.py"
    clean.write_bytes(b"ordinary public documentation\n")
    malformed.write_bytes(b"value = (\n")

    clean_run = subprocess.run(
        [sys.executable, "scripts/payload_secret_scan.py", str(clean)],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    malformed_run = subprocess.run(
        [sys.executable, "scripts/payload_secret_scan.py", str(malformed)],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    missing_run = subprocess.run(
        [sys.executable, "scripts/payload_secret_scan.py", str(tmp_path / "missing")],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )

    assert clean_run.returncode == 0
    assert malformed_run.returncode != 0
    assert "python_syntax_error" in malformed_run.stdout
    assert missing_run.returncode != 0
    assert "payload_read_error" in missing_run.stderr


@pytest.mark.parametrize(
    "document",
    (
        b'{"Authorization": "' + b"Bearer " + _runtime_opaque() + b'"}',
        b"'Authorization': '" + b"Bearer " + _runtime_opaque() + b"'",
        b'Authorization: "' + b"Bearer " + _runtime_opaque() + b'"',
    ),
    ids=(
        "json-quoted-header",
        "yaml-double-quoted-header",
        "yaml-single-quoted-header",
    ),
)
def test_quoted_json_and_yaml_authorization_headers_are_detected(document) -> None:
    assert [finding.rule for finding in scan_bytes("generated.yaml", document)] == [
        "hardcoded_authorization_bearer"
    ]


def test_python_scope_isolation_does_not_pollute_dynamic_parameter_sink() -> None:
    opaque = _runtime_opaque()
    source = b"".join(
        (
            b"def first():\n",
            b"    opaque = \"" + opaque + b"\"\n",
            b"    return opaque\n",
            b"def second(opaque):\n",
            b"    return {\"Authorization\": f\"Bearer {opaque}\"}\n",
        )
    )
    assert scan_bytes("scoped.py", source) == ()


def test_python_explicit_environment_read_with_no_reassignment_is_allowed() -> None:
    source = b"".join(
        (
            b"import os\n",
            b'token = os.environ["RUNTIME_TOKEN"]\n',
            b'headers = {"Authorization": f"Bearer {token}"}\n',
        )
    )
    assert scan_bytes("environment.py", source) == ()


def test_python_static_default_authorization_parameter_fails_closed() -> None:
    opaque = _runtime_opaque()
    source = b"".join(
        (
            b'def send(token="',
            opaque,
            b'"):\n    return {"Authorization": f"Bearer {token}"}\n',
        )
    )
    assert [finding.rule for finding in scan_bytes("default.py", source)] == [
        "hardcoded_authorization_bearer"
    ]


def test_python_same_file_static_authorization_argument_fails_closed() -> None:
    opaque = _runtime_opaque()
    source = b"".join(
        (
            b'def send(token):\n    return {"Authorization": f"Bearer {token}"}\n',
            b'send("',
            opaque,
            b'")\n',
        )
    )
    assert [finding.rule for finding in scan_bytes("callsite.py", source)] == [
        "hardcoded_authorization_bearer"
    ]


def test_python_shadowed_os_environ_is_not_a_trusted_runtime_source() -> None:
    opaque = _runtime_opaque()
    source = b"".join(
        (
            b'class Container:\n    environ = {"TOKEN": "',
            opaque,
            b'"}\nos = Container()\ntoken = os.environ["TOKEN"]\n',
            b'headers = {"Authorization": f"Bearer {token}"}\n',
        )
    )
    assert [finding.rule for finding in scan_bytes("shadow.py", source)] == [
        "hardcoded_authorization_bearer"
    ]


def test_python_authorization_constant_join_call_fails_closed() -> None:
    opaque = _runtime_opaque()
    source = b"".join(
        (
            b'headers = {"Authorization": "".join(("Bearer ", "',
            opaque,
            b'"))}\n',
        )
    )
    assert [finding.rule for finding in scan_bytes("join.py", source)] == [
        "hardcoded_authorization_bearer"
    ]


def test_python_authorization_unknown_call_fails_closed() -> None:
    source = b'headers = {"Authorization": build_header(runtime_value)}\n'
    assert [finding.rule for finding in scan_bytes("call.py", source)] == [
        "hardcoded_authorization_bearer"
    ]


@pytest.mark.parametrize(
    "case",
    range(4),
)
def test_python_authorization_header_keys_are_ascii_case_insensitive(case) -> None:
    prefixes = (
        b'headers = {"authorization": "',
        b'request.add_header("aUtHoRiZaTiOn", "',
        b'headers = dict(AUTHORIZATION="',
        b'headers["AuThOrIzAtIoN"] = "',
    )
    suffixes = (b'"}\n', b'")\n', b'")\n', b'"\n')
    bearer_prefix = b"Bear" + b"er "
    source = prefixes[case] + bearer_prefix + _runtime_opaque() + suffixes[case]
    assert [finding.rule for finding in scan_bytes("case.py", source)] == [
        "hardcoded_authorization_bearer"
    ]


def test_lowercase_business_authorization_without_bearer_is_not_an_http_sink() -> None:
    source = b'record = {"authorization": override.to_dict()}\n'
    assert scan_bytes("business.py", source) == ()


@pytest.mark.parametrize(
    "template",
    (
        b'headers = dict([("authorization", "Bearer %s")])\n',
        b'headers.update([("AUTHORIZATION", "Bearer %s")])\n',
        b'headers.update(authorization="Bearer %s")\n',
        b'headers.setdefault("aUtHoRiZaTiOn", "Bearer %s")\n',
    ),
)
def test_python_authorization_container_transforms_are_scanned(template) -> None:
    source = template.replace(b"%s", _runtime_opaque())
    assert [finding.rule for finding in scan_bytes("container.py", source)] == [
        "hardcoded_authorization_bearer"
    ]


def test_python_unknown_header_update_call_fails_closed() -> None:
    source = b"headers.update(build_runtime_headers())\n"
    assert [finding.rule for finding in scan_bytes("update.py", source)] == [
        "hardcoded_authorization_bearer"
    ]


def test_python_unknown_dict_transform_assigned_to_headers_fails_closed() -> None:
    source = b"headers = dict(build_runtime_pairs())\n"
    assert [finding.rule for finding in scan_bytes("dict-call.py", source)] == [
        "hardcoded_authorization_bearer"
    ]


def test_python_pep263_pyw_adjacent_constants_are_scanned() -> None:
    opaque = _runtime_opaque()
    source = b"".join(
        (
            b"# coding: latin-1\nlabel = 'caf\xe9'\n",
            b'headers = {"Authorization": "Bearer " "',
            opaque,
            b'"}\n',
        )
    )
    assert [finding.rule for finding in scan_bytes("window.PYW", source)] == [
        "hardcoded_authorization_bearer"
    ]


@pytest.mark.parametrize(
    "source",
    (
        b"def call(token):\n"
        b'    return {"Authorization": f"Bearer {token + \'-suffix\'}"}\n',
        b"def call(token):\n"
        b'    return {"Authorization": f"Bearer {(token := \'static\')}"}\n',
        b"def call(token):\n"
        b'    token += "-suffix"\n'
        b'    return {"Authorization": f"Bearer {token}"}\n',
    ),
)
def test_python_complex_named_expression_and_augmented_flows_fail_closed(source) -> None:
    assert [finding.rule for finding in scan_bytes("complex.py", source)] == [
        "hardcoded_authorization_bearer"
    ]


def test_shell_tracks_arbitrary_variable_and_static_alias_into_bearer_sink() -> None:
    opaque = _runtime_opaque()
    source = b"\n".join(
        (
            b"opaque='" + opaque + b"'",
            b'alias="${opaque}"',
            b'curl -H "Authorization: Bearer ${alias}" endpoint',
        )
    )
    assert [finding.rule for finding in scan_bytes("generated.sh", source)] == [
        "hardcoded_authorization_bearer"
    ]


@pytest.mark.parametrize(
    "declaration",
    (b"local", b"declare -r", b"readonly", b"typeset"),
)
def test_shell_declaration_forms_cannot_hide_static_bearer_values(declaration) -> None:
    source = b"\n".join(
        (
            b"call() {",
            b"  " + declaration + b" opaque='" + _runtime_opaque() + b"'",
            b'  curl -H "Authorization: Bearer ${opaque}" endpoint',
            b"}",
        )
    )
    assert [finding.rule for finding in scan_bytes("generated.sh", source)] == [
        "hardcoded_authorization_bearer"
    ]


def test_shell_adjacent_quoted_header_key_is_normalized_and_detected() -> None:
    source = b"".join(
        (
            b"curl -H 'Author''ization: ",
            b"Bearer ",
            _runtime_opaque(),
            b"' endpoint",
        )
    )
    assert [finding.rule for finding in scan_bytes("generated.sh", source)] == [
        "hardcoded_authorization_bearer"
    ]


def test_shell_same_line_local_assignment_and_bearer_sink_fail_closed() -> None:
    source = b"".join(
        (
            b"call() { local token='",
            _runtime_opaque(),
            b"'; curl -H \"Authorization: " + b"Bearer ${token}\" endpoint; }",
        )
    )
    assert [finding.rule for finding in scan_bytes("generated.sh", source)] == [
        "hardcoded_authorization_bearer"
    ]


def test_shell_command_substitution_from_static_source_fails_closed() -> None:
    source = b"\n".join(
        (
            b"source_value='" + _runtime_opaque() + b"'",
            b'token=$(printf "%s" "$source_value" | jq -r \'.token\')',
            b'curl -H "Authorization: Bearer ${token}" endpoint',
        )
    )
    assert [finding.rule for finding in scan_bytes("generated.sh", source)] == [
        "hardcoded_authorization_bearer"
    ]


@pytest.mark.parametrize(
    "assignment",
    (
        b"token+=generated-static-suffix",
        b"token[0]=generated-static-value",
        b"printf -v token %s generated-static-value",
    ),
)
def test_shell_mutations_map_to_base_variable_conflict(assignment) -> None:
    source = b"\n".join(
        (
            b"#!/usr/bin/env bash",
            assignment,
            b'curl -H "Authorization: Bearer ${token}" endpoint',
        )
    )
    assert [finding.rule for finding in scan_bytes("extensionless", source)] == [
        "hardcoded_authorization_bearer"
    ]


@pytest.mark.parametrize("extension", ("task.zsh", "task.ksh", "task.bats", "task.command"))
def test_common_shell_extensions_are_analyzed(extension) -> None:
    source = b"\n".join(
        (
            b"token=generated-static-value",
            b'curl -H "Authorization: Bearer ${token}" endpoint',
        )
    )
    assert [finding.rule for finding in scan_bytes(extension, source)] == [
        "hardcoded_authorization_bearer"
    ]


def test_extensionless_shebang_allows_unassigned_external_runtime_variable() -> None:
    source = b"\n".join(
        (
            b"#!/bin/sh",
            b'curl -H "Authorization: Bearer ${EXTERNAL_RUNTIME_TOKEN}" endpoint',
        )
    )
    assert scan_bytes("extensionless", source) == ()


def test_shell_static_prefix_and_token_variable_concatenation_fails_closed() -> None:
    source = b"\n".join(
        (
            b"prefix='Authorization: Bearer '",
            b"token='" + _runtime_opaque() + b"'",
            b'curl -H "${prefix}${token}" endpoint',
        )
    )
    assert [finding.rule for finding in scan_bytes("generated.sh", source)] == [
        "hardcoded_authorization_bearer"
    ]


def test_extensionless_ash_shebang_is_analyzed() -> None:
    source = b"\n".join(
        (
            b"#!/bin/ash",
            b"token='" + _runtime_opaque() + b"'",
            b'curl -H "Authorization: Bearer ${token}" endpoint',
        )
    )
    assert [finding.rule for finding in scan_bytes("extensionless", source)] == [
        "hardcoded_authorization_bearer"
    ]


@pytest.mark.parametrize("reader", (b"read -r", b"readarray", b"mapfile -t"))
def test_shell_here_string_input_marks_read_targets_conflicted(reader) -> None:
    source = b"\n".join(
        (
            b"#!/bin/ash",
            reader + b" TOKEN <<< '" + _runtime_opaque() + b"'",
            b'curl -H "Authorization: Bearer ${TOKEN}" endpoint',
        )
    )
    assert [finding.rule for finding in scan_bytes("extensionless", source)] == [
        "hardcoded_authorization_bearer"
    ]


@pytest.mark.parametrize(
    "mutation",
    (
        b'printf -v "$DEST" %s generated-static-value',
        b'declare "$DEST=generated-static-value"',
    ),
)
def test_shell_static_indirect_assignment_targets_fail_closed(mutation) -> None:
    source = b"\n".join(
        (
            b"DEST=TOKEN",
            mutation,
            b'curl -H "Authorization: Bearer ${TOKEN}" endpoint',
        )
    )
    assert [finding.rule for finding in scan_bytes("generated.sh", source)] == [
        "hardcoded_authorization_bearer"
    ]


def test_finding_shape_and_scanner_output_are_strictly_redacted(capsys) -> None:
    generated = b"Authorization: " + b"Bearer " + _runtime_opaque()
    findings = scan_bytes("generated", generated)
    captured = capsys.readouterr()

    assert tuple(item.name for item in fields(SecretFinding)) == ("path", "rule")
    assert tuple(vars(findings[0])) == ("path", "rule")
    assert captured.out == ""
    assert captured.err == ""


def test_complete_candidate_source_snapshot_has_no_secret_findings() -> None:
    if (ROOT / ".git").exists():
        listed = subprocess.check_output(
            ["git", "ls-files", "-co", "--exclude-standard", "-z"],
            cwd=ROOT,
        ).split(b"\0")
    else:
        listed = [
            item.relative_to(ROOT).as_posix().encode("utf-8")
            for item in sorted(ROOT.rglob("*"))
            if item.is_file() or item.is_symlink()
        ]
    frozen: dict[str, bytes] = {}
    for raw_path in listed:
        if not raw_path:
            continue
        path = raw_path.decode("utf-8", errors="strict")
        # audit_artifacts/ is the immutable output/evidence root, never a
        # candidate source root.  Historical patches intentionally preserve
        # earlier security probes and therefore can contain credential-shaped
        # bytes.  Exclude only that generated root; every current source path
        # remains in this independently reconstructed candidate inventory.
        if path == "audit_artifacts" or path.startswith("audit_artifacts/"):
            continue
        if payload_path_finding(path) is not None:
            continue
        candidate = ROOT / path
        assert not candidate.is_symlink(), path
        if not candidate.is_file():
            assert path == "xhxmt.github.io"
            continue
        frozen[path] = candidate.read_bytes()

    assert scan_frozen_payload(frozen) == ()


def test_mineru_entrypoints_have_no_high_confidence_secret_findings() -> None:
    frozen = {
        relative: (ROOT / relative).read_bytes()
        for relative in ("scripts/mineru_ocr.sh", "scripts/mineru_ocr.py")
    }
    assert scan_frozen_payload(frozen) == ()


@pytest.mark.parametrize("supplied", (None, "", " \t "))
def test_mineru_entrypoints_fail_closed_for_missing_or_blank_key(
    tmp_path,
    supplied,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    network_sentinel = tmp_path / "network-command-was-invoked"
    fake_curl = fake_bin / "curl"
    fake_curl.write_text(
        "#!/usr/bin/env bash\n"
        f"touch {network_sentinel!s}\n"
        "exit 99\n",
        encoding="utf-8",
    )
    fake_curl.chmod(0o700)
    environment = dict(os.environ)
    if supplied is None:
        environment.pop("MINERU_TOKEN", None)
    else:
        environment["MINERU_TOKEN"] = supplied
    environment["PATH"] = f"{fake_bin}:{environment['PATH']}"
    environment["MINERU_NETWORK_SENTINEL"] = str(network_sentinel)
    environment["MINERU_TEST_OUT"] = str(tmp_path / "python")

    commands = (
        ["bash", "scripts/mineru_ocr.sh", "input.pdf", str(tmp_path / "shell")],
        [
            sys.executable,
            "-c",
            (
                "import os,pathlib,runpy,sys,urllib.request;"
                "urllib.request.urlopen=lambda *a,**k: "
                "(pathlib.Path(os.environ['MINERU_NETWORK_SENTINEL']).touch(),"
                "(_ for _ in ()).throw(RuntimeError('network invoked')))[1];"
                "sys.argv=['scripts/mineru_ocr.py','input.pdf',os.environ['MINERU_TEST_OUT']];"
                "runpy.run_path('scripts/mineru_ocr.py',run_name='__main__')"
            ),
        ],
    )
    for command in commands:
        completed = subprocess.run(
            command,
            cwd=ROOT,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=10,
            check=False,
        )
        assert completed.returncode != 0
        assert "MINERU_TOKEN is required" in completed.stdout
        assert not network_sentinel.exists()


def test_mineru_shell_uses_stdin_headers_without_credential_argv_or_files(tmp_path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    sentinels = tmp_path / "sentinels"
    sentinels.mkdir()
    fake_curl = fake_bin / "curl"
    fake_curl.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "count_file=\"$MINERU_TEST_SENTINEL/curl-count\"\n"
        "count=0\n"
        "if [[ -f \"$count_file\" ]]; then read -r count < \"$count_file\"; fi\n"
        "count=$((count + 1))\n"
        "printf '%s\\n' \"$count\" > \"$count_file\"\n"
        "stdin_header=false\n"
        "if [[ -v MINERU_TOKEN || -v MINERU_AUTH_TOKEN ]]; then touch \"$MINERU_TEST_SENTINEL/env-leak\"; fi\n"
        "for argument in \"$@\"; do\n"
        "  if [[ \"$argument\" == *Authorization:* || \"$argument\" == *Bearer* ]]; then touch \"$MINERU_TEST_SENTINEL/argv-leak\"; fi\n"
        "  if [[ \"$argument\" == '@-' ]]; then stdin_header=true; fi\n"
        "done\n"
        "if $stdin_header; then\n"
        "  IFS= read -r header || true\n"
        "  if [[ \"$header\" != Authorization:* || \"$header\" != *Bearer* ]]; then touch \"$MINERU_TEST_SENTINEL/header-invalid\"; fi\n"
        "  touch \"$MINERU_TEST_SENTINEL/header-$count\"\n"
        "fi\n"
        "case \"$count\" in\n"
        "  1) printf '%s\\n' '{\"data\":{\"file_urls\":[\"https://upload.invalid/object\"],\"batch_id\":\"batch\"}}' ;;\n"
        "  2) : ;;\n"
        "  3) printf '%s\\n' '{}' ;;\n"
        "  4) printf '%s\\n' '{\"data\":{\"list\":[{\"state\":\"failed\"}]}}' ;;\n"
        "  *) exit 98 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    fake_curl.chmod(0o700)
    fake_sleep = fake_bin / "sleep"
    fake_sleep.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    fake_sleep.chmod(0o700)
    pdf = tmp_path / "input.pdf"
    pdf.write_bytes(b"%PDF-generated-test")
    token = _runtime_opaque().decode("ascii")
    environment = dict(os.environ)
    environment["MINERU_TOKEN"] = token
    environment["MINERU_TEST_SENTINEL"] = str(sentinels)
    environment["PATH"] = f"{fake_bin}:{environment['PATH']}"

    completed = subprocess.run(
        ["bash", "scripts/mineru_ocr.sh", str(pdf), str(tmp_path / "output")],
        cwd=ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=10,
        check=False,
    )

    output_contains_token = token in completed.stdout
    temporary_contains_token = any(
        token.encode("ascii") in path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    )
    assert completed.returncode != 0
    assert (sentinels / "curl-count").read_text(encoding="utf-8").strip() == "4"
    assert all((sentinels / f"header-{number}").is_file() for number in (1, 3, 4))
    assert not (sentinels / "header-invalid").exists()
    assert not (sentinels / "argv-leak").exists()
    assert not (sentinels / "env-leak").exists()
    assert output_contains_token is False
    assert temporary_contains_token is False


def test_mineru_python_removes_credential_environment_before_subprocess(tmp_path) -> None:
    sentinels = tmp_path / "sentinels"
    sentinels.mkdir()
    pdf = tmp_path / "input.pdf"
    pdf.write_bytes(b"%PDF-generated-test")
    token = _runtime_opaque().decode("ascii")
    environment = dict(os.environ)
    environment["MINERU_TOKEN"] = token
    environment["MINERU_PY_SENTINEL"] = str(sentinels)
    environment["MINERU_PY_PDF"] = str(pdf)
    environment["MINERU_PY_OUT"] = str(tmp_path / "output")
    wrapper = (
        "import json,os,pathlib,runpy,subprocess,sys,urllib.request\n"
        "S=pathlib.Path(os.environ['MINERU_PY_SENTINEL'])\n"
        "class R:\n"
        " def __enter__(self): return self\n"
        " def __exit__(self,*a): return False\n"
        " def read(self): return json.dumps({'data':{'batch_id':'b','file_urls':['https://upload.invalid']}}).encode()\n"
        "def open_(*a,**k):\n"
        " if 'MINERU_TOKEN' in os.environ: (S/'env-leak').touch()\n"
        " (S/'urlopen-called').touch()\n"
        " return R()\n"
        "def call_(*a,**k):\n"
        " if 'MINERU_TOKEN' in os.environ: (S/'env-leak').touch()\n"
        " (S/'subprocess-called').touch()\n"
        " raise SystemExit(77)\n"
        "urllib.request.urlopen=open_\n"
        "subprocess.call=call_\n"
        "sys.argv=['scripts/mineru_ocr.py',os.environ['MINERU_PY_PDF'],os.environ['MINERU_PY_OUT']]\n"
        "runpy.run_path('scripts/mineru_ocr.py',run_name='__main__')\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", wrapper],
        cwd=ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=10,
        check=False,
    )

    output_contains_token = token in completed.stdout
    assert completed.returncode == 77
    assert (sentinels / "urlopen-called").is_file()
    assert (sentinels / "subprocess-called").is_file()
    assert not (sentinels / "env-leak").exists()
    assert output_contains_token is False


def _assert_gcp_runtime_fd_source_identity(
    root: Path,
    relative: str,
    value: bytes,
    approved: str,
) -> None:
    """Bind the exception to Git HEAD or the extracted candidate closure."""

    try:
        git_root = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    except OSError:
        git_root = None

    if git_root is not None and git_root.returncode == 0:
        assert Path(git_root.stdout.strip()).resolve() == root.resolve()
        head_value = subprocess.check_output(
            ["git", "show", f"HEAD:{relative}"],
            cwd=root,
        )
        assert value == head_value
        return

    manifest_path = root / "MANIFEST.json"
    checksums_path = root / "checksums" / "SHA256SUMS"
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes.decode("utf-8"))
    archive_root = manifest["archive_root"]
    assert isinstance(archive_root, str) and archive_root

    matching = [
        entry
        for entry in manifest["files"]
        if entry.get("source_path") == relative
    ]
    assert len(matching) == 1
    entry = matching[0]
    archive_path = f"{archive_root}/{relative}"
    assert entry == {
        "archive_path": archive_path,
        "mode": (root / relative).stat().st_mode & 0o777,
        "sha256": approved,
        "size": len(value),
        "source_path": relative,
    }

    checksum_entries: dict[str, str] = {}
    for line in checksums_path.read_text(encoding="utf-8").splitlines():
        digest, separator, path = line.partition("  ")
        assert separator == "  "
        assert path not in checksum_entries
        checksum_entries[path] = digest
    manifest_archive_path = f"{archive_root}/MANIFEST.json"
    expected_paths = {
        item["archive_path"] for item in manifest["files"]
    } | {manifest_archive_path}
    assert set(checksum_entries) == expected_paths
    assert checksum_entries[archive_path] == approved
    assert checksum_entries[manifest_archive_path] == hashlib.sha256(
        manifest_bytes
    ).hexdigest()


def test_gcp_runtime_fd_exception_is_exact_path_and_frozen_bytes_only() -> None:
    relative = "scripts/gcp_solver_client.sh"
    value = (ROOT / relative).read_bytes()
    approved = "20593f23782ee9c053a2e7c40f4fad2f36fe868a37e20a0410751b12e679cf82"
    assert hashlib.sha256(value).hexdigest() == approved
    _assert_gcp_runtime_fd_source_identity(ROOT, relative, value, approved)
    assert scan_bytes(relative, value) == ()
    assert scan_bytes(f"paper_factory/{relative}", value) == ()

    function_body = (
        b'    "$PYTHON_BIN_RESOLVED" "$AUTH_HELPER" token --audience "$SERVICE_URL"\n'
    )
    static_function = value.replace(
        function_body,
        b"    printf '%s\\n' 'generated-static-function-value'\n",
        1,
    )
    assignment = b'ID_TOKEN="$(get_identity_token)"\n'
    static_reassignment = value.replace(
        assignment,
        assignment + b"ID_TOKEN='generated-static-reassignment'\n",
        1,
    )
    cases = (
        (f"nested/{relative}", value),
        (relative, value + b"\n"),
        (relative, static_function),
        (relative, static_reassignment),
    )
    for path, candidate in cases:
        assert [finding.rule for finding in scan_bytes(path, candidate)] == [
            "hardcoded_authorization_bearer"
        ]


def test_gcp_runtime_fd_exception_accepts_exact_candidate_manifest_closure(
    tmp_path,
) -> None:
    relative = "scripts/gcp_solver_client.sh"
    approved = "20593f23782ee9c053a2e7c40f4fad2f36fe868a37e20a0410751b12e679cf82"
    value = (ROOT / relative).read_bytes()
    candidate_path = tmp_path / relative
    candidate_path.parent.mkdir(parents=True)
    candidate_path.write_bytes(value)
    candidate_path.chmod(0o755)

    archive_root = "paper_factory_phase4_6_candidate"
    archive_path = f"{archive_root}/{relative}"
    manifest = {
        "archive_root": archive_root,
        "files": [
            {
                "archive_path": archive_path,
                "mode": 0o755,
                "sha256": approved,
                "size": len(value),
                "source_path": relative,
            }
        ],
    }
    manifest_bytes = (
        json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    (tmp_path / "MANIFEST.json").write_bytes(manifest_bytes)
    checksums = tmp_path / "checksums"
    checksums.mkdir()
    (checksums / "SHA256SUMS").write_text(
        f"{approved}  {archive_path}\n"
        f"{hashlib.sha256(manifest_bytes).hexdigest()}  "
        f"{archive_root}/MANIFEST.json\n",
        encoding="utf-8",
    )

    _assert_gcp_runtime_fd_source_identity(
        tmp_path,
        relative,
        value,
        approved,
    )


def test_gcp_solver_client_refreshes_fd_token_without_argv_log_or_temp_leak(tmp_path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    sentinels = tmp_path / "sentinels"
    sentinels.mkdir()
    runtime_tmp = tmp_path / "runtime-tmp"
    runtime_tmp.mkdir()
    auth_count = sentinels / "auth-count"
    curl_count = sentinels / "curl-count"
    fake_gcloud = fake_bin / "gcloud"
    fake_gcloud.write_text(
        "#!/usr/bin/env bash\nprintf '%s\\n' 'https://solver.invalid'\n",
        encoding="utf-8",
    )
    fake_gcloud.chmod(0o700)
    fake_gsutil = fake_bin / "gsutil"
    fake_gsutil.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    fake_gsutil.chmod(0o700)
    fake_sleep = fake_bin / "sleep"
    fake_sleep.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    fake_sleep.chmod(0o700)
    fake_curl = fake_bin / "curl"
    fake_curl.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "count=0\n"
        "if [[ -f \"$GCP_CURL_COUNT\" ]]; then read -r count < \"$GCP_CURL_COUNT\"; fi\n"
        "count=$((count + 1))\n"
        "printf '%s\\n' \"$count\" > \"$GCP_CURL_COUNT\"\n"
        "fd_header=false\n"
        "if [[ -v ID_TOKEN ]]; then touch \"$GCP_SENTINEL/env-leak\"; fi\n"
        "for argument in \"$@\"; do\n"
        "  if [[ \"$argument\" == *generated-runtime-token-* ]]; then touch \"$GCP_SENTINEL/argv-leak\"; fi\n"
        "  if [[ \"$argument\" == '@/dev/fd/3' ]]; then fd_header=true; fi\n"
        "done\n"
        "if ! $fd_header; then touch \"$GCP_SENTINEL/header-invalid\"; else\n"
        "  IFS= read -r header <&3 || true\n"
        "  if [[ \"$header\" != Authorization:* || \"$header\" != *Bearer* || \"$header\" != *generated-runtime-token-* ]]; then touch \"$GCP_SENTINEL/header-invalid\"; fi\n"
        "  touch \"$GCP_SENTINEL/header-$count\"\n"
        "fi\n"
        "case \"$count\" in\n"
        "  1) printf '%s\\n' '{\"job_id\":\"fixed-job\",\"status\":\"queued\"}' ;;\n"
        "  2) printf '%s\\n' '{\"status\":\"running\",\"stdout_url\":null,\"stderr_url\":null}' ;;\n"
        "  3) printf '%s\\n' '{\"status\":\"completed\",\"stdout_url\":null,\"stderr_url\":null}' ;;\n"
        "  *) exit 98 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    fake_curl.chmod(0o700)
    capability_manifest = tmp_path / "capabilities.json"
    capability_manifest.write_text(
        '{"runtimes":{"python":{"enabled":true}}}\n',
        encoding="utf-8",
    )
    auth_helper = tmp_path / "auth-helper.py"
    auth_helper.write_text(
        "from pathlib import Path\n"
        "import os\n"
        "path = Path(os.environ['GCP_AUTH_COUNT'])\n"
        "count = int(path.read_text() if path.exists() else '0') + 1\n"
        "path.write_text(str(count))\n"
        "print(f'generated-runtime-token-{count}')\n",
        encoding="utf-8",
    )
    solver_script = tmp_path / "solve.py"
    solver_script.write_text("print('not executed')\n", encoding="utf-8")
    environment = dict(os.environ)
    environment["PATH"] = f"{fake_bin}:{environment['PATH']}"
    environment["CLOUD_SOLVER_CAPABILITIES_FILE"] = str(capability_manifest)
    environment["CLOUD_SOLVER_AUTH_HELPER"] = str(auth_helper)
    environment["GCLOUD_BIN"] = str(fake_gcloud)
    environment["GSUTIL_BIN"] = str(fake_gsutil)
    environment["PYTHON_BIN"] = sys.executable
    environment["GCP_AUTH_COUNT"] = str(auth_count)
    environment["GCP_CURL_COUNT"] = str(curl_count)
    environment["GCP_SENTINEL"] = str(sentinels)
    environment["TMPDIR"] = str(runtime_tmp)

    completed = subprocess.run(
        [
            "bash",
            "scripts/gcp_solver_client.sh",
            "--type",
            "python",
            "--script",
            str(solver_script),
            "--job-id",
            "fixed-job",
        ],
        cwd=ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=10,
        check=False,
    )

    output_contains_token = "generated-runtime-token-" in completed.stdout
    temporary_contains_token = any(
        b"generated-runtime-token-" in path.read_bytes()
        for path in runtime_tmp.rglob("*")
        if path.is_file()
    )
    assert completed.returncode == 0
    assert auth_count.read_text(encoding="utf-8") == "3"
    assert curl_count.read_text(encoding="utf-8").strip() == "3"
    assert all((sentinels / f"header-{number}").is_file() for number in (1, 2, 3))
    assert not (sentinels / "header-invalid").exists()
    assert not (sentinels / "argv-leak").exists()
    assert not (sentinels / "env-leak").exists()
    assert output_contains_token is False
    assert temporary_contains_token is False


def test_payload_policy_rejects_runtime_locks_and_sensitive_state_paths() -> None:
    cases = {
        ".claude/scheduled_tasks.lock": "runtime_lock_state",
        "nested/.claude/worker.lock": "runtime_lock_state",
        "web/auth.db": "database_state",
        "state/cache.sqlite": "database_state",
        "state/cache.sqlite3": "database_state",
        "runtime/cas/objects/sha256/aa/.put-deadbeef-1234": "cas_temporary_state",
        "node_modules/pkg/index.js": "dependency_or_cache_path",
        ".env.production": "environment_credential_file",
    }
    for path, expected_rule in cases.items():
        finding = payload_path_finding(path)
        assert finding is not None
        assert (finding.path, finding.rule) == (path, expected_rule)


@pytest.mark.parametrize(
    "path",
    (
        "web/auth.db-wal",
        "web/auth.db-shm",
        "web/auth.db-journal",
        "runtime/cache.sqlite-wal",
        "runtime/cache.sqlite-shm",
        "runtime/cache.sqlite-journal",
        "runtime/cache.sqlite3-wal",
        "RUNTIME/NESTED/AUTH.DB-WAL",
        r"runtime\nested\Auth.Db-ShM",
        r"C:\controller\acl\PROJECT.SQLITE-JOURNAL",
    ),
)
def test_payload_policy_rejects_normalized_sqlite_sidecars(path) -> None:
    finding = payload_path_finding(path)

    assert finding is not None
    assert (finding.path, finding.rule) == (path, "database_state")


def test_payload_policy_rejects_sidecars_from_a_real_active_sqlite_wal(
    tmp_path,
) -> None:
    database = tmp_path / "controller.sqlite"
    connection = sqlite3.connect(database)
    try:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        connection.execute("CREATE TABLE acl (project TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO acl VALUES ('generated-project')")
        connection.commit()

        runtime_paths = sorted(
            path.name
            for path in tmp_path.iterdir()
            if path.name.startswith(database.name)
        )
        assert f"{database.name}-wal" in runtime_paths
        assert f"{database.name}-shm" in runtime_paths
        for basename in runtime_paths:
            finding = payload_path_finding(f"runtime/acl/{basename}")
            assert finding is not None
            assert finding.rule == "database_state"
    finally:
        connection.close()


def test_payload_policy_allows_repository_config_and_test_log_fixture() -> None:
    assert payload_path_finding(".claude/settings.json") is None
    assert payload_path_finding(".env.example") is None
    assert payload_path_finding("web/.env.example") is None
    assert payload_path_finding("tests/fixtures/mini_proj/logs/solve.log") is None
    assert payload_path_finding("docs/sqlite-wal-recovery.md") is None
    assert payload_path_finding("tests/fixtures/database_state.json") is None
    assert payload_path_finding("backups/controller.sqlite-wal.txt") is None
    assert payload_path_finding(r"C:\docs\ordinary.txt").rule == "unsafe_archive_path"
