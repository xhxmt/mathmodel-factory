#!/usr/bin/env python3
"""Fetch 2024 CUMCM excellent-paper image galleries from dxs.moe.gov.cn.

The site publishes papers as article pages containing page images.  This script
builds a local index, downloads the page images, and optionally assembles each
paper into a PDF for downstream OCR.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path


BASE = "https://dxs.moe.gov.cn"
DEFAULT_INDEX = BASE + "/zx/hd/sxjm/sxjmlw/2024qgdxssxjmjslwzs/index.shtml"


def fetch(url: str, dest: Path, timeout: int = 60, retries: int = 3) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "KHTML, like Gecko) Chrome/124 Safari/537.36"
            )
        },
    )
    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                dest.write_bytes(resp.read())
            return
        except Exception as exc:  # network-facing utility: retry transient TLS/timeouts
            last_err = exc
            if attempt < retries:
                time.sleep(2 * attempt)
    assert last_err is not None
    raise last_err


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def extract_articles(index_html: str) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    pattern = re.compile(
        r"""href=['"](?P<href>/zx/a/hd_sxjm_sxjmlw_2024qgdxssxjmjslwzs_2024[abcde]tlw/[^'"]+?\.shtml)['"]""",
        re.I,
    )
    for match in pattern.finditer(index_html):
        href = match.group("href")
        url = urllib.parse.urljoin(BASE, href)
        if url not in seen:
            urls.append(url)
            seen.add(url)
    return urls


def strip_tags(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)).strip()


def extract_title(page_html: str) -> str:
    m = re.search(r"<div class=\"detail-tit\">\s*(.*?)\s*</div>", page_html, re.S)
    if not m:
        m = re.search(r"<title>\s*(.*?)\s*</title>", page_html, re.S)
    return html.unescape(strip_tags(m.group(1))) if m else ""


def extract_code(title: str, url: str) -> str:
    m = re.search(r"[（(]([A-E]\d+)[）)]", title, re.I)
    if m:
        return m.group(1).upper()
    m = re.search(r"2024([abcde])tlw", url, re.I)
    content = re.search(r"/(\d+)\.shtml$", url)
    q = m.group(1).upper() if m else "X"
    return f"{q}{content.group(1) if content else 'unknown'}"


def extract_question(url: str) -> str:
    m = re.search(r"2024([abcde])tlw", url, re.I)
    return m.group(1).upper() if m else "X"


def extract_page_images(page_html: str, code: str) -> list[dict[str, str]]:
    # Restrict to paper page images via alt text, avoiding recommendation images.
    items: list[dict[str, str]] = []
    img_pattern = re.compile(
        r"""<img\s+[^>]*src=['"](?P<src>https://univs-news-1256833609\.file\.myqcloud\.com/123/upload/resources/image/[^'"]+?\.(?P<ext>jpe?g|png))['"][^>]*alt=['"](?P<alt>[^'"]*页面_(?P<page>\d{2})\.(?:jpe?g|png))['"][^>]*>""",
        re.I,
    )
    for match in img_pattern.finditer(page_html):
        items.append(
            {
                "page": match.group("page"),
                "url": html.unescape(match.group("src")),
                "alt": html.unescape(match.group("alt")),
                "ext": match.group("ext").lower(),
            }
        )
    if not items:
        # Some pages may put alt before src.
        img_pattern = re.compile(
            r"""<img\s+[^>]*alt=['"](?P<alt>[^'"]*页面_(?P<page>\d{2})\.(?:jpe?g|png))['"][^>]*src=['"](?P<src>https://univs-news-1256833609\.file\.myqcloud\.com/123/upload/resources/image/[^'"]+?\.(?P<ext>jpe?g|png))['"][^>]*>""",
            re.I,
        )
        for match in img_pattern.finditer(page_html):
            items.append(
                {
                    "page": match.group("page"),
                    "url": html.unescape(match.group("src")),
                    "alt": html.unescape(match.group("alt")),
                    "ext": match.group("ext").lower(),
                }
            )
    # Sort by displayed page number, not by resource id.
    return sorted(items, key=lambda x: int(x["page"]))


def assemble_pdf(image_paths: list[Path], pdf_path: Path) -> bool:
    if not image_paths:
        return False
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["magick", *map(str, image_paths), str(pdf_path)]
    subprocess.run(cmd, check=True)
    return pdf_path.exists() and pdf_path.stat().st_size > 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--index-url", default=DEFAULT_INDEX)
    parser.add_argument("--out", default="external/2024_excellent_papers")
    parser.add_argument("--limit", type=int, default=0, help="0 means all")
    parser.add_argument(
        "--questions",
        default="ABC",
        help="Question letters to keep, e.g. ABC. Defaults to excluding D/E.",
    )
    parser.add_argument("--skip-images", action="store_true")
    parser.add_argument("--skip-pdf", action="store_true")
    parser.add_argument("--force-pdf", action="store_true")
    parser.add_argument("--sleep", type=float, default=0.2)
    args = parser.parse_args(argv)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    index_path = out / "index.html"
    if not index_path.exists():
        print(f"fetch index: {args.index_url}", flush=True)
        fetch(args.index_url, index_path)
    index_html = read_text(index_path)
    keep_questions = {q.upper() for q in args.questions if q.strip()}
    article_urls = [
        url for url in extract_articles(index_html) if extract_question(url) in keep_questions
    ]
    if args.limit:
        article_urls = article_urls[: args.limit]
    print(f"articles={len(article_urls)}", flush=True)

    records: list[dict[str, object]] = []
    for idx, url in enumerate(article_urls, start=1):
        question = extract_question(url)
        content_id = re.search(r"/(\d+)\.shtml$", url)
        stub = content_id.group(1) if content_id else str(idx)
        tmp_page = out / "_pages" / f"{stub}.html"
        if not tmp_page.exists():
            print(f"[{idx}/{len(article_urls)}] fetch page {url}", flush=True)
            fetch(url, tmp_page)
            time.sleep(args.sleep)
        page_html = read_text(tmp_page)
        title = extract_title(page_html)
        code = extract_code(title, url)
        paper_dir = out / f"{question}_{code}"
        paper_dir.mkdir(parents=True, exist_ok=True)
        page_path = paper_dir / "page.html"
        if not page_path.exists():
            page_path.write_text(page_html, encoding="utf-8")
        images = extract_page_images(page_html, code)
        image_paths: list[Path] = []
        if not args.skip_images:
            img_dir = paper_dir / "images"
            img_dir.mkdir(exist_ok=True)
            for img in images:
                img_path = img_dir / f"page_{img['page']}.{img.get('ext', 'jpg')}"
                image_paths.append(img_path)
                if img_path.exists() and img_path.stat().st_size > 0:
                    continue
                print(f"  image {code} page {img['page']}", flush=True)
                fetch(str(img["url"]), img_path)
                time.sleep(args.sleep)
        else:
            image_paths = [
                paper_dir / "images" / f"page_{img['page']}.{img.get('ext', 'jpg')}"
                for img in images
            ]

        pdf_path = paper_dir / f"{code}.pdf"
        if not args.skip_pdf and image_paths and (args.force_pdf or not pdf_path.exists()):
            print(f"  assemble {pdf_path}", flush=True)
            assemble_pdf(image_paths, pdf_path)

        record = {
            "question": question,
            "code": code,
            "title": title,
            "url": url,
            "pages": len(images),
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
    print(f"saved manifest: {out / 'manifest.json'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
