# Plan: Refactor to Full Serverless Architecture

## Context
The current app runs as a single FastAPI server (server.py) with in-memory sessions, deployed optionally via Mangum to a single Lambda. The goal is a true serverless architecture: 3 dedicated Lambda functions, 2 agents on AWS AgentCore Runtime, DynamoDB for session state, API Gateway + CloudFront + S3 static hosting for the frontend. This enables horizontal scaling, no persistent server, and proper session durability.

---

## Target Architecture

```
Browser
  ├─ POST /api/upload-and-index  → API Gateway → Lambda 1 (upload+index)
  ├─ POST /api/retrieve-and-rank → API Gateway → Lambda 2 (wrapper) → AgentCore RetrieveRank Agent
  └─ POST /api/generate          → Lambda 3 Function URL (streaming) → AgentCore Generate Agent

All Lambdas + Agents ↔ DynamoDB (LitReviewSessions table)
Lambda 1 + Agents ↔ S3 (CSV, papers, vectors, ranked papers JSON)
AgentCore Agents ↔ OpenAI API (outbound internet via networkMode=PUBLIC)
```

---

## New File Structure

```
lit-review-serverless/
├── backend/
│   ├── shared/                        # Lambda Layer shared by all functions
│   │   ├── pipeline.py                # MODIFIED (remove FastAPI import, refactor generator)
│   │   ├── vectorstore.py             # COPIED unchanged
│   │   ├── s3_storage.py              # COPIED unchanged
│   │   ├── config.py                  # MODIFIED (remove dotenv load)
│   │   └── dynamodb_session.py        # NEW
│   ├── lambdas/
│   │   ├── upload_index/
│   │   │   ├── handler.py             # NEW: Lambda 1
│   │   │   └── requirements.txt
│   │   ├── retrieve_rank/
│   │   │   ├── handler.py             # NEW: Lambda 2 (AgentCore wrapper)
│   │   │   └── requirements.txt       # boto3 only
│   │   └── generate/
│   │       ├── handler.py             # NEW: Lambda 3 (AgentCore wrapper, streaming)
│   │       └── requirements.txt       # boto3 + awslambdaric only
│   ├── agents/
│   │   ├── retrieve_rank_agent/
│   │   │   ├── agent.py               # NEW: AgentCore agent (bedrock-agentcore SDK)
│   │   │   ├── Dockerfile             # NEW: ARM64 container for ECR
│   │   │   └── requirements.txt       # bedrock-agentcore + full ML deps
│   │   └── generate_agent/
│   │       ├── agent.py               # NEW: AgentCore agent (bedrock-agentcore SDK)
│   │       ├── Dockerfile             # NEW: ARM64 container for ECR
│   │       └── requirements.txt       # bedrock-agentcore + full ML deps
│   ├── iam/
│   │   └── agentcore-role-policy.json # NEW: IAM policy for AgentCore execution role
│   ├── template.yaml                  # NEW: AWS SAM template (Lambdas + DynamoDB)
│   └── samconfig.toml                 # NEW: SAM deploy config
├── frontend/
│   └── app/page.tsx                   # MODIFIED: session_id in body, dual API URLs
└── .env.example                       # UPDATED with new vars
```

> **Deployment split:** SAM deploys the 3 Lambda functions + DynamoDB. AgentCore agents are deployed separately via ECR + AWS CLI (SAM does not yet have a native `AWS::BedrockAgentCore::AgentRuntime` resource type for custom code agents).

---

## Step 1: DynamoDB Table (`LitReviewSessions`)

**PK:** `session_id` (String) — no sort key

| Attribute | Type | Description |
|---|---|---|
| `session_id` | S | 8-char UUID, PK |
| `ttl` | N | Unix epoch + 86400s (auto-deleted by DynamoDB) |
| `created_at` | N | Unix epoch |
| `status` | S | `INDEXING` → `INDEXED` → `RANKING` → `RANKED` → `GENERATING` → `DONE` \| `ERROR` |
| `filename` | S | Original CSV filename |
| `s3_csv_uri` | S | `s3://bucket/session_id/file.csv` |
| `index_name` | S | S3 Vectors index name, e.g. `abc12345-index` |
| `s3_vector_bucket` | S | S3 Vectors bucket name |
| `s3_data_bucket` | S | Paper documents S3 bucket |
| `total_abstracts` | N | Paper count from indexing |
| `chunks_created` | N | Chunk count from indexing |
| `query` | S | Research idea text |
| `hybrid_k` | N | Retrieval k value |
| `ranked_papers_s3_key` | S | S3 key for ranked_papers.json (stored separately — DynamoDB 400KB limit) |
| `error_message` | S | Error detail if status=ERROR |

