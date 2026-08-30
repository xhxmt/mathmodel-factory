from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import os
from pathlib import Path
import stat
import subprocess
import threading
import time

import pytest

from factory_core.artifact_ownership import ArtifactOwnership
from factory_core.owner_compiler import compile_owner_registry
from factory_core.phase3_artifacts import (
    build_artifact_occurrence,
    build_artifact_record,
    register_artifact_owner,
)
from factory_core.reference_materializer import (
    ReferenceBlockerCode,
    ReferenceCas,
    ReferenceMaterializationError,
    ReferenceMaterializerConfig,
    load_reference_package,
    materialize_reference_pdf,
    read_trusted_pdf,
    structured_reference_unavailable,
)


_ENCRYPTED_PDF_BASE64 = (
    "JVBERi0xLjIKJcK1wrYKCjEgMCBvYmoKPDwvVHlwZS9DYXRhbG9nL1BhZ2VzIDIgMCBSPj4KZW5kb2JqCgoyIDAgb2JqCjw8"
    "L1R5cGUvUGFnZXMvS2lkc1szIDAgUl0vQ291bnQgMT4+CmVuZG9iagoKMyAwIG9iago8PC9UeXBlL1BhZ2UvTWVkaWFCb3hb"
    "MCAwIDcyIDcyXS9QYXJlbnQgMiAwIFIvUmVzb3VyY2VzPDwvUHJvY1NldFsvUERGXT4+L0NvbnRlbnRzIDQgMCBSPj4KZW5k"
    "b2JqCgo0IDAgb2JqCjw8L0xlbmd0aCA4MC9GaWx0ZXIvRmxhdGVEZWNvZGU+PgpzdHJlYW0K0nFwcNltVKNUFkdofaySyrIW"
    "dmK60aKOPpae5UDwzXwKXSYKW8GO1GMUhl8JVaA6pSBiGkUPaOOEAkHvxn5xWXodUyqmomeU6ZebIxOK5hQKZW5kc3RyZWFt"
    "CmVuZG9iagoKNSAwIG9iago1NQplbmRvYmoKCjYgMCBvYmoKPDwvQ3JlYXRpb25EYXRlPDY4MjIzQTUxMzBGQTNBQjRDMzRC"
    "NjJBQjQ3RTRDQ0UzOEM1MzM1NkVCRDI5NkNEODJFQzA4RDQ2OUU4NkJENkRDNUEzRkQ3RDI5QzhGMEY4RjkyNjVFQTcxRkVD"
    "NzY3Nj4vUHJvZHVjZXI8Mjg4QkY2MEJDMDVFODEwQkExNUI4NTU1ODJGQjhGNTNDNzMxNjg5OTJCN0M5NDFCREJEQzBDNzc4"
    "OTRCQUNFMEZEQzIzNDY5RTMzQjBGNTQwRTMxODlGOTc5MURDNzhGPj4+CmVuZG9iagoKeHJlZgowIDcKMDAwMDAwMDAwMCA2"
    "NTUzNiBmIAowMDAwMDAwMDE2IDAwMDAwIG4gCjAwMDAwMDAwNjIgMDAwMDAgbiAKMDAwMDAwMDExNCAwMDAwMCBuIAowMDAw"
    "MDAwMjIxIDAwMDAwIG4gCjAwMDAwMDAzNjkgMDAwMDAgbiAKMDAwMDAwMDM4OCAwMDAwMCBuIAoKdHJhaWxlcgo8PC9TaXpl"
    "IDcvSW5mbyA2IDAgUi9Sb290IDEgMCBSL0lEWzxCQkMwRDQ1Qzc2NkE0QkIyMzgyM0MxRjdFMTQzN0I1OT48MzE2MEYyQzA1"
    "NzExNkI2MTE4Q0QzNDcwOThGMTdDRjI+XS9FbmNyeXB0PDwvRmlsdGVyL1N0YW5kYXJkL1IgNi9WIDUvTGVuZ3RoIDI1Ni9Q"
    "IC00L0VuY3J5cHRNZXRhZGF0YSB0cnVlL1N0bUYvU3RkQ0YvU3RyRi9TdGRDRi9DRjw8L1N0ZENGPDwvQXV0aEV2ZW50L0Rv"
    "Y09wZW4vQ0ZNL0FFU1YzL0xlbmd0aCAzMj4+Pj4vTzw5OEE2NjNGNDRCNUM3NEU2M0I4MDRGNjVBMkVCNjM0QzU4REQxQTY2"
    "QzE0NkZDQzEwMDU5RjMzMTM5NDhEQTFEQkJFNkIxNUI1QTRCQTUyOTQzNDQzMjExRjU3RTcxQkE+L1U8M0MyMTlCOThGMDdG"
    "QjdDQkNCM0E4NTNBQ0E1NjQ0MzNGQTM2Q0VGMzg1OUVBRTdDM0IxMjNEMDVFRUU3NjA5QzE3QUUxNUI1OTBBNTg5NUI5ODJC"
    "REM1NUU4NjhCRUYxPi9PRTxGOTk1NEU0MEI2Qzc1RkIwQTc0M0VEQ0ExQTREOTM3RTk2OUU1NENERUQwRTExMDc5NjZCMzFD"
    "OUE0MzFGNjJGPi9VRTxFQzQxNTRENDNBQ0U1NjBCOTE1MzBCMTUxNjE1MThGNzAwNTVEN0VCOThEQzkyRTM3RjhDQjVEMjVD"
    "NzdFOUVEPi9QZXJtczxBNkQ5NkE4Q0ZDMkJBQjJGRUExOTdEQzMwMkNEQzM2Rj4+Pj4+CnN0YXJ0eHJlZgo2MjcKJSVFT0YK"
)


