# Deployment Guide — Literature Review Serverless

This guide walks through deploying every component of the architecture to AWS,
in the correct order. Each section calls out whether the step is a CLI command,
a manual AWS Console action, or a code change.

---

## Architecture Overview

```
Browser
  ├─ POST /api/upload-and-index  → API Gateway → Lambda 1 (upload + index)
  ├─ POST /api/retrieve-and-rank → API Gateway → Lambda 2 → AgentCore RetrieveRank Agent
  └─ POST /api/generate          → Lambda 3 Function URL (streaming) → AgentCore Generate Agent

All Lambdas + Agents ↔ DynamoDB (LitReviewSessions)
Lambda 1 + Agents    ↔ S3 (CSV, papers, vectors, ranked JSON)
Agents               ↔ OpenAI API (outbound via AgentCore PUBLIC network mode)
```

## Deployment Order

```
Step 1  Prerequisites (local tools)
Step 2  AWS account prep (S3 buckets + Secrets Manager)  ← Manual Console
Step 3  SAM deploy — DynamoDB + Lambda functions + API Gateway
Step 4  IAM — AgentCore execution role                   ← Manual Console
Step 5  ECR — push agent Docker images
Step 6  AgentCore — create Runtime agents
Step 7  SAM redeploy — inject AgentCore ARNs
Step 8  Frontend — build and host on S3/CloudFront       ← Manual Console
Step 9  Smoke tests
```

---

## Step 1 — Prerequisites (local tools)

Install the following before starting:

```bash
# AWS CLI v2
brew install awscli
aws configure          # set Access Key, Secret, region=us-east-2, output=json

# AWS SAM CLI
brew install aws-sam-cli
sam --version          # must be >= 1.120

# Docker Desktop (needed for SAM build and agent images)
# Download from https://www.docker.com/products/docker-desktop/
docker --version

# Python 3.12
python3 --version      # must be 3.12.x

# Node.js 20+ (for frontend build)
node --version
```

Verify your AWS identity:
```bash
aws sts get-caller-identity
# Note your Account ID — used throughout this guide as ACCOUNT_ID
```

---

## Step 2 — AWS Account Prep (Manual Console + CLI)

### 2a. S3 Buckets

Create three S3 buckets in `us-east-2`. All must be private (block public access).

| Bucket name | Purpose |
|---|---|
| `lit-review-upload` | Temporary CSV uploads (Lambda 1) |
| `lit-review-papers` | Paper JSON documents + ranked results |
| `lit-llm-s3-vectors-ACCOUNT_ID` | S3 Vectors embeddings index |

**Console:** S3 → Create bucket → us-east-2 → Block all public access ✓

Or via CLI:
```bash
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

aws s3 mb s3://lit-review-upload          --region us-east-2
aws s3 mb s3://lit-review-papers          --region us-east-2
aws s3 mb s3://lit-llm-s3-vectors-${ACCOUNT_ID} --region us-east-2
```

### 2b. OpenAI API Key in Secrets Manager

**Console:** AWS Secrets Manager → Store a new secret → Other type of secret

- Key: `api_key`
- Value: `sk-proj-...` (your OpenAI key)
- Secret name: `lit-review/openai`
- Region: `us-east-2`

Or via CLI:
```bash
aws secretsmanager create-secret \
  --name lit-review/openai \
  --region us-east-2 \
  --secret-string '{"api_key":"sk-proj-YOUR_KEY_HERE"}'
```

Note the returned ARN — you will need it in Step 3.
Format: `arn:aws:secretsmanager:us-east-2:ACCOUNT_ID:secret:lit-review/openai-XXXXXX`

### 2c. Enable S3 Vectors

S3 Vectors is a new AWS service. Verify it is available in your account:

```bash
aws s3vectors list-vector-buckets --region us-east-2
# Should return an empty list (not an error)
```

If you get `UnknownServiceError`, update your AWS CLI:
```bash
pip install --upgrade awscli
```

---

## Step 3 — SAM Deploy (DynamoDB + Lambda Functions + API Gateway)

This deploys: DynamoDB table, Lambda Layer (shared code), Lambda 1/2/3,
and the HTTP API Gateway. AgentCore ARNs are not yet known so Lambdas 2/3
will deploy with empty ARNs — that is expected.

