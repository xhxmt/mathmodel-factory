#!/usr/bin/env python3
"""Deterministic PDF render and layout gate for final judge submissions.

The gate reports only machine-observable defects. It does not assign aesthetic
scores or infer human preference.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:
    from PIL import Image, ImageStat
except ImportError:  # pragma: no cover - exercised as an infrastructure failure
    Image = None
    ImageStat = None


SCHEMA_VERSION = "pdf-visual-gate-v1"
REQUIRED_TOOLS = ("pdfinfo", "pdffonts", "pdfimages", "mutool", "pdftoppm")


@dataclass(frozen=True)
class Finding:
    code: str
    severity: str
    message: str
    page: int | None = None
    evidence: dict[str, Any] | None = None


class VisualGateError(RuntimeError):
    """An infrastructure failure prevented a complete visual audit."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run(argv: list[str], *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            argv,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise VisualGateError(f"failed to run {argv[0]}: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[:500]
        raise VisualGateError(f"{argv[0]} exited {result.returncode}: {detail}")
    return result


def _parse_pdfinfo(text: str) -> dict[str, Any]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            values[key.strip()] = value.strip()
    try:
        pages = int(values["Pages"])
    except (KeyError, ValueError) as exc:
        raise VisualGateError("pdfinfo did not report a valid page count") from exc
    return {
        "pages": pages,
        "encrypted": values.get("Encrypted", "unknown").lower(),
        "javascript": values.get("JavaScript", "unknown").lower(),
        "page_size": values.get("Page size"),
        "pdf_version": values.get("PDF version"),
    }


def _parse_pdffonts(text: str) -> list[dict[str, Any]]:
    fonts: list[dict[str, Any]] = []
    for line in text.splitlines():
        tokens = line.split()
        if len(tokens) < 8 or tokens[0] in {"name", "---"} or set(tokens[0]) == {"-"}:
            continue
        if tokens[-1].isdigit() and tokens[-2].isdigit() and tokens[-5] in {"yes", "no"}:
            fonts.append(
                {
                    "name": tokens[0],
                    "embedded": tokens[-5] == "yes",
                    "subset": tokens[-4] == "yes",
                    "unicode": tokens[-3] == "yes",
                }
            )
    return fonts


def _parse_pdfimages(text: str) -> list[dict[str, Any]]:
    images: list[dict[str, Any]] = []
    for line in text.splitlines():
        tokens = line.split()
        if len(tokens) < 15 or not tokens[0].isdigit() or not tokens[1].isdigit():
            continue
        try:
            x_ppi = float(tokens[-4])
            y_ppi = float(tokens[-3])
            width = int(tokens[3])
            height = int(tokens[4])
        except (ValueError, IndexError):
            continue
        images.append(
            {
                "page": int(tokens[0]),
                "number": int(tokens[1]),
                "type": tokens[2],
                "width": width,
                "height": height,
                "x_ppi": x_ppi,
                "y_ppi": y_ppi,
            }
        )
    return images


def _numbers(value: str | None, expected: int) -> tuple[float, ...] | None:
    if not value:
        return None
    try:
        result = tuple(float(item) for item in value.split())
    except ValueError:
        return None
    return result if len(result) == expected else None


def _parse_bbox_xml(text: str) -> list[dict[str, Any]]:
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise VisualGateError("mutool bbox output is invalid XML") from exc
    pages: list[dict[str, Any]] = []
    for index, element in enumerate(root.findall(".//page"), start=1):
        pages.append(
            {
                "page": index,
                "content_bbox": _numbers(element.get("bbox"), 4),
                "media_box": _numbers(element.get("mediabox"), 4),
            }
        )
    return pages


def _bbox(value: Any) -> tuple[float, float, float, float] | None:
    if not isinstance(value, dict):
        return None
    try:
        x = float(value["x"])
        y = float(value["y"])
        width = float(value["w"])
        height = float(value["h"])
    except (KeyError, TypeError, ValueError):
        return None
    return x, y, x + width, y + height


def _intersection_ratio(
    first: tuple[float, float, float, float], second: tuple[float, float, float, float]
) -> float:
    left = max(first[0], second[0])
    top = max(first[1], second[1])
    right = min(first[2], second[2])
    bottom = min(first[3], second[3])
    intersection = max(0.0, right - left) * max(0.0, bottom - top)
    first_area = max(0.0, first[2] - first[0]) * max(0.0, first[3] - first[1])
    return intersection / first_area if first_area else 0.0


