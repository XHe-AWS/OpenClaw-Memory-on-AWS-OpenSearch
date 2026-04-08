#!/bin/bash
# deploy.sh — Deploy OpenClaw Memory System v2
# Run on an EC2 instance with appropriate IAM role.
#
# Usage:
#   ./deploy.sh [region] [collection-name]
#   ./deploy.sh us-west-2 openclaw-memory
set -euo pipefail

REGION="${1:-us-west-2}"
COLLECTION_NAME="${2:-openclaw-memory}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "🚀 Deploying OpenClaw Memory System v2"
echo "   Region: $REGION"
echo "   Collection: $COLLECTION_NAME"
echo ""

# Step 1: Deploy CloudFormation
echo "=== Step 1: Deploy CloudFormation stack ==="
aws cloudformation deploy \
  --template-file "$SCRIPT_DIR/cloudformation/memory-system.yaml" \
  --stack-name openclaw-memory \
  --parameter-overrides CollectionName="$COLLECTION_NAME" \
  --capabilities CAPABILITY_NAMED_IAM \
  --region "$REGION" \
  --no-fail-on-empty-changeset

# Get endpoint
ENDPOINT=$(aws cloudformation describe-stacks \
  --stack-name openclaw-memory \
  --query 'Stacks[0].Outputs[?OutputKey==`CollectionEndpoint`].OutputValue' \
  --output text --region "$REGION")

echo "   Endpoint: $ENDPOINT"
echo ""

# Step 2: Python environment
echo "=== Step 2: Setup Python environment ==="
if [ ! -d "$SCRIPT_DIR/.venv" ]; then
  python3 -m venv "$SCRIPT_DIR/.venv"
fi
source "$SCRIPT_DIR/.venv/bin/activate"
pip install -q -r "$SCRIPT_DIR/requirements.txt"

# Step 3: Set environment
echo "=== Step 3: Configure ==="
export OPENCLAW_MEMORY_OPENSEARCH_ENDPOINT="$ENDPOINT"
export OPENCLAW_MEMORY_OPENSEARCH_REGION="$REGION"
export OPENCLAW_MEMORY_COLLECTION_NAME="$COLLECTION_NAME"

# Wait for collection to be ACTIVE
echo "   Waiting for collection to become ACTIVE..."
for i in $(seq 1 60); do
  STATUS=$(aws opensearchserverless batch-get-collection \
    --names "$COLLECTION_NAME" \
    --query 'collectionDetails[0].status' \
    --output text --region "$REGION" 2>/dev/null || echo "UNKNOWN")
  if [ "$STATUS" = "ACTIVE" ]; then
    echo "   Collection is ACTIVE!"
    break
  fi
  echo "   Status: $STATUS (attempt $i/60)"
  sleep 10
done

# Step 3.5: Ensure current caller has AOSS data access
echo ""
echo "=== Step 3.5: Grant AOSS data access to current caller ==="
CALLER_ARN=$(aws sts get-caller-identity --query 'Arn' --output text --region "$REGION")
# Normalize: if it's an assumed-role ARN, extract the role ARN
if echo "$CALLER_ARN" | grep -q 'assumed-role'; then
  ROLE_NAME=$(echo "$CALLER_ARN" | sed 's|.*assumed-role/||' | cut -d/ -f1)
  ACCOUNT_ID=$(echo "$CALLER_ARN" | cut -d: -f5)
  CALLER_ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${ROLE_NAME}"
else
  CALLER_ROLE_ARN="$CALLER_ARN"
fi

# Get the CFN-created role ARN
CFN_ROLE_ARN=$(aws cloudformation describe-stacks \
  --stack-name openclaw-memory \
  --query 'Stacks[0].Outputs[?OutputKey==`RoleArn`].OutputValue' \
  --output text --region "$REGION" 2>/dev/null || echo "")

# Build principal list (deduplicated)
if [ "$CALLER_ROLE_ARN" = "$CFN_ROLE_ARN" ] || [ -z "$CALLER_ROLE_ARN" ]; then
  echo "   Current caller is the CFN role, no extra access needed."
else
  echo "   Current caller: $CALLER_ROLE_ARN"
  echo "   Updating AOSS data access policy to include current caller..."
  POLICY_DOC=$(cat << POLICYEOF
[{
  "Rules": [
    {
      "ResourceType": "index",
      "Resource": ["index/${COLLECTION_NAME}/*"],
      "Permission": [
        "aoss:CreateIndex", "aoss:UpdateIndex", "aoss:DescribeIndex",
        "aoss:ReadDocument", "aoss:WriteDocument"
      ]
    },
    {
      "ResourceType": "collection",
      "Resource": ["collection/${COLLECTION_NAME}"],
      "Permission": ["aoss:CreateCollectionItems", "aoss:DescribeCollectionItems"]
    }
  ],
  "Principal": ["${CFN_ROLE_ARN}", "${CALLER_ROLE_ARN}"]
}]
POLICYEOF
  )
  aws opensearchserverless update-access-policy \
    --name "${COLLECTION_NAME}-access" \
    --type data \
    --policy-version "$(aws opensearchserverless get-access-policy \
      --name "${COLLECTION_NAME}-access" \
      --type data \
      --query 'accessPolicyDetail.policyVersion' \
      --output text --region "$REGION")" \
    --policy "$POLICY_DOC" \
    --region "$REGION" > /dev/null 2>&1
  echo "   Data access policy updated. Waiting 15s for propagation..."
  sleep 15
fi

# Step 4: Create index + search pipeline
echo ""
echo "=== Step 4: Create OpenSearch index + search pipeline ==="
python "$SCRIPT_DIR/setup_opensearch.py"

# Step 5: Output config
echo ""
echo "=== Step 5: Configuration ==="
MCP_PATH="$SCRIPT_DIR"
PYTHON_PATH="$SCRIPT_DIR/.venv/bin/python"

cat << EOF

✅ Deployment complete!

Add to openclaw.json:

{
  "mcp": {
    "servers": {
      "openclaw-memory": {
        "command": "$PYTHON_PATH",
        "args": ["$MCP_PATH/mcp_server.py"],
        "env": {
          "OPENCLAW_MEMORY_OPENSEARCH_ENDPOINT": "$ENDPOINT",
          "OPENCLAW_MEMORY_OPENSEARCH_REGION": "$REGION"
        }
      }
    }
  }
}

Then: openclaw gateway restart

Optional — add dreaming cron job:
  openclaw cron add --name memory-dreaming \\
    --schedule "0 3 * * *" \\
    --command "$PYTHON_PATH -m dreaming.runner --agent xiaoxiami"

Test with: "记住我喜欢吃火锅"
EOF