### 3a. Edit samconfig.toml

Open `backend/samconfig.toml` and fill in your real values:

```toml
parameter_overrides = [
    "S3VectorBucket=lit-llm-s3-vectors-YOUR_ACCOUNT_ID",
    "S3DataBucket=lit-review-papers",
    "S3CsvBucket=lit-review-upload",
    "OpenAIApiKeyArn=arn:aws:secretsmanager:us-east-2:YOUR_ACCOUNT_ID:secret:lit-review/openai-XXXXXX",
]
```

### 3b. Build and deploy

```bash
cd backend
sam build
sam deploy
```

SAM will show a changeset preview and ask for confirmation — type `y`.

After deploy, note the outputs printed to the terminal:

```
Key                 ApiGatewayUrl
Value               https://XXXXXXXXXX.execute-api.us-east-2.amazonaws.com

Key                 GenerateFunctionUrl
Value               https://XXXXXXXXXXXXXXXXXX.lambda-url.us-east-2.on.aws

Key                 SessionsTableName
Value               LitReviewSessions
```

Save these — you need them for the frontend (Step 8).

API Gateway URL │ https://yybtmvac9a.execute-api.us-east-2.amazonaws.com                
Lambda Function URL│ https://yvpqwr3cipxjlv2wmwlms322jq0bmzqh.lambda-url.us-east-2.on.aws/
DynamoDB table   │ LitReviewSessions                     

### 3c. Verify DynamoDB table

**Console:** DynamoDB → Tables → `LitReviewSessions` → should be ACTIVE
with `session_id` as partition key and TTL enabled on `ttl` attribute.

---

## Step 4 — IAM: AgentCore Execution Role (Manual Console)

AgentCore Runtime needs an IAM role to access DynamoDB, S3, and Secrets Manager
on behalf of your agent containers. SAM does not create this role — do it once manually.

### 4a. Create the role

**Console:** IAM → Roles → Create role

- Trusted entity type: **Custom trust policy**
- Paste the contents of `backend/iam/agentcore-trust-policy.json`:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": { "Service": "bedrock-agentcore.amazonaws.com" },
    "Action": "sts:AssumeRole"
  }]
}
```

- Role name: `AgentCoreExecutionRole`
- Click **Create role**

### 4b. Attach the permission policy

Still in IAM, find the role `AgentCoreExecutionRole` → **Add permissions** → **Create inline policy** → JSON tab.

Paste the contents of `backend/iam/agentcore-role-policy.json`, replacing the
placeholders:

- Replace `REGION` with `us-east-2`
- Replace `ACCOUNT_ID` with your 12-digit account ID

Click **Create policy**.

### 4c. Note the role ARN

IAM → Roles → AgentCoreExecutionRole → copy the ARN.
Format: `arn:aws:iam::ACCOUNT_ID:role/AgentCoreExecutionRole`

---

## Step 5 — ECR: Build and Push Agent Images

Both agents are ARM64 Docker containers. Build them from the `backend/` directory
(the Dockerfiles use paths relative to that root).

### 5a. Create ECR repositories

```bash
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
REGION=us-east-2

aws ecr create-repository --repository-name lit-review-retrieve-rank \
  --region $REGION --image-scanning-configuration scanOnPush=true

aws ecr create-repository --repository-name lit-review-generate \
  --region $REGION --image-scanning-configuration scanOnPush=true