def _analyze_stext(value: dict[str, Any]) -> tuple[list[Finding], dict[str, Any]]:
    raw_pages = value.get("pages")
    if not isinstance(raw_pages, list):
        raise VisualGateError("mutool structured-text output has no pages")
    findings: list[Finding] = []
    minimum_font = math.inf
    text_lines = 0
    image_blocks = 0
    for page_number, page in enumerate(raw_pages, start=1):
        blocks = page.get("blocks") if isinstance(page, dict) else None
        if not isinstance(blocks, list):
            continue
        lines: list[tuple[str, tuple[float, float, float, float]]] = []
        images: list[tuple[float, float, float, float]] = []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "image":
                box = _bbox(block.get("bbox"))
                if box:
                    images.append(box)
                    image_blocks += 1
                continue
            if block.get("type") != "text":
                continue
            raw_lines = block.get("lines")
            if not isinstance(raw_lines, list):
                continue
            for line in raw_lines:
                if not isinstance(line, dict):
                    continue
                text = " ".join(str(line.get("text") or "").split())
                box = _bbox(line.get("bbox"))
                if not text or box is None:
                    continue
                text_lines += 1
                lines.append((text, box))
                font = line.get("font")
                size = font.get("size") if isinstance(font, dict) else None
                if isinstance(size, (int, float)) and size > 0:
                    minimum_font = min(minimum_font, float(size))
                    if size < 4.5:
                        findings.append(
                            Finding(
                                "UNREADABLY_SMALL_TEXT",
                                "blocking",
                                f"text is rendered at {size:g}pt",
                                page_number,
                                {"text": text[:120], "font_size": size},
                            )
                        )
                    elif size < 6.0:
                        findings.append(
                            Finding(
                                "VERY_SMALL_TEXT",
                                "warning",
                                f"text is rendered at {size:g}pt",
                                page_number,
                                {"text": text[:120], "font_size": size},
                            )
                        )

        for index, (text, box) in enumerate(lines):
            for other_text, other_box in lines[index + 1 :]:
                if text == other_text and len(text) >= 4 and _intersection_ratio(box, other_box) >= 0.85:
                    findings.append(
                        Finding(
                            "DUPLICATE_TEXT_OVERLAY",
                            "blocking",
                            "identical text is drawn twice at the same location",
                            page_number,
                            {"text": text[:160], "bbox": list(box)},
                        )
                    )
                    break
            if any(_intersection_ratio(box, image) >= 0.85 for image in images):
                findings.append(
                    Finding(
                        "TEXT_OVER_IMAGE",
                        "warning",
                        "text substantially overlaps an image object",
                        page_number,
                        {"text": text[:120], "bbox": list(box)},
                    )
                )
    return findings, {
        "text_lines": text_lines,
        "image_blocks": image_blocks,
        "minimum_font_pt": None if minimum_font is math.inf else minimum_font,
    }


def _analyze_tex_log(path: Path | None) -> list[Finding]:
    if path is None or not path.is_file():
        return []
    text = path.read_text(encoding="utf-8", errors="replace")
    findings: list[Finding] = []
    undefined_patterns = (
        r"LaTeX Warning: (?:Reference|Citation) .+ undefined",
        r"LaTeX Warning: There were undefined references",
        r"undefined citations?",
    )
    if any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in undefined_patterns):
        findings.append(
            Finding(
                "UNDEFINED_REFERENCE",
                "blocking",
                "the final LaTeX log contains unresolved references or citations",
                evidence={"log": path.name},
            )
        )
    for match in re.finditer(r"Overfull \\hbox \((\d+(?:\.\d+)?)pt too wide\)", text):
        excess = float(match.group(1))
        if excess >= 5:
            findings.append(
                Finding(
                    "OVERFULL_HBOX",
                    "blocking" if excess >= 15 else "warning",
                    f"LaTeX reports an overfull horizontal box ({excess:g}pt)",
                    evidence={"excess_pt": excess, "log": path.name},
                )
            )
    return findings


