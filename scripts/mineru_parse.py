#!/usr/bin/env python3
"""
mineru_parse.py — convert a PDF (or directory of PDFs) to Markdown via the
OpenXLab MinerU hosted API (https://mineru.net/doc/docs/index_en/).

Stdlib only — no pip install required (works under Debian's PEP 668).

Usage:
    ./scripts/mineru_parse.py path/to/file.pdf
    ./scripts/mineru_parse.py path/to/dir         # walks for *.pdf, parses any missing
    ./scripts/mineru_parse.py file.pdf --out other.md
    ./scripts/mineru_parse.py file.pdf --force    # re-parse even if .md exists
    ./scripts/mineru_parse.py file.pdf --lang en
    ./scripts/mineru_parse.py file.pdf --ocr      # force OCR even on text PDFs

Env:
    MINERU_TOKEN  — required, OpenXLab API token (https://mineru.net/apiManage/token)
                    falls back to reading ./.env if unset

Output (next to the source PDF unless --out is given):
    <stem>.md                  full Markdown
    <stem>.mineru/             unzipped layout.json + middle.json + content_list.json
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

API_ROOT = "https://mineru.net/api/v4"
POLL_INTERVAL = 8.0       # seconds between status checks
POLL_TIMEOUT = 20 * 60    # 20 min hard cap
UPLOAD_TIMEOUT = 5 * 60


def load_token() -> str:
    tok = os.environ.get("MINERU_TOKEN", "").strip()
    if tok:
        return tok
    env_file = Path(__file__).resolve().parent.parent / ".env"
    if env_file.is_file():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line.startswith("MINERU_TOKEN="):
                return line.split("=", 1)[1].strip()
    sys.exit("MINERU_TOKEN not set (env var or .env)")


def http_json(method: str, url: str, token: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        msg = e.read().decode("utf-8", errors="replace")
        sys.exit(f"{method} {url} failed: HTTP {e.code}: {msg[:400]}")
    if payload.get("code") != 0:
        sys.exit(f"{method} {url} returned error: {payload}")
    return payload["data"]


def upload_pdf(signed_url: str, pdf_path: Path) -> None:
    # OSS presigned URLs are sensitive to which headers the client adds — urllib
    # injects User-Agent/Accept-Encoding that break the signature in some cases.
    # curl --upload-file sends only Host + Content-Length, which is what MinerU
    # signed against.  -f makes curl exit non-zero on HTTP >= 400.
    cmd = [
        "curl", "--silent", "--show-error", "--fail",
        "--max-time", str(UPLOAD_TIMEOUT),
        "--upload-file", str(pdf_path),
        signed_url,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        sys.exit(f"PUT upload failed (curl exit {proc.returncode}): "
                 f"{proc.stderr.strip() or proc.stdout.strip()}")


def download_zip(url: str, dest: Path) -> None:
    with urllib.request.urlopen(url, timeout=120) as resp, dest.open("wb") as out:
        shutil.copyfileobj(resp, out)


def parse_one(pdf: Path, out_md: Path, token: str, language: str, force_ocr: bool) -> None:
    print(f"[mineru] submit {pdf.name} ({pdf.stat().st_size/1024:.0f}KB)")
    batch = http_json(
        "POST", f"{API_ROOT}/file-urls/batch", token,
        body={
            "files": [{"name": pdf.name, "is_ocr": force_ocr}],
            "enable_formula": True,
            "enable_table": True,
            "language": language,
            "model_version": "vlm",
        },
    )
    batch_id = batch["batch_id"]
    signed_url = batch["file_urls"][0]
    print(f"[mineru] batch_id={batch_id} — uploading…")
    upload_pdf(signed_url, pdf)

    print("[mineru] polling status…")
    deadline = time.time() + POLL_TIMEOUT
    last_msg = ""
    while time.time() < deadline:
        status = http_json("GET", f"{API_ROOT}/extract-results/batch/{batch_id}", token)
        result = status["extract_result"][0]
        state = result.get("state", "?")
        if state == "done":
            zip_url = result["full_zip_url"]
            break
        if state == "failed":
            sys.exit(f"[mineru] extract failed: {result.get('err_msg', 'no message')}")
        prog = result.get("extract_progress") or {}
        msg = f"state={state}"
        if prog:
            msg += f" page {prog.get('extracted_pages', 0)}/{prog.get('total_pages', '?')}"
        if msg != last_msg:
            print(f"[mineru] {msg}")
            last_msg = msg
        time.sleep(POLL_INTERVAL)
    else:
        sys.exit("[mineru] timed out waiting for extract")

    with tempfile.TemporaryDirectory() as tmp:
        zip_path = Path(tmp) / "result.zip"
        download_zip(zip_url, zip_path)
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(tmp)
            members = zf.namelist()

        full_md = next(
            (Path(tmp) / m for m in members if m.endswith("full.md") or m == "full.md"),
            None,
        )
        if full_md is None or not full_md.is_file():
            sys.exit(f"[mineru] zip missing full.md (members: {members[:5]}…)")

        out_md.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(full_md, out_md)

        # Preserve sidecar JSONs alongside, useful for figures/tables/citation extraction.
        sidecar_dir = out_md.with_suffix(".mineru")
        if sidecar_dir.exists():
            shutil.rmtree(sidecar_dir)
        sidecar_dir.mkdir()
        for m in members:
            mp = Path(tmp) / m
            if not mp.is_file():
                continue
            if mp.name in ("layout.json", "middle.json", "content_list.json", "model.json") \
                    or mp.name.endswith(("_layout.json", "_middle.json",
                                          "_content_list.json", "_model.json")):
                shutil.copy(mp, sidecar_dir / mp.name)
            elif mp.suffix.lower() in (".png", ".jpg", ".jpeg"):
                img_dir = sidecar_dir / "images"
                img_dir.mkdir(exist_ok=True)
                shutil.copy(mp, img_dir / mp.name)

    md_size = out_md.stat().st_size
    print(f"[mineru] wrote {out_md}  ({md_size/1024:.1f}KB markdown)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("path", help="PDF file or directory containing PDFs")
    ap.add_argument("--out", help="Output markdown path (single PDF only)")
    ap.add_argument("--lang", default="ch", help="Document language hint (default: ch)")
    ap.add_argument("--ocr", action="store_true", help="Force OCR (default: auto)")
    ap.add_argument("--force", action="store_true", help="Re-parse even if .md exists")
    args = ap.parse_args()

    token = load_token()
    root = Path(args.path).resolve()

    if root.is_file():
        if root.suffix.lower() != ".pdf":
            sys.exit(f"not a PDF: {root}")
        out = Path(args.out).resolve() if args.out else root.with_suffix(".md")
        if out.exists() and not args.force:
            print(f"[mineru] skip {root.name} — {out.name} exists (--force to redo)")
            return
        parse_one(root, out, token, args.lang, args.ocr)
        return

    if not root.is_dir():
        sys.exit(f"path not found: {root}")
    if args.out:
        sys.exit("--out only makes sense for a single PDF input")

    pdfs = sorted(root.rglob("*.pdf"))
    if not pdfs:
        sys.exit(f"no PDFs under {root}")
    print(f"[mineru] {len(pdfs)} PDF(s) under {root}")
    for pdf in pdfs:
        out = pdf.with_suffix(".md")
        if out.exists() and not args.force:
            print(f"[mineru] skip {pdf.relative_to(root)} — .md exists")
            continue
        try:
            parse_one(pdf, out, token, args.lang, args.ocr)
        except SystemExit as e:
            print(f"[mineru] FAILED {pdf}: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