**Ranked papers S3 object:** stored at `ranked/{session_id}/ranked_papers.json` in `BEDROCK_KNOWLEDGE_S3_DATA` bucket.

---

## Step 2: Shared Code Changes

### `shared/pipeline.py` — 2 surgical changes
1. **Remove line 10:** `from fastapi.responses import StreamingResponse`
2. **Refactor `generate_related_work_text()`:** when `stream=True`, return the inner `event_stream()` generator directly instead of wrapping it in `StreamingResponse`. Update return type hint to `Union[Tuple[str, GenerationMetadata], Generator[str, None, None]]`.

All other functions (build_index, retrieve_and_rank_papers, RelevanceAgent, score_papers_async) are **unchanged**.

### `shared/config.py` — 1 change
Remove the `load_dotenv` call and the `Path`/dotenv imports. Lambda injects env vars at runtime.

### `shared/vectorstore.py`, `shared/s3_storage.py`
Copy verbatim — no changes needed.

---

## Step 3: `shared/dynamodb_session.py` (New)

**Class `DynamoDBSessionStore`** — wraps all DynamoDB operations:

```python
class DynamoDBSessionStore:
    def create_session(session_id, filename, s3_csv_uri, s3_vector_bucket, s3_data_bucket)
    def update_after_indexing(session_id, index_name, total_abstracts, chunks_created)
    def get_session(session_id) -> dict          # ConsistentRead=True
    def update_status(session_id, status, error_message=None)
    def save_ranked_papers(session_id, query, hybrid_k, ranked_papers_s3_key)
    def get_ranked_papers_key(session_id) -> str

# Module-level helpers for S3 ranked papers JSON
def save_ranked_papers_to_s3(s3_bucket, session_id, ranked_data_dict) -> str  # returns s3_key
def load_ranked_papers_from_s3(s3_bucket, s3_key) -> dict
```

**Exceptions:** `SessionError` (base), `SessionNotFoundError`, `SessionStateError`

---

## Step 4: Lambda 1 — Upload & Index (`lambdas/upload_index/handler.py`)

**Trigger:** API Gateway POST `/api/upload-and-index` (multipart/form-data)
**Timeout:** 900s, **Memory:** 2048MB

**Logic:**
1. Parse multipart body from base64-encoded API Gateway event (use `python-multipart`)
2. Validate CSV extension and size
3. Generate `session_id = str(uuid.uuid4())[:8]`
4. Upload CSV to S3 via `S3CSVStorage.upload_csv()`
5. `DynamoDBSessionStore.create_session(...)` → status=`INDEXING`
6. Build `PipelineConfig`, create `LiteratureReviewPipeline`
7. Call `pipeline.build_index(s3_csv_uri)` → `IndexResult`
8. `DynamoDBSessionStore.update_after_indexing(...)` → status=`INDEXED`
9. Return JSON with `session_id`, index stats

**No Mangum** — native Lambda handler directly parses the API Gateway event.

**requirements.txt:** boto3, langchain-openai, langchain-core, langchain-text-splitters, numpy, pandas, pydantic, tqdm, python-multipart

> `openai-agents` is NOT needed — only used in relevance scoring (retrieve-rank agent). `openai` is a transitive dep of `langchain-openai`. `fastapi` and `mangum` are not needed (native handler).

---

## Step 5: AgentCore Retrieve-Rank Agent (`agents/retrieve_rank_agent/`)

**SDK:** `bedrock-agentcore` Python package — wraps the agent as a lightweight HTTP server exposing `/invocations` (POST) and `/ping` (GET) endpoints, which AgentCore Runtime calls internally.

**Deployment process:**
1. Build ARM64 Docker image: `docker buildx build --platform linux/arm64 -t retrieve-rank-agent .`
2. Push to ECR: `aws ecr create-repository --repository-name lit-review-retrieve-rank` → `docker push`
3. Create AgentCore Runtime agent via CLI:
   ```bash
   aws bedrock-agentcore create-agent-runtime \
     --agent-runtime-name lit-review-retrieve-rank \
     --agent-runtime-artifact containerImage={uri=ECR_IMAGE_URI} \
     --execution-role-arn arn:aws:iam::ACCOUNT:role/AgentCoreExecutionRole \
     --network-configuration networkMode=PUBLIC
   ```
4. Note the returned `agentRuntimeArn` — set as `AGENTCORE_RETRIEVE_RANK_ARN` env var in Lambda 2

