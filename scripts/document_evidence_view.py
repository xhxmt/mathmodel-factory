"""Bounded, source-bound XLSX cells and PDF page evidence. No model calls.

Raw sources and rendered pages are content-addressed packet assets. The text
view contains every stored cell, or every PDF page's text and image locator.
Availability of a view is not a claim that a judge has reviewed it.
"""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import struct
import subprocess
import tempfile
import xml.etree.ElementTree as ET
import zipfile

CONTRACT = "document-evidence-v1"
SUFFIXES = {".xlsx", ".pdf"}
MAX_SOURCE_BYTES = 4 * 1024 * 1024
MAX_EXPANDED_BYTES = 32 * 1024 * 1024
MAX_CELLS = 100_000
MAX_SHEETS = 32
MAX_PAGES = 32
MAX_IMAGES = 128
MAX_VIEW_BYTES = 2_000_000
MAX_ASSET_BYTES = 32 * 1024 * 1024
DPI = 144
_ASSET = re.compile(r"judge_packets/assets/[0-9a-f]{64}\.(xlsx|pdf|png)\Z")
_CELL = re.compile(r"([A-Z]{1,3})([1-9][0-9]{0,6})\Z")
NS = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
RID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def compact(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def asset(data, suffix):
    return {"path": f"judge_packets/assets/{sha(data)}{suffix}",
            "sha256": sha(data), "bytes": len(data)}


def read_asset(project, relative):
    if not isinstance(relative, str) or not _ASSET.fullmatch(relative):
        raise ValueError("invalid_document_asset_path")
    path = project / relative
    if path.is_symlink() or not path.resolve().is_relative_to(project.resolve()):
        raise ValueError("document_asset_outside_project")
    if path.stat().st_size > MAX_ASSET_BYTES:
        raise ValueError("document_asset_byte_limit")
    return path.read_bytes()


def _xml(data):
    if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise ValueError("xlsx_unsupported_xml_declaration")
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise ValueError("xlsx_invalid_xml") from exc


def xlsx_view(raw):
    try:
        return _xlsx_view(raw)
    except (zipfile.BadZipFile, zipfile.LargeZipFile, NotImplementedError, RuntimeError) as exc:
        raise ValueError("xlsx_invalid_or_unsupported_archive") from exc


def _xlsx_view(raw):
    """Preserve lexical numeric values, formulas/caches, styles, and all sheets.

    The common numeric case uses compact address=value rows. All other cells
    carry their complete XML (including type, style, formula, rich text). Shared
    strings and workbook/styles/relationships are included, never sampled.
    Workbook drawings, comments, external links, and embedded objects are not
    silently reduced to a cell view: their presence makes this reader reject.
    """
    if len(raw) > MAX_SOURCE_BYTES:
        raise ValueError("document_source_byte_limit")
    try:
        z = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as exc:
        raise ValueError("xlsx_invalid_zip") from exc
    with z:
        infos = z.infolist()
        names = [i.filename for i in infos]
        if (len(infos) > 512 or len(set(names)) != len(names)
                or sum(i.file_size for i in infos) > MAX_EXPANDED_BYTES):
            raise ValueError("xlsx_expansion_limit_or_duplicate_member")
        if any(i.flag_bits & 1 for i in infos):
            raise ValueError("xlsx_encrypted_member")
        allowed = re.compile(r"(?:\[Content_Types\]\.xml|_rels/\.rels|docProps/[^/]+\.xml|"
            r"xl/(?:workbook\.xml|styles\.xml|sharedStrings\.xml|calcChain\.xml|"
            r"_rels/workbook\.xml\.rels|theme/[^/]+\.xml|worksheets/sheet[0-9]+\.xml))\Z")
        if any(not n.endswith("/") and not allowed.fullmatch(n) for n in names):
            raise ValueError("xlsx_unsupported_member_requires_native_review")
        try:
            workbook = _xml(z.read("xl/workbook.xml"))
            rels = _xml(z.read("xl/_rels/workbook.xml.rels"))
        except KeyError as exc:
            raise ValueError("xlsx_missing_workbook") from exc
        links = {}
        for rel in rels:
            if rel.get("TargetMode") == "External":
                raise ValueError("xlsx_external_relationship")
            target = rel.get("Target", "")
            target = target.lstrip("/") if target.startswith("/") else "xl/" + target
            if ".." in PurePosixPath(target).parts:
                raise ValueError("xlsx_unsupported_relationship")
            if rel.get("Id") in links:
                raise ValueError("xlsx_duplicate_relationship")
            links[rel.get("Id")] = target
        sheets = workbook.findall("s:sheets/s:sheet", NS)
        if workbook.tag != "{" + NS["s"] + "}workbook" or not sheets or len(sheets) > MAX_SHEETS:
            raise ValueError("xlsx_sheet_limit")
        shared = []
        if "xl/sharedStrings.xml" in names:
            shared = list(_xml(z.read("xl/sharedStrings.xml")))
        lines = [CONTRACT + " XLSX", "Source SHA256: " + sha(raw),
                 "Cells: ROW number, then column=raw numeric; other cells are JSON-escaped XML.",
                 "Cell addresses are sheet + column + ROW. Empty unstored coordinates are blank.",
                 "All stored cells/sheets are included, including hidden ones. Formula caches are stored values, not recalculated."]
        # Preserve semantic metadata; XML is JSON-escaped to keep context framing unambiguous.
        for name in sorted(names):
            if name.endswith((".xml", ".rels")) and not name.startswith("xl/worksheets/"):
                root = _xml(z.read(name))
                if name.endswith(".rels") and any(rel.get("TargetMode") == "External" for rel in root):
                    raise ValueError("xlsx_external_relationship")
                lines.append("METADATA " + name + " " + compact(ET.tostring(root, encoding="unicode")))
        lines.append("RELATIONSHIPS " + compact(ET.tostring(rels, encoding="unicode")))
        count = 0
        records = []
        visited = set()
        for sheet in sheets:
            target = links.get(sheet.get(RID))
            if target not in names or target in visited:
                raise ValueError("xlsx_invalid_sheet_relationship")
            visited.add(target)
            root = _xml(z.read(target))
            if root.tag != "{" + NS["s"] + "}worksheet":
                raise ValueError("xlsx_invalid_worksheet")
            sheet_record = {"name": sheet.get("name"), "state": sheet.get("state", "visible"),
                            "member": target, "cells": 0}
            lines.append("SHEET " + compact(sheet_record))
            addresses = set()
            row_attributes = {}
            data = root.find("s:sheetData", NS)
            row_ids = set()
            for row in list(data) if data is not None else []:
                row_id = row.get("r")
                if (row.tag != "{" + NS["s"] + "}row" or row_id in row_ids
                        or not re.fullmatch(r"[1-9][0-9]{0,6}", row_id or "")
                        or int(row_id) > 1048576):
                    raise ValueError("xlsx_invalid_row")
                row_ids.add(row_id)
                if len(row_ids) > MAX_CELLS:
                    raise ValueError("xlsx_row_limit")
                tokens = ["ROW " + str(row_id)]
                for cell in row:
                    address = cell.get("r", "")
                    match = _CELL.fullmatch(address)
                    if (match is None or match[2] != row_id or address in addresses
                            or cell.tag != "{" + NS["s"] + "}c"):
                        raise ValueError("xlsx_invalid_cell_address")
                    addresses.add(address)
                    count += 1
                    if count > MAX_CELLS:
                        raise ValueError("xlsx_cell_limit")
                    value = cell.find("s:v", NS)
                    if cell.get("t") == "s":
                        try:
                            index = int(value.text)
                            if not 0 <= index < len(shared):
                                raise ValueError
                        except (AttributeError, TypeError, ValueError) as exc:
                            raise ValueError("xlsx_invalid_shared_string") from exc
                    if (set(cell.attrib) <= {"r", "t"} and cell.get("t", "n") == "n"
                            and len(cell) == 1 and value is not None and value.text is not None
                            and re.fullmatch(r"[-+0-9.eE]+", value.text)):
                        rendered = value.text
                    else:
                        rendered = compact(ET.tostring(cell, encoding="unicode"))
                    tokens.append(match[1] + "=" + rendered)
                lines.append("\t".join(tokens))
                # Hidden rows, row styles, heights, etc. are not dropped.
                attributes = {k: v for k, v in row.attrib.items() if k != "r"}
                if attributes:
                    row_attributes.setdefault(compact(attributes), []).append(int(row_id))
            for attributes, numbers in row_attributes.items():
                ranges = []
                for number in numbers:
                    if ranges and ranges[-1][1] + 1 == number:
                        ranges[-1][1] = number
                    else:
                        ranges.append([number, number])
                lines.append("ROW_METADATA " + compact({"ranges": ranges, "attributes": json.loads(attributes)}))
            sheet_record["cells"] = len(addresses)
            records.append(sheet_record)
            if data is not None:
                root.remove(data)
            lines.append("SHEET_METADATA " + compact(ET.tostring(root, encoding="unicode")))
        worksheet_members = {n for n in names if n.startswith("xl/worksheets/") and n.endswith(".xml")}
        if visited != worksheet_members:
            raise ValueError("xlsx_unmapped_worksheet")
        text = "\n".join(lines) + "\n"
        if len(text.encode()) > MAX_VIEW_BYTES:
            raise ValueError("document_view_byte_limit")
        return text, {"sheets": records, "cell_count": count}


def _run(argv):
    try:
        result = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=120, check=True, env=None)
        return result.stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError("pdf_render_failed:" + Path(argv[0]).name) from exc


