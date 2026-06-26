#!/usr/bin/env bash
# Quick setup script - completes steps 4-5 from the guide
set -euo pipefail

PROJECT_ID="level-night-476302-k0"
REGION="europe-west4"

echo "=========================================="
echo "Paper Factory Cloud Solver - Quick Setup"
echo "=========================================="
echo "Project: $PROJECT_ID"
echo "Region: $REGION"
echo ""

# Step 4: Create service account
echo "[Step 4/5] Creating service account..."
SA_EMAIL="solver-runner@${PROJECT_ID}.iam.gserviceaccount.com"

if gcloud iam service-accounts describe "$SA_EMAIL" &>/dev/null; then
    echo "✓ Service account already exists"
else
    echo "Creating service account..."
    gcloud iam service-accounts create solver-runner \
        --display-name="Paper Factory Solver Runner"

    echo "Granting Storage permissions..."
    gcloud projects add-iam-policy-binding "$PROJECT_ID" \
        --member="serviceAccount:${SA_EMAIL}" \
        --role="roles/storage.objectAdmin"

    echo "✓ Service account created and configured"
fi

echo ""
echo "[Step 5/5] Ready to deploy to Cloud Run"
echo ""
echo "Run the following command to build and deploy:"
echo "  ./deploy_cloud_solver.sh"
echo ""
echo "Estimated time: 10-15 minutes"
echo "Estimated cost per deployment: ~€0.02 (Cloud Build)"
echo ""
