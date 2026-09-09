#!/usr/bin/env python3
"""Upload a PDF to MinerU Precise API, wait for OCR, save full.md.
Usage: MINERU_TOKEN=... python3 mineru_ocr.py <pdf_path> <out_dir>
"""
import io
import json
import os
import pathlib
import subprocess
import sys
import time
import urllib.request
import zipfile

try:
    API_KEY = os.environ.pop("MINERU_TOKEN").strip()
except KeyError:
    raise SystemExit("MINERU_TOKEN is required")
if not API_KEY:
    raise SystemExit("MINERU_TOKEN is required")

BASE = "https://mineru.net/api/v4"


def api(method, path, api_key, body=None):
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as response:
        return json.loads(response.read())


pdf_path = pathlib.Path(sys.argv[1])
out_dir = pathlib.Path(sys.argv[2])
out_dir.mkdir(parents=True, exist_ok=True)

print("Step 1: get signed URL…")
result = api(
    "POST",
    "/file-urls/batch",
    API_KEY,
    {"files": [{"name": pdf_path.name, "is_ocr": True, "data_id": "ocr1"}]},
)
batch_id = result["data"]["batch_id"]
put_url = result["data"]["file_urls"][0]
print(f"  batch_id={batch_id}")

print("Step 2: upload via curl --upload-file…")
return_code = subprocess.call(
    [
        "curl",
        "-s",
        "-o",
        "/dev/null",
        "-w",
        "%{http_code}\\n",
        "-X",
        "PUT",
        put_url,
        "--upload-file",
        str(pdf_path),
    ],
    timeout=300,
)
print(f"  curl exit={return_code}")

print("Step 3: submit parse…")
result = api(
    "POST",
    "/extract/task/batch",
    API_KEY,
    {
        "batch_id": batch_id,
        "model_version": "vlm",
        "is_ocr": True,
        "enable_formula": True,
        "enable_table": True,
        "language": "ch",
    },
)
print(f"  code={result.get('code')} msg={result.get('msg')}")

print("Step 4: polling…")
for attempt in range(1, 61):
    time.sleep(10)
    result = api("GET", f"/extract-results/batch/{batch_id}", API_KEY)
    item = result["data"]["extract_result"][0]
    state = item["state"]
    print(f"  [{attempt * 10}s] {state}")
    if state == "done":
        zip_url = item["full_zip_url"]
        with urllib.request.urlopen(zip_url, timeout=60) as response:
            zip_data = response.read()
        with zipfile.ZipFile(io.BytesIO(zip_data)) as archive:
            markdown_names = [
                name for name in archive.namelist() if name.endswith("full.md")
            ] or [name for name in archive.namelist() if name.endswith(".md")]
            content = archive.read(markdown_names[0]) if markdown_names else b""
            if content:
                (out_dir / "full.md").write_bytes(content)
                print(f"  saved {out_dir}/full.md ({len(content)} bytes)")
            else:
                archive.extractall(out_dir)
                print(f"  extracted all to {out_dir}/")
        sys.exit(0)
    if state == "failed":
        print(f"FAILED: {item.get('err_msg')}")
        sys.exit(1)

print("TIMEOUT")
sys.exit(2)