def encrypted_pdf() -> bytes:
    raw = base64.b64decode(_ENCRYPTED_PDF_BASE64, validate=True)
    assert hashlib.sha256(raw).hexdigest() == (
        "9442fffc7c98f24bd872b52fae496958bc6c57afc64e598c0029005331d40444"
    )
    return raw


def make_pdf(text: str = "Phase Eight Reference", *, blank: bool = False) -> bytes:
    stream = (
        b""
        if blank
        else f"BT /F1 18 Tf 40 200 Td ({text}) Tj ET\n".encode("ascii")
    )
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 300] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"endstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    raw = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for ordinal, value in enumerate(objects, start=1):
        offsets.append(len(raw))
        raw.extend(f"{ordinal} 0 obj\n".encode())
        raw.extend(value)
        raw.extend(b"\nendobj\n")
    xref = len(raw)
    raw.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    raw.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        raw.extend(f"{offset:010d} 00000 n \n".encode())
    raw.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return bytes(raw)


def phase3_pdf_occurrence(raw: bytes, path: str = "references/source.pdf"):
    compilation = compile_owner_registry(
        (
            ArtifactOwnership(
                pattern="references/**",
                owner_stage=4,
                semantic_domain="canonical_reference",
                dirty_flag="REFERENCE_DIRTY",
            ),
        )
    )
    registration = register_artifact_owner(compilation, path)
    record = build_artifact_record(registration, content=raw)
    return build_artifact_occurrence(
        workflow_id="workflow-phase8",
        revision=1,
        command_id="command-phase8-1",
        mutation_sha256=hashlib.sha256(b"phase8-mutation-1").hexdigest(),
        artifact_record=record,
    )


def materializer_fixture(tmp_path: Path, *, raw: bytes | None = None):
    raw = make_pdf() if raw is None else raw
    project = tmp_path / "project"
    source = project / "references" / "source.pdf"
    source.parent.mkdir(parents=True)
    source.write_bytes(raw)
    cas = tmp_path / "cas"
    scratch = tmp_path / "scratch"
    cas.mkdir()
    scratch.mkdir()
    occurrence = phase3_pdf_occurrence(raw)
    result = materialize_reference_pdf(
        reference_id="reference-phase8-1",
        project_root=project,
        pdf_path=source,
        phase3_artifact_occurrence=occurrence,
        bibliographic_metadata={
            "title": "Phase Eight Reference",
            "authors": ["Ada Example"],
            "published_year": 2026,
            "doi": None,
        },
        external_share_classification="internal",
        cas_root=cas,
        scratch_root=scratch,
        config=ReferenceMaterializerConfig(render_dpi=72),
    )
    return result, occurrence, project, source, cas, scratch


