#!/usr/bin/env python3
"""Fetch image-gallery articles from dxs.moe.gov.cn list API.

Useful for dynamic catalog pages whose article rows are returned by
front/front/univs/content and include an imagePath list.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path


BASE = "https://dxs.moe.gov.cn"
API = BASE + "/front/front/univs/content"


def fetch(url: str, dest: Path, timeout: int = 90, retries: int = 3) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "KHTML, like Gecko) Chrome/124 Safari/537.36"
            ),
            "Referer": BASE + "/zx/",
        },
    )
    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                dest.write_bytes(resp.read())
            return
        except Exception as exc:
            last_err = exc
            if attempt < retries:
                time.sleep(2 * attempt)
    assert last_err is not None
    raise last_err


def build_api_url(alias: str, page_size: int) -> str:
    query = urllib.parse.urlencode(
        {
            "pageIndex": 0,
            "catalogAlias": alias,
            "hasAttribute": "",
            "siteID": 123,
            "pageSize": page_size,
            "contentType": "",
            "condition": "",
        }
    )
    return f"{API}?{query}"


def question_from_title(title: str) -> str | None:
    m = re.search(r"([A-E])题", title, re.I)
    return m.group(1).upper() if m else None


def safe_code(question: str, row: dict[str, object]) -> str:
    title = str(row.get("title") or "")
    m = re.search(r"([A-E])题", title, re.I)
    prefix = m.group(1).upper() if m else question
    return f"{prefix}{row.get('id')}"


def image_urls(row: dict[str, object]) -> list[str]:
    raw = str(row.get("imagePath") or "")
    return [u.strip() for u in raw.split(",") if u.strip()]


def assemble_pdf(image_paths: list[Path], pdf_path: Path) -> None:
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(["magick", *map(str, image_paths), str(pdf_path)], check=True)
        return
    except subprocess.CalledProcessError:
        pass

    # Large slide galleries can exceed ImageMagick's cache policy when decoded
    # all at once. Convert one page at a time, then concatenate PDFs.
    with tempfile.TemporaryDirectory(prefix=f"{pdf_path.stem}_pages_", dir=pdf_path.parent) as tmp:
        tmpdir = Path(tmp)
        page_pdfs: list[Path] = []
        for idx, img in enumerate(image_paths, start=1):
            page_pdf = tmpdir / f"page_{idx:03d}.pdf"
            subprocess.run(["magick", str(img), str(page_pdf)], check=True)
            page_pdfs.append(page_pdf)
        subprocess.run(["pdfunite", *map(str, page_pdfs), str(pdf_path)], check=True)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--alias", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--questions", default="ABC")
    ap.add_argument("--page-size", type=int, default=100)
    ap.add_argument("--force-pdf", action="store_true")
    ap.add_argument("--skip-images", action="store_true")
    ap.add_argument("--skip-pdf", action="store_true")
    ap.add_argument("--sleep", type=float, default=0.2)
    args = ap.parse_args(argv)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    api_path = out / "list_api.json"
    if not api_path.exists():
        fetch(build_api_url(args.alias, args.page_size), api_path)
    payload = json.loads(api_path.read_text(encoding="utf-8"))
    rows = payload.get("data", {}).get("data", [])
    keep = {q.upper() for q in args.questions if q.strip()}
    records: list[dict[str, object]] = []
    for row in rows:
        title = str(row.get("title") or "")
        question = question_from_title(title)
        if question not in keep:
            continue
        code = safe_code(question, row)
        paper_dir = out / f"{question}_{code}"
        img_dir = paper_dir / "images"
        img_dir.mkdir(parents=True, exist_ok=True)
        urls = image_urls(row)
        paths: list[Path] = []
        print(f"{code} images={len(urls)} {title}", flush=True)
        for idx, url in enumerate(urls, start=1):
            ext = Path(urllib.parse.urlparse(url).path).suffix.lower() or ".jpg"
            img_path = img_dir / f"page_{idx:02d}{ext}"
            paths.append(img_path)
            if args.skip_images or (img_path.exists() and img_path.stat().st_size > 0):
                continue
            print(f"  image {idx:02d}", flush=True)
            fetch(url, img_path)
            time.sleep(args.sleep)
        pdf_path = paper_dir / f"{code}.pdf"
        if not args.skip_pdf and paths and (args.force_pdf or not pdf_path.exists()):
            print(f"  assemble {pdf_path}", flush=True)
            assemble_pdf(paths, pdf_path)
        record = {
            "question": question,
            "code": code,
            "title": title,
            "author": row.get("author"),
            "url": row.get("artUrl"),
            "json": row.get("link"),
            "pages": len(urls),
            "dir": str(paper_dir),
            "pdf": str(pdf_path) if pdf_path.exists() else "",
        }
        records.append(record)
        (paper_dir / "metadata.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    (out / "manifest.json").write_text(
        json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"saved manifest: {out / 'manifest.json'} ({len(records)} records)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
