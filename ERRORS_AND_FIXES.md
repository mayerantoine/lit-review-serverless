  File: ERRORS_AND_FIXES                                                                    
                                                                                                     
    # Deployment Errors & Fixes                                                                        
                                                                                                     
    ## AWS CLI / AgentCore                                                                           

    ### 1. Wrong AWS CLI service name
    - **Error:** `argument operation: Found invalid choice 'create-agent-runtime'`
    - **Fix:** Use `aws bedrock-agentcore-control create-agent-runtime` (control plane), not `aws
    bedrock-agentcore create-agent-runtime` (data plane)

    ### 2. Wrong parameter: `--execution-role-arn`
    - **Error:** `the following arguments are required: --role-arn`
    - **Fix:** Replace `--execution-role-arn` with `--role-arn`

    ### 3. Wrong artifact format: `containerImage={uri=...}`
    - **Error:** ValidationException on `--agent-runtime-artifact`
    - **Fix:** Use `containerConfiguration={containerUri=...}` (shorthand syntax confirmed via `help`)

    ### 4. Agent runtime name contains hyphens
    - **Error:** `Value 'lit-review-retrieve-rank' failed to satisfy constraint: pattern
    [a-zA-Z][a-zA-Z0-9_]{0,47}`
    - **Fix:** Rename to `lit_review_retrieve_rank` (underscores only)

    ### 5. Missing ECR permissions on AgentCoreExecutionRole
    - **Error:** `Access denied while validating ECR URI`
    - **Fix:** Add to role policy: `ecr:GetAuthorizationToken`, `ecr:BatchGetImage`,
    `ecr:GetDownloadUrlForLayer` with `Resource: "*"`

    ### 6. Wrong `--parameter-overrides` format for ARNs containing `=`
    - **Error:** `AGENTCORE_RETRIEVE_RANK_ARN=arn:...` is not a valid format
    - **Fix:** Use `ParameterKey=AgentCoreRetrieveRankArn,ParameterValue=arn:...` format

    ---

    ## AWS SAM

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
    - **Fix:** Add `Conditions` block and use `!If [HasRetrieveRankArn, !Ref AgentCoreRetrieveRankArn,
    "*"]`

    ### 11. Stack stuck in UPDATE_ROLLBACK_FAILED
    - **Error:** Cannot update a stack in ROLLBACK_FAILED state
    - **Fix:** `aws cloudformation continue-update-rollback --stack-name lit-review-serverless`

    ### 12. Lambda Layer too large (347MB > 262MB limit)
    - **Error:** Function code combined with layers exceeds 262144000 bytes
    - **Fix:** Switch shared layer to `BuildMethod: makefile` with a `Makefile` that only copies `.py`
    files (no pip install into layer). Move heavy deps into each Lambda's own `requirements.txt`

    ---

    ## Lambda Runtime Errors

    ### 13. `No module named 's3_storage'`
    - **Cause:** Lambda Layer `BuildMethod: python3.12` with `requirements.txt` installed packages but
    didn't place `.py` source files under `python/` correctly
    - **Fix:** Use `BuildMethod: makefile` with `Makefile` that copies `*.py` to
    `$(ARTIFACTS_DIR)/python/`

    ### 14. `No module named 'agents'` (openai-agents)
    - **Cause:** `pipeline.py` had `from agents import Agent, Runner` at module level; `upload_index`
    Lambda doesn't have `openai-agents` installed
    - **Fix:** Move import inside the functions that need it (`create_relevance_agent`,
    `score_single_paper`) as lazy imports

    ### 15. API Gateway "Service Unavailable" with no CloudWatch logs
    - **Cause:** API Gateway HTTP API has a hard 30s timeout; `upload-and-index` (indexing) takes
    several minutes
    - **Fix:** Split into async pattern — fast handler returns 202 + `session_id`, fires
    `BuildIndexFunction` async (`InvocationType=Event`); frontend polls `/api/session/{id}/status`

    ### 16. `BuildIndexFunction` AccessDenied on S3 CSV bucket
    - **Error:** `s3:GetObject` not allowed on `lit-review-upload` bucket
    - **Fix:** Add `S3CrudPolicy: BucketName: !Ref S3CsvBucket` to `BuildIndexFunction` policies

    ---

    ## AgentCore Runtime Invocation

    ### 17. `acceptContentType` is not a valid parameter
    - **Error:** `Parameter validation failed: Unknown parameter in input: "acceptContentType"`
    - **Fix:** Replace `acceptContentType=` with `accept=`

    ### 18. `runtimeSessionId` too short (min 33 chars)
    - **Error:** `Invalid length for parameter runtimeSessionId, value: 8, valid min length: 33`
    - **Fix:** Pad: `runtime_session_id = f"session-{session_id}-{'x' * 25}"`

    ### 19. IAM `bedrock-agentcore:InvokeAgentRuntime` denied on runtime endpoint
    - **Error:** Not authorized on
    `arn:.../runtime/lit_review_retrieve_rank-.../runtime-endpoint/DEFAULT`
    - **Fix:** Allow both base ARN and wildcard: add both `!Ref AgentCoreRetrieveRankArn` and `!Sub
    "${AgentCoreRetrieveRankArn}/*"` as Resources

    ### 20. AgentCore response key is `response`, not `outputStream`
    - **Error:** `KeyError: 'outputStream'`
    - **Fix:** Check for `response` key first (a `StreamingBody`), fall back to `outputStream`

    ### 21. AgentCore response body is SSE format, not plain JSON
    - **Cause:** Response `contentType: text/event-stream`; body is `data: "...\n\n"`
    - **Fix:** Parse SSE lines: strip `data: ` prefix, then `json.loads()` the value; handle
    double-encoding (value is a JSON string containing JSON)

    ### 22. Agent container missing `nest_asyncio`
    - **Error:** `No module named 'nest_asyncio'` inside agent
    - **Fix:** Add `nest_asyncio` to `agents/retrieve_rank_agent/requirements.txt`

    ### 23. Dockerfile copied wrong `requirements.txt`
    - **Cause:** `COPY requirements.txt .` picked up the root dev `requirements.txt` instead of the
    agent's
    - **Fix:** Change to `COPY agents/retrieve_rank_agent/requirements.txt .` (same for generate agent)

    ### 24. AgentCore caches old container image despite `latest` tag update
    - **Cause:** AgentCore Runtime doesn't re-pull `latest` on update
    - **Fix:** Push with a new explicit tag (`:v2`, `:v3`, etc.) and update the runtime to point to the
     new tag

    ### 25. Retrieve-and-rank also hits 30s API Gateway timeout
    - **Cause:** AgentCore agent takes ~60s to score papers; API Gateway times out
    - **Fix:** Same async pattern as upload — fast handler returns 202, fires `RankWorkerFunction`
    async, frontend polls status until `RANKED`

    ---

    ## Environment Variables

    ### 26. AgentCore container crashes on startup: `KeyError: 'BEDROCK_KNOWLEDGE_S3_DATA'`
    - **Cause:** Environment variables not set on the AgentCore Runtime (only set in SAM for Lambdas)
    - **Fix:** `aws bedrock-agentcore-control update-agent-runtime --environment-variables
    "BEDROCK_KNOWLEDGE_S3_DATA=...,S3_VECTOR=...,DYNAMODB_TABLE=...,OPENAI_API_KEY=...,DEFAULT_AWS_REGI
    ON=us-east-2"`

    Save this to ERRORS_AND_FIXES.md in the repo root. Since I can't run tools, ask the main agent to
    write it or run:

    # from lit-review-serverless/
    cat > ERRORS_AND_FIXES.md << 'EOF'
    <paste content above>
    EOF