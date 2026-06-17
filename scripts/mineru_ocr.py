#!/usr/bin/env python3
"""Upload a PDF to MinerU Precise API, wait for OCR, save full.md.
Usage: python3 mineru_ocr.py <pdf_path> <out_dir>
"""
import sys, time, json, zipfile, io, pathlib, subprocess, urllib.request

import os
API_KEY = os.environ.get("MINERU_TOKEN", "")
if not API_KEY:
    sys.exit("ERROR: MINERU_TOKEN not set. Copy .env.example to .env and fill in your token.\n"
             "Get one at: https://mineru.net/apiManage/token")
BASE = "https://mineru.net/api/v4"

def api(method, path, body=None):
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())

pdf_path = pathlib.Path(sys.argv[1])
out_dir  = pathlib.Path(sys.argv[2])
out_dir.mkdir(parents=True, exist_ok=True)

print("Step 1: get signed URL…")
r = api("POST", "/file-urls/batch",
        {"files": [{"name": pdf_path.name, "is_ocr": True, "data_id": "ocr1"}]})
batch_id = r["data"]["batch_id"]
put_url  = r["data"]["file_urls"][0]
print(f"  batch_id={batch_id}")

print("Step 2: upload via curl --upload-file…")
rc = subprocess.call(
    ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}\\n",
     "-X", "PUT", put_url, "--upload-file", str(pdf_path)],
    timeout=300)
print(f"  curl exit={rc}")

print("Step 3: submit parse…")
r = api("POST", "/extract/task/batch",
        {"batch_id": batch_id, "model_version": "vlm",
         "is_ocr": True, "enable_formula": True, "enable_table": True, "language": "ch"})
print(f"  code={r.get('code')} msg={r.get('msg')}")

print("Step 4: polling…")
for i in range(1, 61):
    time.sleep(10)
    r = api("GET", f"/extract-results/batch/{batch_id}")
    item = r["data"]["extract_result"][0]
    state = item["state"]
    print(f"  [{i*10}s] {state}")
    if state == "done":
        zip_url = item["full_zip_url"]
        with urllib.request.urlopen(zip_url, timeout=60) as resp:
            zdata = resp.read()
        with zipfile.ZipFile(io.BytesIO(zdata)) as zf:
            md_names = [n for n in zf.namelist() if n.endswith("full.md")] or \
                       [n for n in zf.namelist() if n.endswith(".md")]
            content = zf.read(md_names[0]) if md_names else b""
            if content:
                (out_dir / "full.md").write_bytes(content)
                print(f"  saved {out_dir}/full.md ({len(content)} bytes)")
            else:
                zf.extractall(out_dir)
                print(f"  extracted all to {out_dir}/")
        sys.exit(0)
    if state == "failed":
        print(f"FAILED: {item.get('err_msg')}"); sys.exit(1)

print("TIMEOUT"); sys.exit(2)