def pdf_text(binding):
    lines = [CONTRACT + " PDF", "Source SHA256: " + binding["source_sha256"],
             "All pages are attached as images in the following order. Inspect images for graphical evidence; text alone is insufficient."]
    for page in binding["pages"]:
        lines.append("PAGE " + str(page["page"]) + " IMAGE " + page["image"]["path"]
                     + " SHA256 " + page["image"]["sha256"])
        lines.append("PAGE_TEXT " + compact(page["text"]))
    return "\n".join(lines) + "\n"


def render_file(path):
    if path.stat().st_size > MAX_SOURCE_BYTES:
        raise ValueError("document_source_byte_limit")
    raw = path.read_bytes()
    suffix = path.suffix.lower()
    source = asset(raw, suffix)
    assets = {source["path"]: raw}
    binding = {"contract": CONTRACT, "kind": suffix[1:], "source_sha256": sha(raw),
               "source_size": len(raw), "source_asset": source, "complete": True}
    if suffix == ".xlsx":
        text, details = xlsx_view(raw)
        binding.update(details)
    elif suffix == ".pdf":
        commands = {t: shutil.which(t) for t in ("pdfinfo", "pdftotext", "pdftoppm")}
        if not all(commands.values()):
            raise ValueError("pdf_renderer_unavailable")
        binding["renderer"] = {"dpi": DPI, "tools": {
            name: sha(Path(exe).read_bytes()) for name, exe in commands.items()}}
        with tempfile.TemporaryDirectory(prefix="judge-pdf-") as tmp:
            source_path = Path(tmp) / "source.pdf"
            source_path.write_bytes(raw)
            info = _run([commands["pdfinfo"], str(source_path)]).decode("utf-8", errors="strict")
            match = re.search(r"^Pages:\s+(\d+)\s*$", info, re.M)
            if match is None or not 1 <= int(match[1]) <= MAX_PAGES:
                raise ValueError("pdf_page_limit")
            binding["pages"] = []
            for number in range(1, int(match[1]) + 1):
                # -scale-to bounds unusual page dimensions while retaining the entire page.
                prefix = Path(tmp) / "page"
                _run([commands["pdftoppm"], "-f", str(number), "-l", str(number),
                      "-singlefile", "-r", str(DPI), "-scale-to", "2400", "-png", str(source_path), str(prefix)])
                png = prefix.with_suffix(".png").read_bytes()
                if not png.startswith(b"\x89PNG\r\n\x1a\n"):
                    raise ValueError("pdf_invalid_render")
                image = asset(png, ".png")
                assets[image["path"]] = png
                if sum(map(len, assets.values())) > MAX_ASSET_BYTES:
                    raise ValueError("document_asset_byte_limit")
                width, height = struct.unpack(">II", png[16:24])
                page_text = _run([commands["pdftotext"], "-f", str(number), "-l", str(number),
                    "-layout", "-enc", "UTF-8", str(source_path), "-"]).decode("utf-8", errors="strict")
                binding["pages"].append({"page": number, "image": image,
                    "width": width, "height": height, "text": page_text})
            binding["page_count"] = len(binding["pages"])
        text = pdf_text(binding)
    else:
        raise ValueError("unsupported_document")
    encoded = text.encode("utf-8")
    if len(encoded) > MAX_VIEW_BYTES:
        raise ValueError("document_view_byte_limit")
    binding.update(view_sha256=sha(encoded), view_bytes=len(encoded))
    return text, binding, assets


