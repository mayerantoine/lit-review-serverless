# Errors and Fixes

A running log of all errors encountered, root causes, and lessons learned — covering CLI setup, SAM/CloudFormation, AgentCore, Lambda runtime, and frontend.

---

## Part 1 — CLI, SAM & AgentCore Setup

### 1. Wrong AWS CLI service name
- **Error:** `argument operation: Found invalid choice 'create-agent-runtime'`
- **Fix:** Use `aws bedrock-agentcore-control create-agent-runtime` (control plane), not `aws bedrock-agentcore create-agent-runtime` (data plane)

### 2. Wrong parameter: `--execution-role-arn`
- **Error:** `the following arguments are required: --role-arn`
- **Fix:** Replace `--execution-role-arn` with `--role-arn`

### 3. Wrong artifact format: `containerImage={uri=...}`
- **Error:** ValidationException on `--agent-runtime-artifact`
- **Fix:** Use `containerConfiguration={containerUri=...}` (shorthand syntax confirmed via `help`)

### 4. Agent runtime name contains hyphens
- **Error:** `Value 'lit-review-retrieve-rank' failed to satisfy constraint: pattern [a-zA-Z][a-zA-Z0-9_]{0,47}`
- **Fix:** Rename to `lit_review_retrieve_rank` (underscores only)

### 5. Missing ECR permissions on AgentCoreExecutionRole
- **Error:** `Access denied while validating ECR URI`
- **Fix:** Add to role policy: `ecr:GetAuthorizationToken`, `ecr:BatchGetImage`, `ecr:GetDownloadUrlForLayer` with `Resource: "*"`

### 6. Wrong `--parameter-overrides` format for ARNs containing `=`
- **Error:** `AGENTCORE_RETRIEVE_RANK_ARN=arn:...` is not a valid format
- **Fix:** Use `ParameterKey=AgentCoreRetrieveRankArn,ParameterValue=arn:...` format

---

## Part 2 — AWS SAM / CloudFormation

### 7. Missing `version = 0.1` in samconfig.toml
- **Error:** `SamConfigVersionException`
- **Fix:** Add `version = 0.1` at top of `samconfig.toml`

### 8. TOML inline comments inside array
- **Error:** TOML parse error on `parameter_overrides`
- **Fix:** Move all `#` comments outside the array block

### 9. SAM build fails: "Failed to find Python runtime containing pip"
- **Error:** `.venv/bin/python3.12` (no pip) shadowed Homebrew python on PATH
- **Fix:** `brew install python@3.12`, then `PATH="/opt/homebrew/bin:$PATH" sam build`

### 10. CloudFormation: "Resource must be in ARN format or *"
- **Error:** Empty string `""` used as IAM Resource for `bedrock-agentcore:InvokeAgentRuntime`
- **Fix:** Add `Conditions` block and use `!If [HasRetrieveRankArn, !Ref AgentCoreRetrieveRankArn, "*"]`

### 11. Stack stuck in UPDATE_ROLLBACK_FAILED
- **Error:** Cannot update a stack in ROLLBACK_FAILED state
- **Fix:** `aws cloudformation continue-update-rollback --stack-name lit-review-serverless`

### 12. Lambda Layer too large (347MB > 262MB limit)
- **Error:** `Function code combined with layers exceeds 262144000 bytes`
- **Fix:** Switch shared layer to `BuildMethod: makefile` with a `Makefile` that only copies `.py` files (no pip install into layer). Move heavy deps into each Lambda's own `requirements.txt`

---

## Part 3 — Lambda Runtime Errors

### 13. `No module named 's3_storage'`
- **Cause:** Lambda Layer `BuildMethod: python3.12` with `requirements.txt` installed packages but didn't place `.py` source files under `python/` correctly
- **Fix:** Use `BuildMethod: makefile` with `Makefile` that copies `*.py` to `$(ARTIFACTS_DIR)/python/`

### 14. `No module named 'agents'` (openai-agents)
- **Cause:** `pipeline.py` had `from agents import Agent, Runner` at module level; `upload_index` Lambda doesn't have `openai-agents` installed
- **Fix:** Move import inside the functions that need it (`create_relevance_agent`, `score_single_paper`) as lazy imports

