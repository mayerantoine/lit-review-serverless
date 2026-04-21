#!/usr/bin/env bash
# Deploy generate AgentCore agent to ECR + AgentCore Runtime
# Run from the backend/ directory: bash agents/generate_agent/deploy.sh

set -euo pipefail

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
REGION=${DEFAULT_AWS_REGION:-us-east-2}
REPO_NAME="lit-review-generate"
IMAGE_TAG="latest"
ECR_URI="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/${REPO_NAME}:${IMAGE_TAG}"
AGENT_NAME="lit-review-generate"
ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/AgentCoreExecutionRole"

echo "==> Building ARM64 Docker image..."
docker buildx build \
  --platform linux/arm64 \
  --file agents/generate_agent/Dockerfile \
  --tag "${ECR_URI}" \
  --load \
  .

echo "==> Authenticating to ECR..."
aws ecr get-login-password --region "${REGION}" \
  | docker login --username AWS --password-stdin \
    "${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"

echo "==> Creating ECR repository (idempotent)..."
aws ecr create-repository \
  --repository-name "${REPO_NAME}" \
  --region "${REGION}" \
  --image-scanning-configuration scanOnPush=true \
  2>/dev/null || echo "Repository already exists, continuing."

echo "==> Pushing image to ECR..."
docker push "${ECR_URI}"

echo "==> Creating AgentCore Runtime agent..."
AGENT_ARN=$(aws bedrock-agentcore create-agent-runtime \
  --agent-runtime-name "${AGENT_NAME}" \
  --agent-runtime-artifact "containerImage={uri=${ECR_URI}}" \
  --execution-role-arn "${ROLE_ARN}" \
  --network-configuration "networkMode=PUBLIC" \
  --query agentRuntimeArn \
  --output text)

echo ""
echo "=========================================="
echo "Deploy complete!"
echo "Agent ARN: ${AGENT_ARN}"
echo ""
echo "Next steps:"
echo "  1. Set AGENTCORE_GENERATE_ARN=${AGENT_ARN} in samconfig.toml"
echo "  2. Run: sam deploy  (to inject ARN into Lambda 3 env vars)"
echo "=========================================="