**`Dockerfile` (ARM64):**
```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY agent.py shared/ ./
EXPOSE 8080
CMD ["python", "agent.py"]
```

**`agent.py` structure:**
```python
from bedrock_agentcore import AgentCore
from shared.pipeline import LiteratureReviewPipeline, PipelineConfig
from shared.dynamodb_session import DynamoDBSessionStore, save_ranked_papers_to_s3

app = AgentCore()

@app.entrypoint
def handler(payload, context):
    # payload is the parsed JSON from invoke_agent_runtime()
    session_id = payload["session_id"]
    # ... run retrieve_and_rank_papers(), save to S3 + DynamoDB ...
    return {"status": "success", "data": ranked_data}

if __name__ == "__main__":
    app.run()  # starts HTTP server on port 8080
```

**`requirements.txt`:** bedrock-agentcore, boto3, openai, openai-agents, langchain-openai, langchain-core, langchain-text-splitters, numpy, pandas, pydantic, tqdm

---

## Step 6: Lambda 2 — Retrieve & Rank Wrapper (`lambdas/retrieve_rank/handler.py`)

**Trigger:** API Gateway POST `/api/retrieve-and-rank` (JSON body)
**Timeout:** 30s (wrapper only), **Memory:** 512MB

**Input:** `{"session_id": "...", "research_idea": "...", "hybrid_k": 50}`

**Logic:**
1. Validate input, load session from DynamoDB, check status=`INDEXED`
2. Set status=`RANKING`
3. Invoke AgentCore retrieve-rank agent via `bedrock-agentcore` boto3 client:
```python
client = boto3.client('bedrock-agentcore', region_name=REGION)
response = client.invoke_agent_runtime(
    agentRuntimeArn=os.environ['AGENTCORE_RETRIEVE_RANK_ARN'],
    runtimeSessionId=session_id,
    payload=json.dumps(payload).encode(),
    contentType='application/json'
)
# Collect streaming outputStream
result = b""
for event in response['outputStream']:
    result += event.get('chunk', {}).get('bytes', b'')
```
4. Parse JSON result returned by the agent
5. Return ranked papers JSON to frontend

**Required IAM permission:** `bedrock-agentcore:InvokeAgentRuntime` on the agent ARN

**requirements.txt:** boto3 only

---

## Step 7: AgentCore Generate Agent (`agents/generate_agent/`)

**SDK:** Same `bedrock-agentcore` pattern. AgentCore supports streaming responses — the agent yields chunks through its `/invocations` response, which AgentCore forwards as a streaming `outputStream` to the caller (Lambda 3).

**Deployment:** Same ECR + `aws bedrock-agentcore create-agent-runtime` process as retrieve-rank agent. Returns a separate `agentRuntimeArn` set as `AGENTCORE_GENERATE_ARN`.

**`agent.py` structure:**
```python
from bedrock_agentcore import AgentCore
from shared.pipeline import generate_related_work_text
from shared.dynamodb_session import load_ranked_papers_from_s3

app = AgentCore()

@app.entrypoint
def handler(payload, context):
    # Load ranked papers from S3, build papers_df
    # Call generate_related_work_text(stream=True) -> plain generator
    # Yield each SSE chunk — bedrock-agentcore streams them back
    for chunk in generate_related_work_text(..., stream=True):
        yield chunk

if __name__ == "__main__":
    app.run()
```

**`requirements.txt`:** bedrock-agentcore, boto3, openai, langchain-openai, langchain-core, numpy, pandas, pydantic

---

## Step 8: Lambda 3 — Generate Wrapper (`lambdas/generate/handler.py`)

**Trigger:** Lambda Function URL with `InvokeMode: RESPONSE_STREAM` (NOT API Gateway — needed for SSE beyond 30s)
**Timeout:** 900s, **Memory:** 512MB

**Input:** `{"session_id": "...", "research_idea": "...", "selected_paper_ids": [...]}`

**Logic:**
1. Validate input, load session from DynamoDB, check status=`RANKED`
2. Get `ranked_papers_s3_key` from session
3. Set status=`GENERATING`
4. Invoke AgentCore generate agent via `bedrock-agentcore` client with streaming:
```python
client = boto3.client('bedrock-agentcore', region_name=REGION)
response = client.invoke_agent_runtime(
    agentRuntimeArn=os.environ['AGENTCORE_GENERATE_ARN'],
    runtimeSessionId=session_id,
    payload=json.dumps(payload).encode(),
    contentType='application/json'
)
# Forward each streaming chunk directly to Lambda response stream
for event in response['outputStream']:
    chunk = event.get('chunk', {}).get('bytes', b'')
    if chunk:
        response_stream.write(chunk)
```
5. Set status=`DONE` when `[DONE]` chunk received; close stream

