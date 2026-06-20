#!/usr/bin/env bash
# Upload a local PDF to MinerU Precise API, wait for OCR, save full.md
# Usage: ./scripts/mineru_ocr.sh <pdf_path> <out_dir>
set -euo pipefail

PDF="$1"; OUT_DIR="$2"
API_KEY="eyJ0eXBlIjoiSldUIiwiYWxnIjoiSFM1MTIifQ.eyJqdGkiOiIxNDUwMDMzNyIsInJvbCI6IlJPTEVfUkVHSVNURVIiLCJpc3MiOiJPcGVuWExhYiIsImlhdCI6MTc3OTI5MDA3NSwiY2xpZW50SWQiOiJsa3pkeDU3bnZ5MjJqa3BxOXgydyIsInBob25lIjoiIiwib3BlbklkIjpudWxsLCJ1dWlkIjoiMGY0MDkzZmMtNzcyNS00OTBiLTgxYWItMGY3YWFlZGQ3OTNkIiwiZW1haWwiOiIiLCJleHAiOjE3ODcwNjYwNzV9.MmAoZFBLkUG_9ed3rPrUt9Ib2v-_QAFbMWdD-lKhcZTiX7h0gjBUXnpMdFKq3b_wSJ5objCyajrQhipc1xAuZw"
AUTH="Authorization: Bearer $API_KEY"
BASE="https://mineru.net/api/v4"
FNAME="$(basename "$PDF")"

echo "=== Step 1: get signed upload URL ==="
BATCH=$(curl -sf -X POST "$BASE/file-urls/batch" \
  -H "$AUTH" -H "Content-Type: application/json" \
  -d "{\"files\":[{\"name\":\"$FNAME\",\"is_ocr\":true,\"data_id\":\"ocr1\"}]}")
echo "$BATCH" | python3 -m json.tool --no-ensure-ascii 2>/dev/null || echo "$BATCH"

PUT_URL=$(echo "$BATCH" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['data']['file_urls'][0])")
BATCH_ID=$(echo "$BATCH" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['data']['batch_id'])")
echo "batch_id=$BATCH_ID"

echo "=== Step 2: upload PDF ==="
curl -sf -X PUT "$PUT_URL" \
  -H "Content-Type: application/octet-stream" \
  --data-binary "@$PDF"
echo " upload done"

echo "=== Step 3: submit parse job ==="
curl -sf -X POST "$BASE/extract/task/batch" \
  -H "$AUTH" -H "Content-Type: application/json" \
  -d "{\"batch_id\":\"$BATCH_ID\",\"model_version\":\"vlm\",\"is_ocr\":true,\"enable_formula\":true,\"enable_table\":true,\"language\":\"ch\"}" | python3 -m json.tool --no-ensure-ascii

echo "=== Step 4: poll until done ==="
for i in $(seq 1 60); do
  sleep 10
  STATE=$(curl -sf -H "$AUTH" "$BASE/extract-results/batch/$BATCH_ID")
  STATUS=$(echo "$STATE" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['data']['list'][0]['state'])" 2>/dev/null || echo "pending")
  echo "  attempt $i: $STATUS"
  if [ "$STATUS" = "done" ]; then
    ZIP_URL=$(echo "$STATE" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['data']['list'][0]['full_zip_url'])")
    echo "  zip_url=$ZIP_URL"
    mkdir -p "$OUT_DIR"
    TMP=$(mktemp /tmp/mineru_XXXX.zip)
    curl -sf "$ZIP_URL" -o "$TMP"
    unzip -qo "$TMP" "*/full.md" -d "$OUT_DIR" 2>/dev/null || unzip -qo "$TMP" -d "$OUT_DIR"
    # flatten: move any nested full.md to OUT_DIR/full.md
    find "$OUT_DIR" -name "full.md" ! -path "$OUT_DIR/full.md" -exec mv {} "$OUT_DIR/full.md" \; 2>/dev/null || true
    rm "$TMP"
    echo "=== Saved to $OUT_DIR/full.md ==="
    wc -l "$OUT_DIR/full.md"
    exit 0
  fi
  if [ "$STATUS" = "failed" ]; then
    echo "FAILED"; echo "$STATE"; exit 1
  fi
done
echo "TIMEOUT after 600s"; exit 2