def test_materializes_system_pdf_to_deterministic_cas_and_replays_without_source(tmp_path):
    result, _occurrence, _project, source, cas, scratch = materializer_fixture(tmp_path)

    assert result.record.pages[0].canonical_text.text == "Phase Eight Reference\n"
    assert result.record.egress_authority_granted is False
    assert result.package["authoritative"] is False
    assert result.package["toolchain"]["tools"]["pdfinfo"]["implementation"] == "/usr/bin/pdfinfo"
    assert not any(scratch.iterdir())
    source.unlink()

    loaded = load_reference_package(
        cas_root=cas,
        package_blob=result.package_blob,
        receipt_blob=result.receipt_blob,
    )
    assert loaded.package == result.package
    assert loaded.receipt == result.receipt
    assert loaded.record == result.record


def test_repeat_materialization_is_byte_identical_and_cas_idempotent(tmp_path):
    first, occurrence, project, source, cas, scratch = materializer_fixture(tmp_path)
    second = materialize_reference_pdf(
        reference_id="reference-phase8-1",
        project_root=project,
        pdf_path=source,
        phase3_artifact_occurrence=occurrence.as_dict(),
        bibliographic_metadata={
            "title": "Phase Eight Reference",
            "authors": ["Ada Example"],
            "published_year": 2026,
            "doi": None,
        },
        external_share_classification="internal",
        cas_root=cas,
        scratch_root=scratch,
        config=ReferenceMaterializerConfig(render_dpi=72),
    )
    assert second.package_blob == first.package_blob
    assert second.receipt_blob == first.receipt_blob
    assert second.package == first.package


def test_concurrent_same_bytes_cas_put_is_idempotent_without_temporary_residue(tmp_path):
    cas_root = tmp_path / "cas"
    cas_root.mkdir()
    cas = ReferenceCas(cas_root)
    content = b"phase8-concurrent-immutable-component" * 1024

    with ThreadPoolExecutor(max_workers=8) as pool:
        facts = list(pool.map(lambda _ordinal: cas.put(content), range(16)))

    assert len({fact.sha256 for fact in facts}) == 1
    assert len({fact.blob_ref for fact in facts}) == 1
    assert len({fact.byte_length for fact in facts}) == 1
    final = cas._path(facts[0].sha256)
    metadata = final.lstat()
    assert stat.S_ISREG(metadata.st_mode)
    assert stat.S_IMODE(metadata.st_mode) == 0o400
    assert metadata.st_nlink == 1
    assert final.read_bytes() == content
    assert [path for path in cas_root.rglob(".put-*")] == []
    assert [path for path in cas_root.rglob("*") if path.is_file()] == [final]


def test_reader_waits_until_hard_link_publication_has_one_name(
    tmp_path, monkeypatch
):
    cas_root = tmp_path / "cas"
    cas_root.mkdir()
    cas = ReferenceCas(cas_root)
    content = b"phase8-controlled-link-publication" * 1024
    linked = threading.Event()
    release = threading.Event()
    original_link = os.link
    first_link = True
    first_lock = threading.Lock()

    def paused_link(*args, **kwargs):
        nonlocal first_link
        result = original_link(*args, **kwargs)
        with first_lock:
            should_pause = first_link
            first_link = False
        if should_pause:
            linked.set()
            if not release.wait(timeout=5):
                raise TimeoutError("controlled CAS publisher was not released")
        return result

    monkeypatch.setattr(os, "link", paused_link)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(cas.put, content)
        assert linked.wait(timeout=2)
        second = pool.submit(cas.put, content)
        try:
            time.sleep(0.05)
            assert second.done() is False
        finally:
            release.set()
        first_fact = first.result(timeout=2)
        second_fact = second.result(timeout=2)

    assert first_fact == second_fact
    final = cas._path(first_fact.sha256)
    assert final.stat().st_nlink == 1
    assert final.read_bytes() == content
    assert [path for path in cas_root.rglob(".put-*")] == []


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        (make_pdf(blank=True), ReferenceBlockerCode.TEXT_NOT_PRESENT.value),
        (b"%PDF-1.4\nnot-a-pdf\n", ReferenceBlockerCode.PDF_MALFORMED.value),
    ],
)
def test_unavailable_pdf_has_stable_path_free_error(tmp_path, raw, code):
    project = tmp_path / "project"
    source = project / "references" / "source.pdf"
    source.parent.mkdir(parents=True)
    source.write_bytes(raw)
    cas, scratch = tmp_path / "cas", tmp_path / "scratch"
    cas.mkdir(); scratch.mkdir()
    with pytest.raises(ReferenceMaterializationError) as caught:
        materialize_reference_pdf(
            reference_id="reference-phase8-1",
            project_root=project,
            pdf_path=source,
            phase3_artifact_occurrence=phase3_pdf_occurrence(raw),
            bibliographic_metadata={
                "title": "Unavailable", "authors": ["Ada"],
                "published_year": 2026, "doi": None,
            },
            external_share_classification="internal",
            cas_root=cas,
            scratch_root=scratch,
            config=ReferenceMaterializerConfig(render_dpi=72),
        )
    unavailable = structured_reference_unavailable(caught.value)
    assert unavailable["reason_code"] == code
    assert str(source) not in str(unavailable)
    assert unavailable["dispatch_performed"] is False