def _render_metrics(pdf: Path, expected_pages: int) -> tuple[list[Finding], list[dict[str, Any]]]:
    if Image is None or ImageStat is None:
        raise VisualGateError("Pillow is required for rendered-pixel checks")
    findings: list[Finding] = []
    metrics: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="pdf-visual-gate-") as directory:
        prefix = Path(directory) / "page"
        _run(["pdftoppm", "-png", "-r", "36", str(pdf), str(prefix)], timeout=240)
        images = sorted(Path(directory).glob("page-*.png"))
        if len(images) != expected_pages:
            raise VisualGateError(
                f"renderer produced {len(images)} pages for a {expected_pages}-page PDF"
            )
        for page_number, image_path in enumerate(images, start=1):
            with Image.open(image_path) as image:
                gray = image.convert("L")
                histogram = gray.histogram()
                pixels = gray.width * gray.height
                nonwhite = sum(histogram[:248])
                nonwhite_fraction = nonwhite / pixels if pixels else 0.0
                deviation = float(ImageStat.Stat(gray).stddev[0])
                edge = list(gray.crop((0, 0, gray.width, 1)).getdata())
                edge += list(gray.crop((0, gray.height - 1, gray.width, gray.height)).getdata())
                edge += list(gray.crop((0, 0, 1, gray.height)).getdata())
                edge += list(gray.crop((gray.width - 1, 0, gray.width, gray.height)).getdata())
                edge_dark = sum(value < 248 for value in edge)
                metrics.append(
                    {
                        "page": page_number,
                        "width_px": gray.width,
                        "height_px": gray.height,
                        "nonwhite_fraction": round(nonwhite_fraction, 6),
                        "grayscale_stddev": round(deviation, 4),
                        "dark_edge_pixels": edge_dark,
                    }
                )
                if nonwhite_fraction < 0.0002 or deviation < 0.5:
                    findings.append(
                        Finding(
                            "BLANK_RENDERED_PAGE",
                            "blocking",
                            "rendered page is effectively blank",
                            page_number,
                            {"nonwhite_fraction": nonwhite_fraction, "stddev": deviation},
                        )
                    )
                if edge_dark >= max(10, int((gray.width + gray.height) * 0.02)):
                    findings.append(
                        Finding(
                            "CONTENT_TOUCHES_RENDER_EDGE",
                            "blocking",
                            "rendered content touches the physical page edge and may be clipped",
                            page_number,
                            {"dark_edge_pixels": edge_dark},
                        )
                    )
    return findings, metrics