### 15. API Gateway "Service Unavailable" with no CloudWatch logs
- **Cause:** API Gateway HTTP API has a hard 30s timeout; `upload-and-index` (indexing) takes several minutes
- **Fix:** Split into async pattern — fast handler returns 202 + `session_id`, fires `BuildIndexFunction` async (`InvocationType=Event`); frontend polls `/api/session/{id}/status`

### 16. `BuildIndexFunction` AccessDenied on S3 CSV bucket
- **Error:** `s3:GetObject` not allowed on `lit-review-upload` bucket
- **Fix:** Add `S3CrudPolicy: BucketName: !Ref S3CsvBucket` to `BuildIndexFunction` policies

---

## Part 4 — AgentCore Runtime Invocation

### 17. `acceptContentType` is not a valid parameter
- **Error:** `Parameter validation failed: Unknown parameter in input: "acceptContentType"`
- **Fix:** Replace `acceptContentType=` with `accept=`

### 18. `runtimeSessionId` too short (min 33 chars)
- **Error:** `Invalid length for parameter runtimeSessionId, value: 8, valid min length: 33`
- **Fix:** Pad: `runtime_session_id = f"session-{session_id}-{'x' * 25}"`

### 19. IAM `bedrock-agentcore:InvokeAgentRuntime` denied on runtime endpoint
- **Error:** Not authorized on `arn:.../runtime/lit_review_retrieve_rank-.../runtime-endpoint/DEFAULT`
- **Fix:** Allow both base ARN and wildcard: add both `!Ref AgentCoreRetrieveRankArn` and `!Sub "${AgentCoreRetrieveRankArn}/*"` as Resources

### 20. AgentCore response key is `response`, not `outputStream`
- **Error:** `KeyError: 'outputStream'`
- **Fix:** Check for `response` key first (a `StreamingBody`), fall back to `outputStream`

### 21. AgentCore response body is SSE format, not plain JSON
- **Cause:** Response `contentType: text/event-stream`; body is `data: "...\n\n"`
- **Fix:** Parse SSE lines: strip `data: ` prefix, then `json.loads()` the value; handle double-encoding (value is a JSON string containing JSON)

### 22. Agent container missing `nest_asyncio`
- **Error:** `No module named 'nest_asyncio'` inside agent
- **Fix:** Add `nest_asyncio` to `agents/retrieve_rank_agent/requirements.txt`

### 23. Dockerfile copied wrong `requirements.txt`
- **Cause:** `COPY requirements.txt .` picked up the root dev `requirements.txt` instead of the agent's
- **Fix:** Change to `COPY agents/retrieve_rank_agent/requirements.txt .` (same for generate agent)

### 24. AgentCore caches old container image despite `latest` tag update
- **Cause:** AgentCore Runtime doesn't re-pull `latest` on update
- **Fix:** Push with a new explicit tag (`:v2`, `:v3`, etc.) and update the runtime to point to the new tag

### 25. Retrieve-and-rank also hits 30s API Gateway timeout
- **Cause:** AgentCore agent takes ~60s to score papers; API Gateway times out
- **Fix:** Same async pattern as upload — fast handler returns 202, fires `RankWorkerFunction` async, frontend polls status until `RANKED`

---

## Part 5 — Environment Variables

### 26. AgentCore container crashes on startup: `KeyError: 'BEDROCK_KNOWLEDGE_S3_DATA'`
- **Cause:** Environment variables not set on the AgentCore Runtime (only set in SAM for Lambdas)
- **Fix:** `aws bedrock-agentcore-control update-agent-runtime --environment-variables "BEDROCK_KNOWLEDGE_S3_DATA=...,S3_VECTOR=...,DYNAMODB_TABLE=...,OPENAI_API_KEY=...,DEFAULT_AWS_REGION=us-east-2"`

---

## Part 6 — Production Runtime Issues

## 27. BuildIndexFunction — AccessDenied on S3 GetObject