def test_real_encrypted_pdf_is_structured_unavailable_without_temporary_residue(
    tmp_path,
):
    raw = encrypted_pdf()
    project = tmp_path / "project"
    source = project / "references" / "source.pdf"
    source.parent.mkdir(parents=True)
    source.write_bytes(raw)
    cas, scratch = tmp_path / "cas", tmp_path / "scratch"
    cas.mkdir()
    scratch.mkdir()

    inspected = subprocess.run(
        ["/usr/bin/pdfinfo", os.fspath(source)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        env={**os.environ, "LC_ALL": "C", "LANG": "C"},
        timeout=10,
    )
    assert inspected.returncode == 0
    assert b"Encrypted:       yes" in inspected.stdout

    with pytest.raises(ReferenceMaterializationError) as caught:
        materialize_reference_pdf(
            reference_id="reference-phase8-encrypted",
            project_root=project,
            pdf_path=source,
            phase3_artifact_occurrence=phase3_pdf_occurrence(raw),
            bibliographic_metadata={
                "title": "Encrypted Reference",
                "authors": ["Ada"],
                "published_year": 2026,
                "doi": None,
            },
            external_share_classification="internal",
            cas_root=cas,
            scratch_root=scratch,
            config=ReferenceMaterializerConfig(render_dpi=72),
        )

    unavailable = structured_reference_unavailable(caught.value)
    assert caught.value.code == ReferenceBlockerCode.PDF_ENCRYPTED.value
    assert unavailable == {
        "schema_version": "reference-materialization-unavailable-v1",
        "status": "UNAVAILABLE",
        "reason_code": "PDF_ENCRYPTED",
        "authoritative": False,
        "authority_transferred": False,
        "dispatch_performed": False,
    }
    assert str(source) not in str(caught.value)
    assert str(source) not in str(unavailable)
    assert "500" not in str(unavailable)
    assert not any(scratch.iterdir())
    assert not list(cas.rglob(".put-*"))


def test_symlink_and_phase3_byte_drift_fail_closed_before_tools(tmp_path, monkeypatch):
    raw = make_pdf()
    project = tmp_path / "project"
    references = project / "references"
    references.mkdir(parents=True)
    real = references / "real.pdf"
    real.write_bytes(raw)
    link = references / "source.pdf"
    link.symlink_to(real.name)
    cas, scratch = tmp_path / "cas", tmp_path / "scratch"
    cas.mkdir(); scratch.mkdir()
    called = False

    def poison(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("tool must not run")

    monkeypatch.setattr("factory_core.reference_materializer.subprocess.run", poison)
    with pytest.raises(ReferenceMaterializationError) as caught:
        materialize_reference_pdf(
            reference_id="reference-phase8-1",
            project_root=project,
            pdf_path=link,
            phase3_artifact_occurrence=phase3_pdf_occurrence(raw),
            bibliographic_metadata={"title": "T", "authors": ["A"], "published_year": 2026, "doi": None},
            external_share_classification="internal",
            cas_root=cas,
            scratch_root=scratch,
        )
    assert caught.value.code == ReferenceBlockerCode.INPUT_SYMLINK.value
    assert called is False

    link.unlink(); link.write_bytes(make_pdf("Changed"))
    with pytest.raises(ReferenceMaterializationError) as caught:
        materialize_reference_pdf(
            reference_id="reference-phase8-1",
            project_root=project,
            pdf_path=link,
            phase3_artifact_occurrence=phase3_pdf_occurrence(raw),
            bibliographic_metadata={"title": "T", "authors": ["A"], "published_year": 2026, "doi": None},
            external_share_classification="internal",
            cas_root=cas,
            scratch_root=scratch,
        )
    assert caught.value.code == ReferenceBlockerCode.INPUT_IDENTITY_MISMATCH.value
    assert called is False


def test_cas_restart_detects_missing_and_mutable_blob(tmp_path):
    result, _occurrence, _project, _source, cas_root, _scratch = materializer_fixture(tmp_path)
    cas = ReferenceCas(cas_root)
    raw_fact = result.package["components"]["raw_pdf"]
    path = cas._path(raw_fact["sha256"])
    os.chmod(path, 0o600)
    with pytest.raises(ReferenceMaterializationError) as caught:
        load_reference_package(
            cas_root=cas_root,
            package_blob=result.package_blob,
            receipt_blob=result.receipt_blob,
        )
    assert caught.value.code == ReferenceBlockerCode.CAS_CORRUPT.value


def test_one_total_deadline_is_checked_across_materialization(tmp_path):
    class Budget:
        def __init__(self):
            self.calls: list[str] = []

        def check(self, stage):
            self.calls.append(stage)
            if len(self.calls) > 6:
                raise TimeoutError("expired")

        def remaining_seconds(self):
            return 10.0

    raw = make_pdf()
    project = tmp_path / "project"
    source = project / "references" / "source.pdf"
    source.parent.mkdir(parents=True); source.write_bytes(raw)
    cas, scratch = tmp_path / "cas", tmp_path / "scratch"
    cas.mkdir(); scratch.mkdir()
    budget = Budget()
    with pytest.raises(ReferenceMaterializationError) as caught:
        materialize_reference_pdf(
            reference_id="reference-phase8-1",
            project_root=project,
            pdf_path=source,
            phase3_artifact_occurrence=phase3_pdf_occurrence(raw),
            bibliographic_metadata={"title": "T", "authors": ["A"], "published_year": 2026, "doi": None},
            external_share_classification="internal",
            cas_root=cas,
            scratch_root=scratch,
            config=ReferenceMaterializerConfig(render_dpi=72),
            deadline=budget,
        )
    assert caught.value.code == ReferenceBlockerCode.DEADLINE_EXCEEDED.value
    assert len(budget.calls) > 1


def test_missing_input_is_a_structured_unavailable_fact(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    with pytest.raises(ReferenceMaterializationError) as caught:
        read_trusted_pdf(
            project_root=project,
            pdf_path=project / "missing.pdf",
            maximum_bytes=1024,
        )
    assert caught.value.code == ReferenceBlockerCode.INPUT_NOT_REGULAR.value
    unavailable = structured_reference_unavailable(caught.value)
    assert unavailable == {
        "schema_version": "reference-materialization-unavailable-v1",
        "status": "UNAVAILABLE",
        "reason_code": "INPUT_NOT_REGULAR",
        "authoritative": False,
        "authority_transferred": False,
        "dispatch_performed": False,
    }


def test_render_and_text_outputs_are_bounded(tmp_path):
    raw = make_pdf()
    project = tmp_path / "project"
    source = project / "references" / "source.pdf"
    source.parent.mkdir(parents=True)
    source.write_bytes(raw)
    cas, scratch = tmp_path / "cas", tmp_path / "scratch"
    cas.mkdir(); scratch.mkdir()
    with pytest.raises(ReferenceMaterializationError) as caught:
        materialize_reference_pdf(
            reference_id="reference-phase8-1",
            project_root=project,
            pdf_path=source,
            phase3_artifact_occurrence=phase3_pdf_occurrence(raw),
            bibliographic_metadata={
                "title": "T", "authors": ["A"],
                "published_year": 2026, "doi": None,
            },
            external_share_classification="internal",
            cas_root=cas,
            scratch_root=scratch,
            config=ReferenceMaterializerConfig(
                render_dpi=72,
                maximum_component_bytes=16,
                maximum_total_output_bytes=64,
            ),
        )
    assert caught.value.code == ReferenceBlockerCode.OUTPUT_TOO_LARGE.value
    assert not any(scratch.iterdir())