def complete_binding(item):
    b = item.get("document_review", {})
    return (isinstance(b, dict) and b.get("contract") == CONTRACT and b.get("kind") in {"xlsx", "pdf"}
            and b.get("complete") is True
            and b.get("source_sha256") == item.get("sha256")
            and b.get("source_size") == item.get("size")
            and b.get("view_sha256") == item.get("included_sha256")
            and b.get("view_bytes") == item.get("included_bytes"))


def records(binding):
    return [binding["source_asset"]] + [p["image"] for p in binding.get("pages", [])]


def manifest_assets(manifest):
    found = {}
    for item in manifest.get("files", []):
        if item.get("status") not in {"included", "alias"} or "document_review" not in item:
            continue
        for record in records(item["document_review"]):
            path = record["path"]
            if not _ASSET.fullmatch(path) or (path in found and found[path] != record):
                raise ValueError("invalid_document_asset_binding")
            found[path] = record
    return found


def image_records(manifest):
    # Keep page order, including identical images on different pages, and bind
    # every attachment to its source document/page. Canonical aliases add no copy.
    return [{"source": item["path"], "page": page["page"], **page["image"]}
            for item in manifest.get("files", []) if item.get("status") == "included"
            for page in item.get("document_review", {}).get("pages", [])]


def image_inputs(project, paths):
    if len(paths) > MAX_IMAGES:
        raise ValueError("judge_image_count_limit")
    result = []
    total = 0
    for path in paths:
        data = read_asset(project, path)
        if not path.endswith(".png") or not data.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("invalid_judge_image")
        total += len(data)
        if total > MAX_ASSET_BYTES:
            raise ValueError("judge_image_byte_limit")
        result.append({"path": path, "sha256": sha(data), "bytes": len(data)})
    return result


