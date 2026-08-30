"""Pure Phase-8 canonical reference-evidence shadow validator.

The validator accepts only already-materialized facts.  It never opens a PDF,
reads a path, renders or OCRs a page, persists a record, or grants egress
authority.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from typing import Mapping

from factory_core.canonical import canonical_sha256


REFERENCE_EVIDENCE_SCHEMA = "canonical-reference-evidence-v1"
REFERENCE_DOCUMENT_RECORD_SCHEMA = "reference-document-record-v1"
REFERENCE_CHUNK_ID_SCHEMA = "reference-chunk-id-v1"

_SHA256_CHARACTERS = frozenset("0123456789abcdef")
_SHARE_CLASSIFICATIONS = frozenset(
    {"public", "internal", "confidential", "restricted"}
)


class ReferenceEvidenceError(ValueError):
    """Raised when materialized reference facts cannot form a canonical record."""


def _require_mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(
        not isinstance(key, str) for key in value
    ):
        raise ReferenceEvidenceError(f"{field} must be an object")
    return value


def _require_keys(
    value: Mapping[str, object], expected: set[str], field: str
) -> None:
    actual = set(value)
    if actual != expected:
        raise ReferenceEvidenceError(
            f"{field} fields must be exactly {sorted(expected)!r}"
        )


def _require_nonblank_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReferenceEvidenceError(f"{field} must be a non-blank string")
    return value.strip()


def _require_exact_nonblank_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReferenceEvidenceError(f"{field} must be a non-blank string")
    return value


def _require_sha256(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in _SHA256_CHARACTERS for character in value)
    ):
        raise ReferenceEvidenceError(f"{field} must be lowercase SHA-256 hex")
    return value


def _require_integer(
    value: object, field: str, *, minimum: int, maximum: int | None = None
) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ReferenceEvidenceError(f"{field} must be an integer >= {minimum}")
    if maximum is not None and value > maximum:
        raise ReferenceEvidenceError(f"{field} must be an integer <= {maximum}")
    return value


@dataclass(frozen=True)
class RawPdfFact:
    blob_ref: str
    sha256: str
    byte_length: int

    def as_dict(self) -> dict[str, object]:
        return {
            "blob_ref": self.blob_ref,
            "sha256": self.sha256,
            "byte_length": self.byte_length,
        }


@dataclass(frozen=True)
class PdfInspectionFact:
    status: str
    pdf_sha256: str
    page_count: int
    encrypted: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "pdf_sha256": self.pdf_sha256,
            "page_count": self.page_count,
            "encrypted": self.encrypted,
        }


@dataclass(frozen=True)
class PageRenderFact:
    media_type: str
    sha256: str
    byte_length: int
    width_px: int
    height_px: int

    def as_dict(self) -> dict[str, object]:
        return {
            "media_type": self.media_type,
            "sha256": self.sha256,
            "byte_length": self.byte_length,
            "width_px": self.width_px,
            "height_px": self.height_px,
        }


@dataclass(frozen=True)
class CanonicalPageTextFact:
    text: str
    sha256: str
    byte_length: int

    def as_dict(self) -> dict[str, object]:
        return {
            "text": self.text,
            "sha256": self.sha256,
            "byte_length": self.byte_length,
        }


@dataclass(frozen=True)
class ReferencePageFact:
    page_number: int
    page_label: str
    render: PageRenderFact
    canonical_text: CanonicalPageTextFact

    def as_dict(self) -> dict[str, object]:
        return {
            "page_number": self.page_number,
            "page_label": self.page_label,
            "render": self.render.as_dict(),
            "canonical_text": self.canonical_text.as_dict(),
        }


@dataclass(frozen=True)
class ReferenceChunkFact:
    chunk_id: str
    ordinal: int
    page_start: int
    page_end: int
    text: str
    text_sha256: str
    byte_length: int

    def as_dict(self) -> dict[str, object]:
        return {
            "chunk_id": self.chunk_id,
            "ordinal": self.ordinal,
            "page_start": self.page_start,
            "page_end": self.page_end,
            "text": self.text,
            "text_sha256": self.text_sha256,
            "byte_length": self.byte_length,
        }


@dataclass(frozen=True)
class BibliographicMetadata:
    title: str
    authors: tuple[str, ...]
    published_year: int
    doi: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "title": self.title,
            "authors": list(self.authors),
            "published_year": self.published_year,
            "doi": self.doi,
        }


@dataclass(frozen=True)
class MetadataProvenanceFact:
    field: str
    source_kind: str
    source_ref: str
    value_sha256: str

    def as_dict(self) -> dict[str, str]:
        return {
            "field": self.field,
            "source_kind": self.source_kind,
            "source_ref": self.source_ref,
            "value_sha256": self.value_sha256,
        }


@dataclass(frozen=True)
class ReferenceDocumentRecord:
    schema_version: str
    reference_id: str
    raw_pdf: RawPdfFact
    pdf_inspection: PdfInspectionFact
    pages: tuple[ReferencePageFact, ...]
    chunks: tuple[ReferenceChunkFact, ...]
    bibliographic_metadata: BibliographicMetadata
    metadata_provenance: tuple[MetadataProvenanceFact, ...]
    external_share_classification: str
    egress_authority_granted: bool = False

    def _identity_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "reference_id": self.reference_id,
            "raw_pdf": self.raw_pdf.as_dict(),
            "pdf_inspection": self.pdf_inspection.as_dict(),
            "pages": [page.as_dict() for page in self.pages],
            "chunks": [chunk.as_dict() for chunk in self.chunks],
            "bibliographic_metadata": self.bibliographic_metadata.as_dict(),
            "metadata_provenance": [
                fact.as_dict() for fact in self.metadata_provenance
            ],
            "external_share": {
                "classification": self.external_share_classification,
                "authority_granted": self.egress_authority_granted,
            },
        }

    @property
    def record_sha256(self) -> str:
        return canonical_sha256(self._identity_dict())

    def as_dict(self) -> dict[str, object]:
        result = self._identity_dict()
        result["record_sha256"] = self.record_sha256
        return result


def derive_reference_chunk_id(
    *,
    reference_id: str,
    ordinal: int,
    page_start: int,
    page_end: int,
    text_sha256: str,
) -> str:
    """Derive a chunk identity from its reference, order, page span and text."""

    return canonical_sha256(
        {
            "schema_version": REFERENCE_CHUNK_ID_SCHEMA,
            "reference_id": reference_id,
            "ordinal": ordinal,
            "page_start": page_start,
            "page_end": page_end,
            "text_sha256": text_sha256,
        }
    )


def _compile_raw_pdf(value: object) -> RawPdfFact:
    raw = _require_mapping(value, "raw_pdf")
    _require_keys(raw, {"blob_ref", "sha256", "byte_length"}, "raw_pdf")
    sha256 = _require_sha256(raw["sha256"], "raw_pdf.sha256")
    blob_ref = _require_nonblank_string(raw["blob_ref"], "raw_pdf.blob_ref")
    if blob_ref != f"sha256:{sha256}":
        raise ReferenceEvidenceError("raw_pdf.blob_ref must bind raw_pdf.sha256")
    return RawPdfFact(
        blob_ref=blob_ref,
        sha256=sha256,
        byte_length=_require_integer(
            raw["byte_length"], "raw_pdf.byte_length", minimum=1
        ),
    )


def _compile_inspection(value: object, raw_pdf: RawPdfFact) -> PdfInspectionFact:
    inspection = _require_mapping(value, "pdf_inspection")
    _require_keys(
        inspection,
        {"status", "pdf_sha256", "page_count", "encrypted"},
        "pdf_inspection",
    )
    if inspection["status"] != "valid":
        raise ReferenceEvidenceError("pdf_inspection.status must be valid")
    pdf_sha256 = _require_sha256(
        inspection["pdf_sha256"], "pdf_inspection.pdf_sha256"
    )
    if pdf_sha256 != raw_pdf.sha256:
        raise ReferenceEvidenceError("pdf_inspection must bind raw PDF hash")
    if inspection["encrypted"] is not False:
        raise ReferenceEvidenceError("pdf_inspection.encrypted must be false")
    return PdfInspectionFact(
        status="valid",
        pdf_sha256=pdf_sha256,
        page_count=_require_integer(
            inspection["page_count"], "pdf_inspection.page_count", minimum=1
        ),
        encrypted=False,
    )


def _compile_render(value: object, page_number: int) -> PageRenderFact:
    render = _require_mapping(value, f"pages[{page_number}].render")
    _require_keys(
        render,
        {"media_type", "sha256", "byte_length", "width_px", "height_px"},
        f"pages[{page_number}].render",
    )
    if render["media_type"] != "image/png":
        raise ReferenceEvidenceError("page render media_type must be image/png")
    return PageRenderFact(
        media_type="image/png",
        sha256=_require_sha256(render["sha256"], "page render sha256"),
        byte_length=_require_integer(
            render["byte_length"], "page render byte_length", minimum=1
        ),
        width_px=_require_integer(
            render["width_px"], "page render width_px", minimum=1
        ),
        height_px=_require_integer(
            render["height_px"], "page render height_px", minimum=1
        ),
    )


def _compile_page_text(value: object, page_number: int) -> CanonicalPageTextFact:
    fact = _require_mapping(value, f"pages[{page_number}].canonical_text")
    _require_keys(
        fact,
        {"text", "sha256", "byte_length"},
        f"pages[{page_number}].canonical_text",
    )
    text = _require_exact_nonblank_string(fact["text"], "canonical page text")
    encoded = text.encode("utf-8")
    sha256 = _require_sha256(fact["sha256"], "canonical page text sha256")
    if sha256 != canonical_sha256_bytes(encoded):
        raise ReferenceEvidenceError("canonical page text sha256 mismatch")
    byte_length = _require_integer(
        fact["byte_length"], "canonical page text byte_length", minimum=1
    )
    if byte_length != len(encoded):
        raise ReferenceEvidenceError("canonical page text byte_length mismatch")
    return CanonicalPageTextFact(text=text, sha256=sha256, byte_length=byte_length)


def canonical_sha256_bytes(value: bytes) -> str:
    """Hash exact materialized bytes without treating them as canonical JSON."""

    return hashlib.sha256(value).hexdigest()


def _compile_pages(value: object, page_count: int) -> tuple[ReferencePageFact, ...]:
    if not isinstance(value, list):
        raise ReferenceEvidenceError("pages must be an array")
    if len(value) != page_count:
        raise ReferenceEvidenceError("pages must contain exactly page_count entries")
    pages: list[ReferencePageFact] = []
    for expected_page_number, item in enumerate(value, start=1):
        page = _require_mapping(item, f"pages[{expected_page_number}]")
        _require_keys(
            page,
            {"page_number", "page_label", "render", "canonical_text"},
            f"pages[{expected_page_number}]",
        )
        page_number = _require_integer(
            page["page_number"], "page_number", minimum=1
        )
        if page_number != expected_page_number:
            raise ReferenceEvidenceError("pages must cover page_count in order")
        pages.append(
            ReferencePageFact(
                page_number=page_number,
                page_label=_require_nonblank_string(
                    page["page_label"], "page_label"
                ),
                render=_compile_render(page["render"], page_number),
                canonical_text=_compile_page_text(
                    page["canonical_text"], page_number
                ),
            )
        )
    return tuple(pages)


def _compile_chunks(
    value: object, *, reference_id: str, page_count: int
) -> tuple[ReferenceChunkFact, ...]:
    if not isinstance(value, list) or not value:
        raise ReferenceEvidenceError("chunks must be a non-empty array")
    chunks: list[ReferenceChunkFact] = []
    seen_chunk_ids: set[str] = set()
    for expected_ordinal, item in enumerate(value):
        chunk = _require_mapping(item, f"chunks[{expected_ordinal}]")
        _require_keys(
            chunk,
            {
                "chunk_id",
                "ordinal",
                "page_start",
                "page_end",
                "text",
                "text_sha256",
                "byte_length",
            },
            f"chunks[{expected_ordinal}]",
        )
        ordinal = _require_integer(chunk["ordinal"], "chunk ordinal", minimum=0)
        if ordinal != expected_ordinal:
            raise ReferenceEvidenceError("chunk ordinals must be contiguous from zero")
        page_start = _require_integer(
            chunk["page_start"], "chunk page_start", minimum=1
        )
        page_end = _require_integer(
            chunk["page_end"], "chunk page_end", minimum=1
        )
        if page_start > page_end or page_end > page_count:
            raise ReferenceEvidenceError("chunk page range is outside the PDF")
        text = _require_exact_nonblank_string(chunk["text"], "chunk text")
        encoded = text.encode("utf-8")
        text_sha256 = _require_sha256(
            chunk["text_sha256"], "chunk text_sha256"
        )
        if text_sha256 != canonical_sha256_bytes(encoded):
            raise ReferenceEvidenceError("chunk text_sha256 mismatch")
        byte_length = _require_integer(
            chunk["byte_length"], "chunk byte_length", minimum=1
        )
        if byte_length != len(encoded):
            raise ReferenceEvidenceError("chunk byte_length mismatch")
        chunk_id = _require_sha256(chunk["chunk_id"], "chunk_id")
        if chunk_id in seen_chunk_ids:
            raise ReferenceEvidenceError("chunk_id must be unique")
        expected_chunk_id = derive_reference_chunk_id(
            reference_id=reference_id,
            ordinal=ordinal,
            page_start=page_start,
            page_end=page_end,
            text_sha256=text_sha256,
        )
        if chunk_id != expected_chunk_id:
            raise ReferenceEvidenceError("chunk_id does not bind chunk identity")
        seen_chunk_ids.add(chunk_id)
        chunks.append(
            ReferenceChunkFact(
                chunk_id=chunk_id,
                ordinal=ordinal,
                page_start=page_start,
                page_end=page_end,
                text=text,
                text_sha256=text_sha256,
                byte_length=byte_length,
            )
        )
    return tuple(chunks)


def _compile_bibliography(value: object) -> BibliographicMetadata:
    metadata = _require_mapping(value, "bibliographic_metadata")
    _require_keys(
        metadata,
        {"title", "authors", "published_year", "doi"},
        "bibliographic_metadata",
    )
    authors_value = metadata["authors"]
    if not isinstance(authors_value, list) or not authors_value:
        raise ReferenceEvidenceError("bibliographic_metadata.authors must be an array")
    authors = tuple(
        _require_nonblank_string(author, "bibliographic author")
        for author in authors_value
    )
    if len(set(authors)) != len(authors):
        raise ReferenceEvidenceError("bibliographic authors must be unique")
    doi_value = metadata["doi"]
    doi = (
        None
        if doi_value is None
        else _require_nonblank_string(doi_value, "bibliographic_metadata.doi")
    )
    return BibliographicMetadata(
        title=_require_nonblank_string(
            metadata["title"], "bibliographic_metadata.title"
        ),
        authors=authors,
        published_year=_require_integer(
            metadata["published_year"],
            "bibliographic_metadata.published_year",
            minimum=1,
            maximum=9999,
        ),
        doi=doi,
    )


def _compile_provenance(
    value: object, metadata: BibliographicMetadata
) -> tuple[MetadataProvenanceFact, ...]:
    provenance = _require_mapping(value, "metadata_provenance")
    metadata_values = metadata.as_dict()
    if set(provenance) != set(metadata_values):
        raise ReferenceEvidenceError(
            "metadata_provenance must cover every bibliographic metadata field"
        )
    facts: list[MetadataProvenanceFact] = []
    for field in sorted(metadata_values):
        fact = _require_mapping(provenance[field], f"metadata_provenance.{field}")
        _require_keys(
            fact,
            {"source_kind", "source_ref", "value_sha256"},
            f"metadata_provenance.{field}",
        )
        value_sha256 = _require_sha256(
            fact["value_sha256"], f"metadata_provenance.{field}.value_sha256"
        )
        if value_sha256 != canonical_sha256(metadata_values[field]):
            raise ReferenceEvidenceError(
                f"metadata_provenance.{field} does not bind metadata value"
            )
        facts.append(
            MetadataProvenanceFact(
                field=field,
                source_kind=_require_nonblank_string(
                    fact["source_kind"],
                    f"metadata_provenance.{field}.source_kind",
                ),
                source_ref=_require_nonblank_string(
                    fact["source_ref"],
                    f"metadata_provenance.{field}.source_ref",
                ),
                value_sha256=value_sha256,
            )
        )
    return tuple(facts)


def validate_canonical_reference_evidence(
    evidence: Mapping[str, object],
) -> ReferenceDocumentRecord:
    """Validate materialized facts and compile a deterministic frozen record."""

    root = _require_mapping(evidence, "evidence")
    _require_keys(
        root,
        {
            "schema_version",
            "reference_id",
            "raw_pdf",
            "pdf_inspection",
            "pages",
            "chunks",
            "bibliographic_metadata",
            "metadata_provenance",
            "external_share_classification",
        },
        "evidence",
    )
    if root["schema_version"] != REFERENCE_EVIDENCE_SCHEMA:
        raise ReferenceEvidenceError("unsupported reference evidence schema")
    reference_id = _require_nonblank_string(root["reference_id"], "reference_id")
    raw_pdf = _compile_raw_pdf(root["raw_pdf"])
    inspection = _compile_inspection(root["pdf_inspection"], raw_pdf)
    pages = _compile_pages(root["pages"], inspection.page_count)
    chunks = _compile_chunks(
        root["chunks"], reference_id=reference_id, page_count=inspection.page_count
    )
    metadata = _compile_bibliography(root["bibliographic_metadata"])
    provenance = _compile_provenance(root["metadata_provenance"], metadata)
    classification = _require_nonblank_string(
        root["external_share_classification"],
        "external_share_classification",
    )
    if classification not in _SHARE_CLASSIFICATIONS:
        raise ReferenceEvidenceError("unsupported external share classification")
    return ReferenceDocumentRecord(
        schema_version=REFERENCE_DOCUMENT_RECORD_SCHEMA,
        reference_id=reference_id,
        raw_pdf=raw_pdf,
        pdf_inspection=inspection,
        pages=pages,
        chunks=chunks,
        bibliographic_metadata=metadata,
        metadata_provenance=provenance,
        external_share_classification=classification,
        egress_authority_granted=False,
    )
