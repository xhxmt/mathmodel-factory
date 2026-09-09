from __future__ import annotations

from copy import deepcopy
from dataclasses import FrozenInstanceError
import hashlib
import json

import pytest

from factory_core.canonical import canonical_sha256
from factory_core.reference_evidence import (
    REFERENCE_DOCUMENT_RECORD_SCHEMA,
    REFERENCE_EVIDENCE_SCHEMA,
    ReferenceEvidenceError,
    derive_reference_chunk_id,
    validate_canonical_reference_evidence,
)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _text_fact(text: str) -> dict[str, object]:
    return {
        "text": text,
        "sha256": _sha256_text(text),
        "byte_length": len(text.encode("utf-8")),
    }


def _chunk(
    reference_id: str, ordinal: int, page_start: int, page_end: int, text: str
) -> dict[str, object]:
    text_sha256 = _sha256_text(text)
    return {
        "chunk_id": derive_reference_chunk_id(
            reference_id=reference_id,
            ordinal=ordinal,
            page_start=page_start,
            page_end=page_end,
            text_sha256=text_sha256,
        ),
        "ordinal": ordinal,
        "page_start": page_start,
        "page_end": page_end,
        "text": text,
        "text_sha256": text_sha256,
        "byte_length": len(text.encode("utf-8")),
    }


def _evidence() -> dict[str, object]:
    raw_sha256 = "a" * 64
    reference_id = "reference-ordinary-two-page"
    metadata = {
        "title": "A Deterministic Reference",
        "authors": ["Ada Example", "Bao Example"],
        "published_year": 2026,
        "doi": "10.1000/example",
    }
    provenance = {
        field: {
            "source_kind": "declared-bibliography",
            "source_ref": f"packet:bibliography:{field}",
            "value_sha256": canonical_sha256(value),
        }
        for field, value in metadata.items()
    }
    return {
        "schema_version": REFERENCE_EVIDENCE_SCHEMA,
        "reference_id": reference_id,
        "raw_pdf": {
            "blob_ref": f"sha256:{raw_sha256}",
            "sha256": raw_sha256,
            "byte_length": 4096,
        },
        "pdf_inspection": {
            "status": "valid",
            "pdf_sha256": raw_sha256,
            "page_count": 2,
            "encrypted": False,
        },
        "pages": [
            {
                "page_number": 1,
                "page_label": "i",
                "render": {
                    "media_type": "image/png",
                    "sha256": "b" * 64,
                    "byte_length": 1200,
                    "width_px": 1000,
                    "height_px": 1400,
                },
                "canonical_text": _text_fact("Page one canonical text.\n"),
            },
            {
                "page_number": 2,
                "page_label": "1",
                "render": {
                    "media_type": "image/png",
                    "sha256": "c" * 64,
                    "byte_length": 1300,
                    "width_px": 1000,
                    "height_px": 1400,
                },
                "canonical_text": _text_fact("Page two canonical text with Ω.\n"),
            },
        ],
        "chunks": [
            _chunk(reference_id, 0, 1, 1, "Page one canonical text."),
            _chunk(reference_id, 1, 2, 2, "Page two canonical text with Ω."),
        ],
        "bibliographic_metadata": metadata,
        "metadata_provenance": provenance,
        "external_share_classification": "internal",
    }


def _replace(root: object, path: tuple[object, ...], value: object) -> None:
    target = root
    for part in path[:-1]:
        target = target[part]  # type: ignore[index]
    target[path[-1]] = value  # type: ignore[index]


def test_valid_two_page_reference_compiles_complete_canonical_record():
    record = validate_canonical_reference_evidence(_evidence())

    assert record.schema_version == REFERENCE_DOCUMENT_RECORD_SCHEMA
    assert record.pdf_inspection.page_count == 2
    assert [page.page_number for page in record.pages] == [1, 2]
    assert [page.page_label for page in record.pages] == ["i", "1"]
    assert [chunk.ordinal for chunk in record.chunks] == [0, 1]
    assert [(chunk.page_start, chunk.page_end) for chunk in record.chunks] == [
        (1, 1),
        (2, 2),
    ]
    assert record.raw_pdf.blob_ref == f"sha256:{record.raw_pdf.sha256}"


def test_compilation_is_deterministic_and_does_not_modify_input():
    evidence = _evidence()
    before = deepcopy(evidence)

    first = validate_canonical_reference_evidence(evidence)
    second = validate_canonical_reference_evidence(deepcopy(evidence))

    assert evidence == before
    assert first == second
    assert first.record_sha256 == second.record_sha256


def test_record_is_frozen_and_share_classification_never_grants_authority():
    record = validate_canonical_reference_evidence(_evidence())

    assert record.external_share_classification == "internal"
    assert record.egress_authority_granted is False
    assert record.as_dict()["external_share"] == {
        "classification": "internal",
        "authority_granted": False,
    }
    with pytest.raises(FrozenInstanceError):
        record.reference_id = "changed"  # type: ignore[misc]