def verify_pdf_source(raw, binding):
    """Native boundary: rerender frozen source bytes, never trust page metadata.

    The path-free shadow grounding API cannot perform this I/O and therefore
    fails closed for PDF derivations. This does not grant shadow release authority.
    """
    with tempfile.TemporaryDirectory(prefix="verify-judge-pdf-") as tmp:
        path = Path(tmp) / "source.pdf"
        path.write_bytes(raw)
        _, actual, _ = render_file(path)
    if actual != binding:
        raise ValueError("pdf_source_derivation_mismatch")


def verify_view(text, item, loader, *, pdf_verifier=None):
    if not complete_binding(item):
        raise ValueError("document_binding_incomplete")
    b = item["document_review"]
    loaded = {}
    for r in records(b):
        if not _ASSET.fullmatch(r["path"]):
            raise ValueError("invalid_document_asset_path")
        data = loader(r["path"])
        if type(data) is not bytes or len(data) != r["bytes"] or sha(data) != r["sha256"]:
            raise ValueError("document_asset_changed")
        if r != asset(data, PurePosixPath(r["path"]).suffix):
            raise ValueError("document_asset_identity_mismatch")
        loaded[r["path"]] = data
    raw = loaded[b["source_asset"]["path"]]
    if sha(raw) != b["source_sha256"] or len(raw) != b["source_size"]:
        raise ValueError("document_source_mismatch")
    if b["kind"] == "xlsx":
        expected, details = xlsx_view(raw)
        if any(b.get(k) != v for k, v in details.items()):
            raise ValueError("xlsx_cell_map_mismatch")
    else:
        if pdf_verifier is None:
            raise ValueError("pdf_derivation_requires_native_verification")
        pdf_verifier(raw, b)
        if ([p["page"] for p in b["pages"]] != list(range(1, b["page_count"] + 1))
                or not 1 <= b["page_count"] <= MAX_PAGES or not b.get("renderer")):
            raise ValueError("pdf_page_map_mismatch")
        for p in b["pages"]:
            png = loaded[p["image"]["path"]]
            if (not png.startswith(b"\x89PNG\r\n\x1a\n")
                    or struct.unpack(">II", png[16:24]) != (p["width"], p["height"])):
                raise ValueError("pdf_image_mismatch")
        expected = pdf_text(b)
    if expected != text or sha(text.encode()) != b["view_sha256"]:
        raise ValueError("document_view_mismatch")