```

### 5b. Authenticate Docker to ECR

```bash
aws ecr get-login-password --region $REGION \
  | docker login --username AWS --password-stdin \
    ${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com
```

### 5c. Build and push retrieve-rank agent

Run from `backend/`:

```bash
cd backend

RETRIEVE_RANK_URI="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/lit-review-retrieve-rank:latest"

docker buildx build \
  --platform linux/arm64 \
  --file agents/retrieve_rank_agent/Dockerfile \
  --tag $RETRIEVE_RANK_URI \
  --load \
  .

docker push $RETRIEVE_RANK_URI
```

### 5d. Build and push generate agent

```bash
GENERATE_URI="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/lit-review-generate:latest"

docker buildx build \
  --platform linux/arm64 \
  --file agents/generate_agent/Dockerfile \
  --tag $GENERATE_URI \
  --load \
  .

docker push $GENERATE_URI
```

> **Tip:** Steps 5b–5d are wrapped in the deploy scripts. You can also run:
> ```bash
> bash agents/retrieve_rank_agent/deploy.sh   # includes Steps 5b-5c + Step 6a
> bash agents/generate_agent/deploy.sh        # includes Steps 5b-5d + Step 6b
> ```

---

## Step 6 — AgentCore: Create Runtime Agents

### 6a. Create the retrieve-rank agent

```bash
ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/AgentCoreExecutionRole"

RETRIEVE_RANK_AGENT_ARN=$(aws bedrock-agentcore create-agent-runtime \
  --agent-runtime-name lit-review-retrieve-rank \
  --agent-runtime-artifact "containerImage={uri=${RETRIEVE_RANK_URI}}" \
  --execution-role-arn $ROLE_ARN \
  --network-configuration "networkMode=PUBLIC" \
  --query agentRuntimeArn \
  --output text)

echo "Retrieve-rank agent ARN: $RETRIEVE_RANK_AGENT_ARN"
```

### 6b. Create the generate agent

```bash
GENERATE_AGENT_ARN=$(aws bedrock-agentcore create-agent-runtime \
  --agent-runtime-name lit-review-generate \
  --agent-runtime-artifact "containerImage={uri=${GENERATE_URI}}" \
  --execution-role-arn $ROLE_ARN \
  --network-configuration "networkMode=PUBLIC" \
  --query agentRuntimeArn \
  --output text)

echo "Generate agent ARN: $GENERATE_AGENT_ARN"
```

### 6c. Verify agents are running

**Console:** Amazon Bedrock → AgentCore → Runtime agents

Both agents should show status **Active** within a few minutes of creation.
If they show **Failed**, check CloudWatch logs under `/aws/bedrock-agentcore/`.

---

## Step 7 — SAM Redeploy: Inject AgentCore ARNs

Now that you have both agent ARNs, update `backend/samconfig.toml` to
uncomment and fill in the AgentCore lines:

```toml
parameter_overrides = [
    "S3VectorBucket=lit-llm-s3-vectors-YOUR_ACCOUNT_ID",
    "S3DataBucket=lit-review-papers",
    "S3CsvBucket=lit-review-upload",
    "OpenAIApiKeyArn=arn:aws:secretsmanager:us-east-2:YOUR_ACCOUNT_ID:secret:lit-review/openai-XXXXXX",
    "AgentCoreRetrieveRankArn=arn:aws:bedrock-agentcore:us-east-2:ACCOUNT_ID:agent-runtime/RETRIEVE_RANK_ID",
    "AgentCoreGenerateArn=arn:aws:bedrock-agentcore:us-east-2:ACCOUNT_ID:agent-runtime/GENERATE_ID",
]
```

Redeploy:

```bash
cd backend
sam build
sam deploy
```

This injects the ARNs into Lambda 2 and Lambda 3 environment variables so they
can invoke the agents.

### 7a. Verify Lambda environment variables

**Console:** Lambda → `lit-review-retrieve-rank` → Configuration → Environment variables
→ `AGENTCORE_RETRIEVE_RANK_ARN` should be set.

**Console:** Lambda → `lit-review-generate` → Configuration → Environment variables
→ `AGENTCORE_GENERATE_ARN` should be set.

---

## Step 8 — Frontend: Build and Host on S3 + CloudFront

### 8a. Create environment file

In `frontend/`, create `.env.production` with the values from Step 3b:

```bash
# frontend/.env.production
NEXT_PUBLIC_API_URL=https://XXXXXXXXXX.execute-api.us-east-2.amazonaws.com
NEXT_PUBLIC_GENERATE_URL=https://XXXXXXXXXXXXXXXXXX.lambda-url.us-east-2.on.aws
```

### 8b. Build the static site

```bash
cd frontend
npm install
npm run build
# Output is in frontend/out/
```

### 8c. Create S3 bucket for static hosting

**Console:** S3 → Create bucket

- Name: `lit-review-frontend-ACCOUNT_ID`
- Region: `us-east-2`
- **Uncheck** "Block all public access" (frontend must be publicly readable)
- Enable static website hosting: Properties → Static website hosting → Enable
  - Index document: `index.html`
  - Error document: `index.html`

Or via CLI:
```bash
FRONTEND_BUCKET="lit-review-frontend-${ACCOUNT_ID}"

aws s3 mb s3://${FRONTEND_BUCKET} --region us-east-2

aws s3 website s3://${FRONTEND_BUCKET} \
  --index-document index.html \
  --error-document index.html
```

### 8d. Set bucket policy for public read

**Console:** S3 → `lit-review-frontend-ACCOUNT_ID` → Permissions → Bucket policy

Paste:
```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": "*",
    "Action": "s3:GetObject",
    "Resource": "arn:aws:s3:::lit-review-frontend-ACCOUNT_ID/*"
  }]
}
```

### 8e. Upload the built frontend

```bash
aws s3 sync frontend/out/ s3://${FRONTEND_BUCKET}/ \
  --delete \
  --cache-control "public,max-age=31536000,immutable"