def audit_pdf(
    pdf: Path,
    *,
    tex_log: Path | None = None,
    max_pages: int | None = None,
    minimum_image_ppi: float = 120.0,
    blocking_image_ppi: float = 72.0,
) -> dict[str, Any]:
    pdf = pdf.resolve()
    if not pdf.is_file() or pdf.stat().st_size == 0:
        raise VisualGateError(f"PDF is missing or empty: {pdf}")
    missing_tools = [tool for tool in REQUIRED_TOOLS if shutil.which(tool) is None]
    if missing_tools:
        raise VisualGateError(f"required PDF tools are unavailable: {', '.join(missing_tools)}")

    info = _parse_pdfinfo(_run(["pdfinfo", str(pdf)]).stdout)
    findings: list[Finding] = []
    if info["encrypted"] not in {"no", "unknown"}:
        findings.append(Finding("ENCRYPTED_PDF", "blocking", "submission PDF is encrypted"))
    if info["javascript"] not in {"no", "unknown"}:
        findings.append(Finding("PDF_JAVASCRIPT", "blocking", "submission PDF contains JavaScript"))
    if max_pages is not None and info["pages"] > max_pages:
        findings.append(
            Finding(
                "PAGE_LIMIT_EXCEEDED",
                "blocking",
                f"PDF has {info['pages']} pages; configured limit is {max_pages}",
                evidence={"pages": info["pages"], "limit": max_pages},
            )
        )

    fonts = _parse_pdffonts(_run(["pdffonts", str(pdf)]).stdout)
    for font in fonts:
        if not font["embedded"]:
            findings.append(
                Finding(
                    "FONT_NOT_EMBEDDED",
                    "blocking",
                    f"font is not embedded: {font['name']}",
                    evidence={"font": font["name"]},
                )
            )

    images = _parse_pdfimages(_run(["pdfimages", "-list", str(pdf)]).stdout)
    for image in images:
        if image["type"] in {"mask", "smask"}:
            continue
        ppi = min(image["x_ppi"], image["y_ppi"])
        if ppi < minimum_image_ppi:
            severity = "blocking" if ppi < blocking_image_ppi else "warning"
            findings.append(
                Finding(
                    "LOW_IMAGE_RESOLUTION",
                    severity,
                    f"embedded image effective resolution is {ppi:g} PPI",
                    image["page"],
                    {"image_number": image["number"], "x_ppi": image["x_ppi"], "y_ppi": image["y_ppi"]},
                )
            )

    bbox_pages = _parse_bbox_xml(_run(["mutool", "draw", "-F", "bbox", "-o", "-", str(pdf)]).stdout)
    for page in bbox_pages:
        content = page["content_bbox"]
        media = page["media_box"]
        if content is None or media is None:
            continue
        if (
            content[0] < media[0] - 0.5
            or content[1] < media[1] - 0.5
            or content[2] > media[2] + 0.5
            or content[3] > media[3] + 0.5
        ):
            findings.append(
                Finding(
                    "CONTENT_OUTSIDE_MEDIA_BOX",
                    "blocking",
                    "PDF content extends outside the physical page box",
                    page["page"],
                    {"content_bbox": content, "media_box": media},
                )
            )

    structured_text = json.loads(
        _run(["mutool", "draw", "-F", "stext.json", "-o", "-", str(pdf)], timeout=240).stdout
    )
    structure_findings, structure_metrics = _analyze_stext(structured_text)
    findings.extend(structure_findings)
    findings.extend(_analyze_tex_log(tex_log))
    render_findings, render_metrics = _render_metrics(pdf, info["pages"])
    findings.extend(render_findings)

    unique: list[Finding] = []
    seen: set[tuple[Any, ...]] = set()
    for finding in findings:
        key = (finding.code, finding.severity, finding.page, json.dumps(finding.evidence, sort_keys=True))
        if key not in seen:
            seen.add(key)
            unique.append(finding)
    blocking = sum(item.severity == "blocking" for item in unique)
    return {
        "schema": SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "status": "FAIL" if blocking else "PASS",
        "scope": "MACHINE_OBSERVABLE_RENDER_AND_LAYOUT_ONLY",
        "input": {"path": str(pdf), "bytes": pdf.stat().st_size, "sha256": _sha256(pdf)},
        "configuration": {
            "max_pages": max_pages,
            "minimum_image_ppi": minimum_image_ppi,
            "blocking_image_ppi": blocking_image_ppi,
        },
        "pdf": info,
        "metrics": {
            "fonts": len(fonts),
            "unembedded_fonts": sum(not item["embedded"] for item in fonts),
            "images": len(images),
            **structure_metrics,
            "rendered_pages": render_metrics,
        },
        "blocking_findings": blocking,
        "warning_findings": sum(item.severity == "warning" for item in unique),
        "findings": [asdict(item) for item in unique],
        "limitations": [
            "No aesthetic or human-preference judgment is made.",
            "Vector-object collisions without duplicate text may require a dedicated layout oracle.",
        ],
    }


def _atomic_write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    temp: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
            temp = Path(handle.name)
        os.replace(temp, path)
        temp = None
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf")
    parser.add_argument("--tex-log")
    parser.add_argument("--output")
    parser.add_argument("--max-pages", type=int)
    parser.add_argument("--minimum-image-ppi", type=float, default=120.0)
    parser.add_argument("--blocking-image-ppi", type=float, default=72.0)
    args = parser.parse_args()
    output = Path(args.output) if args.output else Path(args.pdf).with_suffix(".visual-gate.json")
    try:
        report = audit_pdf(
            Path(args.pdf),
            tex_log=Path(args.tex_log) if args.tex_log else None,
            max_pages=args.max_pages,
            minimum_image_ppi=args.minimum_image_ppi,
            blocking_image_ppi=args.blocking_image_ppi,
        )
        _atomic_write(output, report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["status"] == "PASS" else 1
    except (OSError, json.JSONDecodeError, VisualGateError) as exc:
        report = {
            "schema": SCHEMA_VERSION,
            "generated_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
            "status": "INDETERMINATE",
            "scope": "MACHINE_OBSERVABLE_RENDER_AND_LAYOUT_ONLY",
            "error": str(exc),
            "findings": [],
        }
        _atomic_write(output, report)
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