**Error**
```
botocore.exceptions.ClientError: An error occurred (AccessDenied) when calling the GetObject
operation: User: arn:aws:sts::979294212144:assumed-role/lit-review-serverless-
BuildIndexFunctionRole-kOVjNu2gc7hE/lit-review-build-index is not authorized to perform:
s3:GetObject on resource: "arn:aws:s3:::lit-review-upload/b2d01077/rag.csv" because no
identity-based policy allows the s3:GetObject action
```

**Symptom:** Upload returns 202 but session stays in ERROR status immediately after INDEXING.

**Root cause:** The IAM role for `BuildIndexFunction` was missing `s3:GetObject` on the CSV bucket. The `S3CrudPolicy` was present in `template.yaml` but the stack had not been redeployed after it was added — SAM's change detection did not flag it as a drift.

**Fix:** `sam deploy` would normally fix this, but reported "No changes to deploy." Applied the policy manually via CLI, then confirmed the subsequent deploy picked it up:
```bash
aws iam put-role-policy \
  --role-name "lit-review-serverless-BuildIndexFunctionRole-kOVjNu2gc7hE" \
  --policy-name "S3CsvBucketAccess" \
  --policy-document '{ "Version":"2012-10-17", "Statement":[{ "Effect":"Allow",
    "Action":["s3:GetObject","s3:PutObject","s3:DeleteObject","s3:ListBucket"],
    "Resource":["arn:aws:s3:::lit-review-upload","arn:aws:s3:::lit-review-upload/*"] }] }'
  --region us-east-2
```

**Lesson:** After adding a new IAM policy to `template.yaml`, always verify the role actually has it (`aws iam list-role-policies`). If SAM says "no changes" but the permission is missing, apply it manually as a hotfix then investigate why SAM's hash didn't detect the change (often a cached build or a previous failed deploy).

---

## 28. GenerateFunction — AttributeError at import time

**Error**
```
{"errorMessage": "module 'awslambdaric.bootstrap' has no attribute 'lambda_handler_streaming'",
 "errorType": "AttributeError"}
```

**Symptom:** Every call to the generate Lambda Function URL returned an error JSON immediately — no streaming, no session update.

**Root cause:** `handler.py` imported `awslambdaric.bootstrap` at module level and decorated a function with `@bootstrap.lambda_handler_streaming`. This attribute does not exist in any version of `awslambdaric` (checked 1.0.0 through 4.0.0). The decorator was based on a non-existent API. Because the error occurred at **import time**, the Lambda crashed before any handler code ran on every invocation.

**Fix:** Removed the entire streaming wrapper block. The `lambda_handler` function already returns an SSE-formatted response body and works correctly with `InvokeMode=RESPONSE_STREAM` on the Function URL without any decorator.

**Lesson:** `awslambdaric` does not provide a `lambda_handler_streaming` decorator. Lambda response streaming for Python works natively — no special decorator is needed. The handler is registered via `Handler: handler.lambda_handler` in `template.yaml` and the Function URL `InvokeMode: RESPONSE_STREAM` handles the rest. Always test Lambda imports with a minimal `aws lambda invoke` before assuming module-level code is safe.

---

## 29. GenerateFunction — DynamoDB UpdateItem AccessDeniedException

**Error**
```
An error occurred (AccessDeniedException) when calling the UpdateItem operation:
User: arn:aws:sts::979294212144:assumed-role/lit-review-serverless-GenerateFunctionRole-.../
lit-review-generate is not authorized to perform: dynamodb:UpdateItem on resource:
arn:aws:dynamodb:us-east-2:979294212144:table/LitReviewSessions
```

**Symptom:** Generate Lambda started successfully after fix #28 but failed immediately when trying to set session status to GENERATING.

**Root cause:** `template.yaml` had `DynamoDBReadPolicy` for the generate function, but the function calls `session_store.update_status()` which requires `UpdateItem` (a write operation). `DynamoDBReadPolicy` only grants read actions.

**Fix:** Changed `DynamoDBReadPolicy` to `DynamoDBCrudPolicy` in `template.yaml` for `GenerateFunction`, then redeployed:
```yaml
Policies:
  - DynamoDBCrudPolicy:
      TableName: !Ref SessionsTable
```

**Lesson:** Any Lambda that updates session state (GENERATING, DONE, ERROR) needs `DynamoDBCrudPolicy`, not `DynamoDBReadPolicy`. Audit all Lambdas that call `update_status()` or any DynamoDB write operation and ensure their policy grants write access.