def test_wire_record_round_trips_as_json_with_stable_record_hash():
    record = validate_canonical_reference_evidence(_evidence())
    wire = json.loads(json.dumps(record.as_dict(), ensure_ascii=False))

    assert wire["record_sha256"] == record.record_sha256
    assert canonical_sha256(
        {key: value for key, value in wire.items() if key != "record_sha256"}
    ) == record.record_sha256
    assert [item["field"] for item in wire["metadata_provenance"]] == [
        "authors",
        "doi",
        "published_year",
        "title",
    ]


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        (("schema_version",), "future-reference-v2", "unsupported.*schema"),
        (("reference_id",), "  ", "reference_id.*non-blank"),
        (("raw_pdf", "sha256"), "A" * 64, "lowercase SHA-256"),
        (("raw_pdf", "blob_ref"), "sha256:" + "b" * 64, "must bind"),
        (("raw_pdf", "byte_length"), True, "integer >= 1"),
        (("pdf_inspection", "status"), "warning", "status must be valid"),
        (("pdf_inspection", "pdf_sha256"), "d" * 64, "bind raw PDF hash"),
        (("pdf_inspection", "encrypted"), True, "encrypted must be false"),
        (("pdf_inspection", "page_count"), 0, "integer >= 1"),
        (("pages", 0, "page_label"), "", "page_label.*non-blank"),
        (("pages", 0, "render", "media_type"), "image/jpeg", "image/png"),
        (("pages", 0, "render", "sha256"), "z" * 64, "lowercase SHA-256"),
        (("pages", 0, "render", "byte_length"), 0, "integer >= 1"),
        (("pages", 0, "render", "width_px"), 0, "integer >= 1"),
        (("pages", 0, "render", "height_px"), False, "integer >= 1"),
        (("pages", 0, "canonical_text", "text"), "\n", "non-blank"),
        (("pages", 0, "canonical_text", "sha256"), "e" * 64, "sha256 mismatch"),
        (("pages", 0, "canonical_text", "byte_length"), 999, "byte_length mismatch"),
        (("pages", 1, "page_number"), 1, "cover page_count in order"),
        (("chunks", 1, "ordinal"), 7, "contiguous from zero"),
        (("chunks", 1, "page_end"), 3, "page range is outside"),
        (("chunks", 0, "text_sha256"), "f" * 64, "text_sha256 mismatch"),
        (("chunks", 0, "byte_length"), 999, "byte_length mismatch"),
        (("bibliographic_metadata", "published_year"), 0, "integer >= 1"),
    ],
)
def test_common_invalid_materialized_fact_fails_closed(path, value, message):
    evidence = _evidence()
    _replace(evidence, path, value)

    with pytest.raises(ReferenceEvidenceError, match=message):
        validate_canonical_reference_evidence(evidence)


def test_page_array_must_completely_cover_inspected_page_count():
    evidence = _evidence()
    evidence["pages"].pop()  # type: ignore[union-attr]

    with pytest.raises(ReferenceEvidenceError, match="exactly page_count"):
        validate_canonical_reference_evidence(evidence)


def test_chunk_id_must_bind_reference_ordinal_range_and_text_hash():
    evidence = _evidence()
    evidence["chunks"][0]["chunk_id"] = "0" * 64  # type: ignore[index]

    with pytest.raises(ReferenceEvidenceError, match="bind chunk identity"):
        validate_canonical_reference_evidence(evidence)


def test_duplicate_chunk_id_is_rejected_explicitly():
    evidence = _evidence()
    evidence["chunks"][1]["chunk_id"] = evidence["chunks"][0]["chunk_id"]  # type: ignore[index]

    with pytest.raises(ReferenceEvidenceError, match="chunk_id must be unique"):
        validate_canonical_reference_evidence(evidence)


def test_chunks_must_be_present():
    evidence = _evidence()
    evidence["chunks"] = []

    with pytest.raises(ReferenceEvidenceError, match="non-empty array"):
        validate_canonical_reference_evidence(evidence)


def test_bibliographic_authors_must_be_unique():
    evidence = _evidence()
    evidence["bibliographic_metadata"]["authors"] = ["Ada", "Ada"]  # type: ignore[index]

    with pytest.raises(ReferenceEvidenceError, match="authors must be unique"):
        validate_canonical_reference_evidence(evidence)


def test_provenance_must_cover_every_metadata_field():
    evidence = _evidence()
    del evidence["metadata_provenance"]["doi"]  # type: ignore[index]

    with pytest.raises(ReferenceEvidenceError, match="cover every"):
        validate_canonical_reference_evidence(evidence)


def test_each_provenance_fact_must_bind_its_normalized_metadata_value():
    evidence = _evidence()
    evidence["metadata_provenance"]["title"]["value_sha256"] = "0" * 64  # type: ignore[index]

    with pytest.raises(ReferenceEvidenceError, match="does not bind"):
        validate_canonical_reference_evidence(evidence)


def test_unknown_share_classification_is_rejected_without_egress_inference():
    evidence = _evidence()
    evidence["external_share_classification"] = "approved-to-upload"

    with pytest.raises(ReferenceEvidenceError, match="unsupported.*classification"):
        validate_canonical_reference_evidence(evidence)
