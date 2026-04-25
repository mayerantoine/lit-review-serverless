#!/usr/bin/env bash
# cleanup.sh — Delete all AWS resources for lit-review-serverless
#
# Usage:
#   bash cleanup.sh              # dry-run: shows what would be deleted
#   bash cleanup.sh --confirm    # actually deletes everything
#
# Resources deleted:
#   - CloudFormation stack (Lambdas, API Gateway, DynamoDB, Lambda Layer)
#   - S3 buckets: lit-review-upload, lit-review-papers, lit-review-frontend-ACCOUNT_ID
#   - S3 Vectors bucket: lit-llm-s3-vectors-ACCOUNT_ID
#   - AgentCore runtimes: lit_review_retrieve_rank, lit_review_generate
#   - ECR repositories: lit-review-retrieve-rank, lit-review-generate, lit-review
#   - CloudFront distribution (disabled then deleted)
#   - Secrets Manager secret: lit-review/openai
#   - IAM role: AgentCoreExecutionRole (and its inline policies)
#
# Resources NOT deleted (shared/service-linked roles created by AWS):
#   - AmazonBedrockAgentCoreSDK* roles
#   - AWSServiceRoleForBedrockAgentCoreRuntimeIdentity

set -euo pipefail

REGION="us-east-2"
STACK_NAME="lit-review-serverless"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

DRY_RUN=true
if [[ "${1:-}" == "--confirm" ]]; then
  DRY_RUN=false
fi

run() {
  # Print the command; execute only if not dry-run
  echo "  + $*"
  if [[ "$DRY_RUN" == "false" ]]; then
    "$@"
  fi
}

echo "========================================"
echo " lit-review-serverless cleanup"
echo " Account: $ACCOUNT_ID  Region: $REGION"
if [[ "$DRY_RUN" == "true" ]]; then
  echo " Mode: DRY RUN (pass --confirm to execute)"
fi
echo "========================================"

# ── 1. CloudFormation stack ──────────────────────────────────────────────────
echo
echo "[ 1/8 ] CloudFormation stack: $STACK_NAME"
run aws cloudformation delete-stack \
  --stack-name "$STACK_NAME" \
  --region "$REGION"
if [[ "$DRY_RUN" == "false" ]]; then
  echo "  Waiting for stack deletion..."
  aws cloudformation wait stack-delete-complete \
    --stack-name "$STACK_NAME" \
    --region "$REGION"
  echo "  Stack deleted."
fi

# ── 2. S3 buckets ────────────────────────────────────────────────────────────
echo
echo "[ 2/8 ] S3 buckets"
for BUCKET in \
  "lit-review-upload" \
  "lit-review-papers" \
  "lit-llm-s3-vectors-${ACCOUNT_ID}" \
  "lit-review-frontend-${ACCOUNT_ID}"; do
  if aws s3api head-bucket --bucket "$BUCKET" 2>/dev/null; then
    echo "  Emptying and deleting s3://$BUCKET"
    run aws s3 rm "s3://$BUCKET" --recursive
    run aws s3api delete-bucket --bucket "$BUCKET" --region "$REGION"
  else
    echo "  s3://$BUCKET — not found, skipping"
  fi
done

# ── 3. AgentCore runtimes ────────────────────────────────────────────────────
echo
echo "[ 3/8 ] AgentCore runtimes"
for AGENT_NAME in "lit_review_retrieve_rank" "lit_review_generate"; do
  AGENT_ARN=$(aws bedrock-agentcore list-agent-runtimes \
    --region "$REGION" \
    --query "agentRuntimes[?agentRuntimeName=='${AGENT_NAME}'].agentRuntimeArn" \
    --output text 2>/dev/null || true)
  if [[ -n "$AGENT_ARN" ]]; then
    echo "  Deleting AgentCore runtime: $AGENT_NAME"
    run aws bedrock-agentcore delete-agent-runtime \
      --agent-runtime-id "$AGENT_ARN" \
      --region "$REGION"
  else
    echo "  AgentCore runtime $AGENT_NAME — not found, skipping"
  fi
done

# ── 4. ECR repositories ──────────────────────────────────────────────────────
echo
echo "[ 4/8 ] ECR repositories"
for REPO in "lit-review-retrieve-rank" "lit-review-generate" "lit-review"; do
  if aws ecr describe-repositories --repository-names "$REPO" --region "$REGION" &>/dev/null; then
    echo "  Deleting ECR repo: $REPO"
    run aws ecr delete-repository \
      --repository-name "$REPO" \
      --force \
      --region "$REGION"
  else
    echo "  ECR repo $REPO — not found, skipping"
  fi
