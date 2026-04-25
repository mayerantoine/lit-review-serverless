# Literature Review Serverless

Automatically generate a Related Work section from a corpus of papers. Upload a CSV of abstracts, retrieve and rank relevant papers against your research idea, then generate a cohesive literature review with inline citations — all serverless on AWS.

## Architecture

```
Browser
  POST /api/upload-and-index   → API Gateway → Lambda (upload + async index)
  POST /api/retrieve-and-rank  → API Gateway → Lambda → AgentCore (rank agent)
  POST /api/generate           → Lambda Function URL → OpenAI (direct call)

All Lambdas ↔ DynamoDB (session state)
Lambdas     ↔ S3 (CSV, paper JSON, S3 Vectors index, ranked results)
```

**Stack:** Python 3.12 Lambdas + shared layer, Next.js 15 frontend, DynamoDB, S3 Vectors, OpenAI API.

## Repo Structure

```
backend/
  lambdas/
    upload_index/    # Lambda 1 — upload CSV, async build vector index
    retrieve_rank/   # Lambda 2 — retrieve + rank papers via AgentCore
    generate/        # Lambda 3 — generate related work via OpenAI directly
    session_status/  # Lambda 4 — poll session status + fetch ranked papers
  agents/
    retrieve_rank_agent/   # AgentCore container — hybrid search + relevance scoring
    generate_agent/        # AgentCore container — (legacy, no longer invoked)
  shared/            # Shared layer: pipeline.py, vectorstore.py, dynamodb_session.py
  template.yaml      # SAM template
frontend/
  app/page.tsx       # Single-page Next.js app
```

## Prerequisites

- AWS CLI v2, configured for `us-east-2`
- AWS SAM CLI >= 1.120
- Docker Desktop (for agent images)
- Python 3.12, Node.js 20+
- OpenAI API key

## Deploy

### 1. AWS resources

```bash
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

# S3 buckets
aws s3 mb s3://lit-review-upload          --region us-east-2
aws s3 mb s3://lit-review-papers          --region us-east-2
aws s3 mb s3://lit-llm-s3-vectors-${ACCOUNT_ID} --region us-east-2

# OpenAI key in Secrets Manager (key field must be "api_key")
aws secretsmanager create-secret \
  --name lit-review/openai \
  --secret-string '{"api_key":"sk-..."}' \
  --region us-east-2
```

### 2. SAM deploy (Lambdas + DynamoDB + API Gateway)

```bash
cd backend
sam build
sam deploy --guided   # first time; saves config to samconfig.toml
```

Set these parameters when prompted:

| Parameter | Value |
|---|---|
| `S3VectorBucket` | `lit-llm-s3-vectors-ACCOUNT_ID` |
| `S3DataBucket` | `lit-review-papers` |
| `S3CsvBucket` | `lit-review-upload` |
| `OpenAIApiKeyArn` | ARN from step 1 |
| `AgentCoreRetrieveRankArn` | see step 3 |

Note the outputs: `ApiGatewayUrl` and `GenerateFunctionUrl`.

### 3. AgentCore retrieve-rank agent

```bash
REGION=us-east-2
ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/AgentCoreExecutionRole"
# Create the IAM role first — see DEPLOYMENT.md section 4

cd backend
bash agents/retrieve_rank_agent/deploy.sh
```

The script builds the ARM64 Docker image, pushes to ECR, and creates the AgentCore runtime. Copy the printed ARN, add it to `samconfig.toml` as `AgentCoreRetrieveRankArn`, then redeploy:

```bash
sam build && sam deploy
```

### 4. Frontend

```bash
cd frontend

# Create .env.production
echo "NEXT_PUBLIC_API_URL=https://<api-id>.execute-api.us-east-2.amazonaws.com" > .env.production
echo "NEXT_PUBLIC_GENERATE_URL=https://<fn-url>.lambda-url.us-east-2.on.aws" >> .env.production

npm install && npm run build

# Deploy to S3 + CloudFront (see DEPLOYMENT.md section 8)
aws s3 sync out/ s3://lit-review-frontend-${ACCOUNT_ID}/ --delete \
  --cache-control "public,max-age=31536000,immutable"
aws s3 cp out/index.html s3://lit-review-frontend-${ACCOUNT_ID}/index.html \
  --cache-control "no-cache,no-store,must-revalidate" --content-type "text/html"
```

## CSV Format

Your input file must have these columns:

| Column | Type | Notes |
|---|---|---|
| `id` | integer | unique per row |
| `title` | string | paper title |
| `abstract` | string | paper abstract |

Max 300 papers, 10 MB.

## Usage

1. Upload a CSV — papers are embedded and indexed (~1–3 min)
2. Enter your research idea → Retrieve & Rank — papers scored for relevance (~30s)
3. Select papers → Generate — related work section with `[id]` citations returned

Sessions expire after 24 hours.

## Full deployment guide

See [DEPLOYMENT.md](DEPLOYMENT.md) for step-by-step instructions including IAM roles, ECR setup, and CloudFront configuration.
