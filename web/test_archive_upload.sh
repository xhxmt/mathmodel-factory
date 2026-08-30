#!/bin/bash
set -euo pipefail

# Test script for archive upload feature
# Requires: backend running on localhost:8000 and ARCHIVE_UPLOAD_TOKEN in env

BACKEND_URL="http://localhost:8000"
TEST_ARCHIVE="/tmp/test_problem.zip"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${YELLOW}=== Archive Upload Feature Test ===${NC}\n"

# Check if test archive exists
if [[ ! -f "$TEST_ARCHIVE" ]]; then
    echo -e "${RED}✗ Test archive not found: $TEST_ARCHIVE${NC}"
    exit 1
fi

echo -e "${GREEN}✓ Test archive found${NC}"
echo "Archive: $TEST_ARCHIVE"
echo ""

# Accept only a caller-provided runtime token; never derive or persist it here.
echo -e "${YELLOW}Step 1: Validate runtime authorization input${NC}"
: "${ARCHIVE_UPLOAD_TOKEN:?ARCHIVE_UPLOAD_TOKEN is required}"

echo -e "${GREEN}✓ Runtime authorization input is present${NC}"
echo ""

# Upload archive
echo -e "${YELLOW}Step 2: Upload archive${NC}"
UPLOAD_RESPONSE=$(curl -s -X POST "$BACKEND_URL/api/upload/problem" \
    -H "Authorization: Bearer ${ARCHIVE_UPLOAD_TOKEN}" \
    -F "file=@$TEST_ARCHIVE")

echo "$UPLOAD_RESPONSE" | jq '.'

STATUS=$(echo "$UPLOAD_RESPONSE" | jq -r '.status // empty')
if [[ "$STATUS" == "ok" ]]; then
    echo -e "${GREEN}✓ Upload successful${NC}"

    FILE_PATH=$(echo "$UPLOAD_RESPONSE" | jq -r '.file_path')
    FILENAME=$(echo "$UPLOAD_RESPONSE" | jq -r '.filename')
    EXTRACTED_DIR=$(echo "$UPLOAD_RESPONSE" | jq -r '.extracted_dir // empty')

    echo ""
    echo "Extracted problem file: $FILE_PATH"
    echo "Filename: $FILENAME"
    if [[ -n "$EXTRACTED_DIR" ]]; then
        echo "Extracted directory: $EXTRACTED_DIR"
        echo ""
        echo -e "${YELLOW}Extracted files:${NC}"
        ls -lh "$EXTRACTED_DIR"
    fi
else
    echo -e "${RED}✗ Upload failed${NC}"
    exit 1
fi

echo ""
echo -e "${GREEN}=== All tests passed ===${NC}"