# HTML files should not be cached long-term
aws s3 cp frontend/out/index.html s3://${FRONTEND_BUCKET}/index.html \
  --cache-control "no-cache,no-store,must-revalidate" \
  --content-type "text/html"
```

### 8f. Create CloudFront distribution (recommended)

**Console:** CloudFront → Create distribution

- Origin domain: select the S3 bucket website endpoint (not the bucket itself)
- Viewer protocol policy: Redirect HTTP to HTTPS
- Default root object: `index.html`
- Price class: Use only North America and Europe

After creation (takes ~10 min), note the CloudFront domain:
`https://XXXXXXXXXXXX.cloudfront.net`

> **Without CloudFront:** You can access the site directly at the S3 website URL
> `http://lit-review-frontend-ACCOUNT_ID.s3-website.us-east-2.amazonaws.com`
> but it will be HTTP only.

---

## Step 9 — Smoke Tests

Set these variables once before running the tests:

```bash
API="https://yybtmvac9a.execute-api.us-east-2.amazonaws.com"
GEN_URL="https://yvpqwr3cipxjlv2wmwlms322jq0bmzqh.lambda-url.us-east-2.on.aws/"
CSV="/path/to/papers.csv"   # must have columns: id, title, abstract (3+ rows)
```

### 9a. Lambda 1 — upload-and-index (returns 202 immediately)

```bash
UPLOAD_RESP=$(curl -s -X POST "$API/api/upload-and-index" -F "file=@$CSV")
echo "$UPLOAD_RESP"
SESSION_ID=$(echo "$UPLOAD_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['session_id'])")

# Expected:
# {"session_id":"abc12345","filename":"papers.csv","s3_csv_uri":"s3://...","status":"INDEXING","message":"Indexing started..."}
```

### 9b. Lambda 3 — session-status (poll until INDEXED)

Lambda 2 (build-index) runs async. Poll every 10s; indexing takes ~1–3 min for 100 papers.

```bash
# Poll until status=INDEXED or ERROR
while true; do
  STATUS=$(curl -s "$API/api/session/$SESSION_ID/status")
  echo "$STATUS"
  S=$(echo "$STATUS" | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])")
  [ "$S" = "INDEXED" ] || [ "$S" = "ERROR" ] && break
  sleep 10
done

# Expected when done:
# {"session_id":"abc12345","status":"INDEXED","total_abstracts":78,"chunks_created":1138,...}
```

### 9c. Lambda 5 — retrieve-and-rank (returns 202 immediately)

```bash
curl -s -X POST "$API/api/retrieve-and-rank" \
  -H "Content-Type: application/json" \
  -d "{\"session_id\":\"$SESSION_ID\",\"research_idea\":\"Using large language models to summarize legal documents\",\"hybrid_k\":10}"

# Expected:
# {"session_id":"abc12345","status":"RANKING","message":"Ranking started..."}
```

### 9d. Lambda 3 — session-status (poll until RANKED)

Lambda 6 (rank-worker) runs async via AgentCore. Ranking takes ~20–60s.