done

# ── 5. CloudFront distribution ───────────────────────────────────────────────
echo
echo "[ 5/8 ] CloudFront distribution"
CF_ID=$(aws cloudfront list-distributions \
  --query "DistributionList.Items[?contains(Origins.Items[0].DomainName, 'lit-review-frontend')].Id" \
  --output text 2>/dev/null || true)
if [[ -n "$CF_ID" ]]; then
  echo "  Found distribution: $CF_ID"
  if [[ "$DRY_RUN" == "false" ]]; then
    # Must disable before deleting
    ETAG=$(aws cloudfront get-distribution-config --id "$CF_ID" --query 'ETag' --output text)
    aws cloudfront get-distribution-config --id "$CF_ID" \
      --query 'DistributionConfig' --output json \
      | python3 -c "import sys,json; d=json.load(sys.stdin); d['Enabled']=False; print(json.dumps(d))" \
      > /tmp/cf-disabled.json
    aws cloudfront update-distribution --id "$CF_ID" \
      --distribution-config file:///tmp/cf-disabled.json \
      --if-match "$ETAG" > /dev/null
    echo "  Waiting for distribution to be disabled (~5 min)..."
    aws cloudfront wait distribution-deployed --id "$CF_ID"
    ETAG=$(aws cloudfront get-distribution --id "$CF_ID" --query 'ETag' --output text)
    aws cloudfront delete-distribution --id "$CF_ID" --if-match "$ETAG"
    echo "  CloudFront distribution deleted."
  else
    echo "  + disable and delete CloudFront distribution $CF_ID"
  fi
else
  echo "  No matching CloudFront distribution found, skipping"
fi

# ── 6. Secrets Manager ───────────────────────────────────────────────────────
echo
echo "[ 6/8 ] Secrets Manager"
SECRET_ARN=$(aws secretsmanager describe-secret \
  --secret-id "lit-review/openai" \
  --region "$REGION" \
  --query 'ARN' --output text 2>/dev/null || true)
if [[ -n "$SECRET_ARN" ]]; then
  echo "  Deleting secret: lit-review/openai"
  run aws secretsmanager delete-secret \
    --secret-id "lit-review/openai" \
    --force-delete-without-recovery \
    --region "$REGION"
else
  echo "  Secret lit-review/openai — not found, skipping"
fi

# ── 7. IAM role: AgentCoreExecutionRole ──────────────────────────────────────
echo
echo "[ 7/8 ] IAM role: AgentCoreExecutionRole"
if aws iam get-role --role-name AgentCoreExecutionRole &>/dev/null; then
  if [[ "$DRY_RUN" == "false" ]]; then
    # Detach managed policies
    MANAGED=$(aws iam list-attached-role-policies \
      --role-name AgentCoreExecutionRole \
      --query 'AttachedPolicies[].PolicyArn' --output text)
    for ARN in $MANAGED; do
      echo "  Detaching policy $ARN"
      aws iam detach-role-policy --role-name AgentCoreExecutionRole --policy-arn "$ARN"
    done
    # Delete inline policies
    INLINE=$(aws iam list-role-policies \
      --role-name AgentCoreExecutionRole \
      --query 'PolicyNames[]' --output text)
    for P in $INLINE; do
      echo "  Deleting inline policy $P"
      aws iam delete-role-policy --role-name AgentCoreExecutionRole --policy-name "$P"
    done
    aws iam delete-role --role-name AgentCoreExecutionRole
    echo "  IAM role deleted."
  else
    echo "  + detach policies and delete IAM role AgentCoreExecutionRole"
  fi
else
  echo "  IAM role AgentCoreExecutionRole — not found, skipping"
fi

# ── 8. SAM CLI managed S3 bucket (optional) ──────────────────────────────────
echo
echo "[ 8/8 ] SAM CLI managed bucket (optional)"
SAM_BUCKET=$(aws s3api list-buckets \
  --query "Buckets[?starts_with(Name,'aws-sam-cli-managed-default')].Name" \
  --output text 2>/dev/null || true)
if [[ -n "$SAM_BUCKET" ]]; then
  echo "  SAM bucket found: $SAM_BUCKET"
  echo "  Skipping — shared across SAM stacks. Delete manually if desired:"
  echo "    aws s3 rm s3://$SAM_BUCKET --recursive"
  echo "    aws s3api delete-bucket --bucket $SAM_BUCKET --region $REGION"
fi

echo
echo "========================================"
if [[ "$DRY_RUN" == "true" ]]; then
  echo " Dry run complete. Run with --confirm to delete."
else
  echo " Cleanup complete."
fi
echo "========================================"