---

## 30. Frontend — localhost:8000 baked into production build

**Error**
```
localhost:8000/api/upload-and-index:1 Failed to load resource: net::ERR_CONNECTION_REFUSED
```

**Symptom:** The deployed CloudFront site made all API calls to `localhost:8000` instead of the API Gateway URL.

**Root cause:** Next.js env file priority: `.env.local` always overrides `.env.production`, even during `next build`. The `.env.local` file contained `NEXT_PUBLIC_API_URL=http://localhost:8000`, which was baked into the static export at build time.

**Fix:** Commented out `NEXT_PUBLIC_API_URL` in `.env.local` so `.env.production` values take effect during production builds. Rebuilt and resynced to S3:
```bash
# frontend/.env.local — comment out for production builds
# NEXT_PUBLIC_API_URL=http://localhost:8000

npm run build
aws s3 sync frontend/out/ s3://lit-review-frontend-979294212144/ --delete
aws cloudfront create-invalidation --distribution-id E21DGM1DMP4RRB --paths "/*"
```

**Lesson:** Next.js `.env.local` takes priority over `.env.production` at build time — it is not "local only" in the sense of being ignored during builds. Never put `NEXT_PUBLIC_*` values in `.env.local` that differ from production. Use `.env.local` only for values that are genuinely not needed during the build (e.g., secrets used server-side, which don't apply to static exports).

---

## 31. CloudFront — 504 Gateway Timeout

**Error**
```
HTTP/2 504
x-cache: Error from cloudfront
```

**Symptom:** The CloudFront URL returned a 504 on every request. S3 static website endpoint worked fine directly via HTTP.

**Root cause:** Two misconfigured CloudFront origin settings:
1. `OriginPath` was set to `/index.html` — CloudFront appended this to every request path, so `/_next/static/chunks/foo.js` became `/_next/static/chunks/foo.js/index.html` (404 on S3).
2. `OriginProtocolPolicy` was `https-only` — S3 static website endpoints only support HTTP, so CloudFront could not connect to the origin.

Additionally, `DefaultRootObject` was empty, so requests to `/` did not resolve to `index.html`.

**Fix:** Updated the CloudFront distribution config:
```bash
# OriginPath: "" (empty)
# OriginProtocolPolicy: http-only
# DefaultRootObject: index.html
aws cloudfront update-distribution --id E21DGM1DMP4RRB \
  --distribution-config file:///tmp/cf_config_fixed.json --if-match $ETAG
```

**Lesson:** When pointing CloudFront at an S3 static website endpoint (`.s3-website.region.amazonaws.com`), always use `http-only` as the origin protocol — S3 website endpoints do not support HTTPS. Leave `OriginPath` empty unless you intentionally want to prefix all paths. Set `DefaultRootObject` to `index.html` for SPAs. If using S3 REST endpoint instead (`.s3.region.amazonaws.com`), use OAC (Origin Access Control) with HTTPS — that endpoint does support HTTPS.

---

## 32. UploadIndexFunction / BuildIndexFunction — No module named 'pandas' (persistent)

**Error**
```
[ERROR] Runtime.ImportModuleError: Unable to import module 'handler': No module named 'pandas'
```

**Symptom:** Lambda 1 returned Internal Server Error on every invocation despite `pandas` being listed in `requirements.txt` and `sam build --no-cached` confirming it was installed.

**Root cause:** Running `sam build GenerateFunction` (to rebuild only one function) **replaced the entire `.aws-sam/build/` directory** with only that function's artifact. The subsequent `sam deploy` then deployed the stale/empty `UploadIndexFunction` package (without `pandas`) because SAM's content hash matched the previously cached version.

The sequence that caused it:
1. `sam build --no-cached` → all functions built correctly including `pandas`
2. `sam build GenerateFunction` → **wiped** `.aws-sam/build/` and rebuilt only `GenerateFunction`
3. `sam deploy` → deployed the now-missing `UploadIndexFunction` artifact

**Fix:** Always run a full `sam build --no-cached` immediately before `sam deploy`. Never run `sam build <SingleFunction>` before a deploy unless you intend to deploy only that function:
```bash
sam build --no-cached && sam deploy
```

**Lesson:** `sam build <FunctionName>` does not append to the build directory — it rebuilds the entire `.aws-sam/build/` with only the specified function. If you need to rebuild a single function quickly, use `sam sync` instead, or always follow a partial build with a full `sam build --no-cached` before deploying. Treat the `.aws-sam/build/` directory as a complete snapshot that must contain all functions before deploying.

---

## 33. GenerateFunction — Duplicate Access-Control-Allow-Origin CORS header

**Error**
```
Access to fetch at 'https://yvpqwr3cipxjlv2wmwlms322jq0bmzqh.lambda-url.us-east-2.on.aws/'
from origin 'https://d3afwft08rzc7q.cloudfront.net' has been blocked by CORS policy:
The 'Access-Control-Allow-Origin' header contains multiple values
'*, https://d3afwft08rzc7q.cloudfront.net', but only one is allowed.
```

**Symptom:** Generate step failed in the browser with a CORS error even though the Lambda Function URL had `AllowOrigins: ["*"]` configured.

**Root cause:** The CORS header was being set twice:
1. Lambda Function URL CORS config in `template.yaml` adds `Access-Control-Allow-Origin: *`
2. `lambda_handler` in `handler.py` also manually returned `"Access-Control-Allow-Origin": "*"` in the response headers

CloudFront (or the browser) received both values concatenated as `*, https://d3afwft08rzc7q.cloudfront.net`, which is invalid — browsers require exactly one value.

**Fix:** Removed the manual `Access-Control-Allow-Origin` header from `handler.py`, letting the Function URL CORS config be the sole source:
```python
# Before
"headers": {
    "Content-Type": "text/event-stream",
    "Access-Control-Allow-Origin": "*",
},
# After
"headers": {
    "Content-Type": "text/event-stream",
},
```

**Lesson:** Never set CORS headers in both the Lambda Function URL config and the response handler — pick one. For Lambda Function URLs, prefer the `FunctionUrlConfig.Cors` block in `template.yaml` as the single source of truth. Setting it in both places guarantees a duplicate header, which browsers reject.

---

## 34. GenerateFunction — fetchEventSource reconnects after stream ends, causing 400 loop

**Error**
```
Failed to load resource: the server responded with a status of 400 (Bad Request)
```
In the browser network tab, after a successful generate, an immediate second POST to the Lambda Function URL returns:
```json
{"statusCode": 400, "headers": {"Content-Type": "text/event-stream"}, "body": "data: [ERROR]{\"type\": \"error\", \"message\": \"Missing required field: session_id\"}\n\n"}
```

**Symptom:** The generated text appeared correctly in the UI, but a 400 error was shown immediately after generation completed. CloudWatch logs confirmed the first request succeeded (`GENERATING → DONE`), and subsequent requests returned 400 within 1–2ms.

**Root cause:** `@microsoft/fetch-event-source` is designed for persistent, auto-reconnecting SSE streams. When the Lambda closed the HTTP connection after sending `[DONE]`, `fetchEventSource` automatically issued a new POST request to reconnect. This second request arrived at the Lambda with the same `session_id`, but the session was now in `DONE` status (not `RANKED`), so the Lambda correctly rejected it with 400.

The `[DONE]` handler in `onmessage` only called `setIsGenerating(false)` but did not abort the controller, so `fetchEventSource` continued its reconnect loop.

**Fix:** Call `controller.abort()` when `[DONE]` is received to stop `fetchEventSource` from reconnecting:
```typescript
// Before
} else if (data === '[DONE]') {
  setIsGenerating(false);
}

// After
} else if (data === '[DONE]') {
  setIsGenerating(false);
  controller.abort();
}
```

**Lesson:** `fetchEventSource` is not a one-shot HTTP client — it behaves like a persistent SSE connection and will automatically reconnect after the server closes the stream. Any time the stream has a defined end condition (e.g., a `[DONE]` sentinel), always call `controller.abort()` at that point to prevent reconnection. Failing to do so causes a second request that may hit unexpected state (in this case, a session that has already moved past `RANKED`).