```bash
while true; do
  STATUS=$(curl -s "$API/api/session/$SESSION_ID/status")
  echo "$STATUS"
  S=$(echo "$STATUS" | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])")
  [ "$S" = "RANKED" ] || [ "$S" = "ERROR" ] && break
  sleep 10
done

# Expected when done:
# {"session_id":"abc12345","status":"RANKED",...}
```

### 9e. Lambda 4 — ranked-papers

```bash
curl -s "$API/api/session/$SESSION_ID/ranked-papers" | python3 -c "
import sys, json
d = json.load(sys.stdin)
print('top_k_papers:', len(d.get('top_k_papers', [])))
print('all_scored_papers:', len(d.get('all_scored_papers', [])))
print('retrieval_stats:', d.get('retrieval_stats'))
"

# Expected: top_k_papers: 3, all_scored_papers: N, retrieval_stats: {...}
```

### 9f. Lambda 7 — generate (streaming)

```bash
curl -s -N -X POST "$GEN_URL" \
  -H "Content-Type: application/json" \
  -d "{\"session_id\":\"$SESSION_ID\",\"research_idea\":\"Using large language models to summarize legal documents\",\"selected_paper_ids\":[]}"

# Expected: stream of SSE-formatted data lines ending with:
# data: "data: [METADATA]{...}\n\n"
# data: "data: [DONE]\n\n"
```

### 9g. Full browser test

Open the CloudFront URL (or S3 website URL). Run the full 3-step flow:
1. Upload a CSV → verify "Index created successfully" green banner (~1–3 min)
2. Enter a research idea → click "Retrieve & Rank Papers" → verify ranked list appears (~30s)
3. Click "Generate Related Work" → verify text streams in on the right panel

---

## Summary: What Lives Where

| Component | AWS Service | Deployed by |
|---|---|---|
| DynamoDB `LitReviewSessions` | DynamoDB | SAM (`template.yaml`) |
| Lambda 1 upload-and-index | Lambda | SAM |
| Lambda 2 retrieve-rank | Lambda | SAM |
| Lambda 3 generate | Lambda + Function URL | SAM |
| API Gateway (upload + rank endpoints) | HTTP API | SAM |
| Lambda Layer (shared code) | Lambda Layer | SAM |
| Retrieve-rank agent | AgentCore Runtime | ECR + CLI |
| Generate agent | AgentCore Runtime | ECR + CLI |
| OpenAI API key | Secrets Manager | Manual (Step 2b) |
| S3 buckets (3×) | S3 | Manual (Step 2a) |
| AgentCore IAM role | IAM | Manual (Step 4) |
| Frontend static site | S3 + CloudFront | Manual (Step 8) |

## Environment Variable Reference

| Variable | Set in | Value |
|---|---|---|
| `DYNAMODB_TABLE` | SAM (auto) | `LitReviewSessions` |
| `S3_DATA_CSV` | SAM parameter | `lit-review-upload` |
| `BEDROCK_KNOWLEDGE_S3_DATA` | SAM parameter | `lit-review-papers` |
| `S3_VECTOR` | SAM parameter | `lit-llm-s3-vectors-ACCOUNT_ID` |
| `OPENAI_API_KEY` | SAM → Secrets Manager | OpenAI secret ARN |
| `AGENTCORE_RETRIEVE_RANK_ARN` | SAM parameter | Agent runtime ARN |
| `AGENTCORE_GENERATE_ARN` | SAM parameter | Agent runtime ARN |
| `NEXT_PUBLIC_API_URL` | `frontend/.env.production` | API Gateway URL |
| `NEXT_PUBLIC_GENERATE_URL` | `frontend/.env.production` | Lambda 3 Function URL |

## Updating After Changes

| Change | Action |
|---|---|
| Lambda handler code | `sam build && sam deploy` |
| Shared layer code | `sam build && sam deploy` (layer version auto-increments) |
| Agent code | `docker buildx build … && docker push … && aws bedrock-agentcore update-agent-runtime …` |
| Frontend code | `npm run build && aws s3 sync frontend/out/ s3://BUCKET/ && aws cloudfront create-invalidation --distribution-id DIST_ID --paths "/*"` |
| New env var for Lambda | Add to `template.yaml` globals or function env, then `sam deploy` |
| DynamoDB schema change | Edit `template.yaml` table definition, then `sam deploy` |