**requirements.txt:** boto3, awslambdaric

---

## Step 9: IAM Roles

### 9a. AgentCore Execution Role (`iam/agentcore-role-policy.json`)

Created manually (one-time) before deploying agents. SAM only manages Lambda IAM.

**Trust policy principal:** `bedrock-agentcore.amazonaws.com`

**Permission policies:**

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem"],
      "Resource": "arn:aws:dynamodb:REGION:ACCOUNT:table/LitReviewSessions"
    },
    {
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject", "s3:ListBucket"],
      "Resource": [
        "arn:aws:s3:::lit-review-papers/*",
        "arn:aws:s3:::lit-llm-s3-vectors-ACCOUNT/*"
      ]
    },
    {
      "Effect": "Allow",
      "Action": ["secretsmanager:GetSecretValue"],
      "Resource": "arn:aws:secretsmanager:REGION:ACCOUNT:secret:lit-review/openai*"
    },
    {
      "Effect": "Allow",
      "Action": ["s3vectors:QueryVectors", "s3vectors:GetVectors"],
      "Resource": "*"
    }
  ]
}
```

> **Outbound internet (OpenAI API):** `networkMode=PUBLIC` on AgentCore Runtime provides outbound access — no VPC/NAT Gateway needed.

### 9b. Lambda IAM (managed in SAM `template.yaml`)

| Lambda | Policies |
|---|---|
| Lambda 1 | S3 CRUD (CSV + data buckets), DynamoDB CRUD, `s3vectors:*`, `secretsmanager:GetSecretValue` |
| Lambda 2 | DynamoDB CRUD, `bedrock-agentcore:InvokeAgentRuntime` on retrieve-rank agent ARN |
| Lambda 3 | DynamoDB read, `bedrock-agentcore:InvokeAgentRuntime` on generate agent ARN |

---

## Step 10: SAM `template.yaml` — Key Resources

```
Resources:
  SessionsTable          (AWS::DynamoDB::Table — TTL on 'ttl' attr, PAY_PER_REQUEST)
  SharedCodeLayer        (AWS::Serverless::LayerVersion — CodeUri: shared/)
  LitReviewApi           (AWS::Serverless::HttpApi — CORS configured)
  UploadIndexFunction    (AWS::Serverless::Function — 900s/2048MB, HttpApi event)
  RetrieveRankFunction   (AWS::Serverless::Function — 30s/512MB, HttpApi event)
  GenerateFunction       (AWS::Serverless::Function — 900s/512MB, FunctionUrlConfig RESPONSE_STREAM)

Parameters:
  AgentCoreRetrieveRankArn   (AgentCore agent ARN — passed after agent deployment)
  AgentCoreGenerateArn       (AgentCore agent ARN — passed after agent deployment)

Outputs:
  ApiGatewayUrl          (for NEXT_PUBLIC_API_URL)
  GenerateFunctionUrl    (for NEXT_PUBLIC_GENERATE_URL)
