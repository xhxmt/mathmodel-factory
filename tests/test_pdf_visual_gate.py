import json

from scripts.pdf_visual_gate import (
    _analyze_stext,
    _analyze_tex_log,
    _parse_bbox_xml,
    _parse_pdfimages,
    _parse_pdffonts,
)


def test_font_and_image_tables_are_parsed_from_poppler_output():
    fonts = _parse_pdffonts(
        "name type encoding emb sub uni object ID\n"
        "----------------------------------------------\n"
        "ABCDE+CMR10 Type 1 Builtin yes yes yes 4 0\n"
        "Helvetica Type 1 WinAnsi no no yes 8 0\n"
    )
    images = _parse_pdfimages(
        "page num type width height color comp bpc enc interp object ID x-ppi y-ppi size ratio\n"
        "1 0 image 800 600 rgb 3 8 image no 12 0 96 90 10K 3%\n"
    )

    assert [font["embedded"] for font in fonts] == [True, False]
    assert images == [
        {
            "page": 1,
            "number": 0,
            "type": "image",
            "width": 800,
            "height": 600,
            "x_ppi": 96.0,
            "y_ppi": 90.0,
        }
    ]


def test_structured_text_detects_duplicate_overlay_and_tiny_text():
    line = {
        "text": "duplicate result",
        "bbox": {"x": 10, "y": 20, "w": 100, "h": 8},
        "font": {"size": 4},
    }
    findings, metrics = _analyze_stext(
        {"pages": [{"blocks": [{"type": "text", "lines": [line, dict(line)]}]}]}
    )

    codes = {finding.code for finding in findings}
    assert "DUPLICATE_TEXT_OVERLAY" in codes
    assert "UNREADABLY_SMALL_TEXT" in codes
    assert metrics["minimum_font_pt"] == 4


def test_bbox_parser_preserves_content_and_media_boxes():
    pages = _parse_bbox_xml(
        '<?xml version="1.0"?><document><page bbox="1 2 3 4" mediabox="0 0 10 20" /></document>'
    )

    assert pages == [
        {"page": 1, "content_bbox": (1.0, 2.0, 3.0, 4.0), "media_box": (0.0, 0.0, 10.0, 20.0)}
    ]


def test_tex_log_classifies_undefined_refs_and_large_overflow_as_blocking(tmp_path):
    log = tmp_path / "paper.log"
    log.write_text(
        "LaTeX Warning: There were undefined references.\n"
        "Overfull \\hbox (18.5pt too wide) in paragraph\n",
        encoding="utf-8",
    )

    findings = _analyze_tex_log(log)

    assert {(item.code, item.severity) for item in findings} == {
        ("UNDEFINED_REFERENCE", "blocking"),
        ("OVERFULL_HBOX", "blocking"),
    }