```

**OPENAI_API_KEY** stored in AWS Secrets Manager, resolved via `{{resolve:secretsmanager:lit-review/openai:SecretString:api_key}}` in SAM globals. Agents retrieve it directly via `secretsmanager:GetSecretValue` using their execution role.

**Deployment order:**
1. `sam deploy` → DynamoDB table + 3 Lambda functions + API Gateway
2. Create AgentCore execution role manually (one-time)
3. Build + push agent Docker images to ECR (ARM64)
4. `aws bedrock-agentcore create-agent-runtime` for each agent → capture ARNs
5. `sam deploy` again with `AgentCoreRetrieveRankArn` and `AgentCoreGenerateArn` parameters

---

## Step 11: Frontend Changes (`frontend/app/page.tsx`)

1. **Add state:** `const [sessionId, setSessionId] = useState<string | null>(null)`
2. **After upload success:** extract and store `result.data.session_id`
3. **All subsequent requests:** pass `session_id` in JSON body, remove `credentials: 'include'`
4. **Generate call:** use `NEXT_PUBLIC_GENERATE_URL` (Lambda Function URL, separate from API Gateway URL)
5. **File removal:** also call `setSessionId(null)`

**New env vars for frontend:**
```
NEXT_PUBLIC_API_URL=https://{api-gw}.execute-api.us-east-2.amazonaws.com     # Lambdas 1+2
NEXT_PUBLIC_GENERATE_URL=https://{fn-url}.lambda-url.us-east-2.on.aws        # Lambda 3
```

---

## Step 12: Step-by-Step Testing

Test each layer in isolation before integrating.

**Phase A — Shared code (local, no AWS)**
- Test `dynamodb_session.py` against DynamoDB Local (`docker run -p 8001:8000 amazon/dynamodb-local`)
- Test `pipeline.py` generator change: confirm `generate_related_work_text(stream=True)` returns a generator, not `StreamingResponse`
- Test `save/load_ranked_papers_to/from_s3` using `moto` mock

**Phase B — Lambda 1**
- `sam local invoke UploadIndexFunction -e events/upload_event.json` with small 5-paper CSV
- Verify: DynamoDB record created (status=INDEXED), S3 CSV uploaded, S3 Vectors index created
- Deploy: `sam build && sam deploy` — test via curl with real CSV

**Phase C — Retrieve-rank agent (standalone)**
- Run `agent.py` as plain Python script with a `session_id` from Phase B
- Verify: ranked_papers.json written to S3, DynamoDB updated to RANKED

**Phase D — AgentCore retrieve-rank agent + Lambda 2**
- Build ARM64 image, push to ECR, create AgentCore runtime, get ARN
- Deploy Lambda 2 with ARN, curl POST with `session_id` from Phase B
- Verify full response JSON with ranked papers

**Phase E — Generate agent (standalone)**
- Run `agent.py` locally with a `session_id` from Phase D
- Verify SSE chunks yield correctly, `[METADATA]` and `[DONE]` appear

**Phase F — Lambda 3 streaming**
- Deploy Lambda 3, curl with `-N` flag: `curl -N -X POST {function-url} -d '{...}'`
- Verify progressive SSE output

**Phase G — Frontend integration**
- Update env vars, run `npm run dev`, test full 3-phase flow in browser
- Verify `session_id` threads through all 3 calls

**Phase H — Error cases**
- Missing session_id → 400 with clear message
- Call generate before rank (wrong status) → 400
- Invalid CSV format → 400 from Lambda 1

---

## Environment Variables

| Variable | Used by | How set |
|---|---|---|
| `OPENAI_API_KEY` | Lambda 1 + agents | Lambda: `{{resolve:secretsmanager:...}}` in SAM; Agents: `secretsmanager:GetSecretValue` via execution role |
| `DYNAMODB_TABLE` | All Lambdas + agents | SAM globals / agent container env |
| `S3_VECTOR` | Lambda 1, retrieve-rank agent | SAM parameter / agent container env |
| `BEDROCK_KNOWLEDGE_S3_DATA` | Lambda 1, both agents | SAM parameter / agent container env |
| `S3_DATA_CSV` | Lambda 1 | SAM parameter |
| `DEFAULT_AWS_REGION` | All | SAM `AWS::Region` / agent container env |
| `AGENTCORE_RETRIEVE_RANK_ARN` | Lambda 2 | SAM parameter (set after agent deployed) |
| `AGENTCORE_GENERATE_ARN` | Lambda 3 | SAM parameter (set after agent deployed) |
| `NEXT_PUBLIC_API_URL` | Frontend | `.env.production` (API Gateway URL) |
| `NEXT_PUBLIC_GENERATE_URL` | Frontend | `.env.production` (Lambda 3 Function URL) |

---

## Critical Files to Modify

| File | Change |
|---|---|
| `backend/pipeline.py` | Remove FastAPI import; refactor `generate_related_work_text()` to return plain generator |
| `backend/config.py` | Remove `load_dotenv` call |
| `frontend/app/page.tsx` | Add `sessionId` state, thread through all API calls, add `NEXT_PUBLIC_GENERATE_URL` |

## New Files to Create

**SAM-deployed (Lambdas):** `backend/shared/dynamodb_session.py`, `backend/lambdas/upload_index/handler.py`, `backend/lambdas/retrieve_rank/handler.py`, `backend/lambdas/generate/handler.py`, `backend/template.yaml`, `backend/samconfig.toml`, plus `requirements.txt` for each Lambda.

**AgentCore-deployed (agents, via ECR):** `backend/agents/retrieve_rank_agent/agent.py`, `backend/agents/retrieve_rank_agent/Dockerfile`, `backend/agents/generate_agent/agent.py`, `backend/agents/generate_agent/Dockerfile`, plus `requirements.txt` for each agent.

**IAM (manual, one-time):** `backend/iam/agentcore-role-policy.json`
