# ARM64 container for AWS AgentCore Runtime
FROM python:3.12-slim

WORKDIR /app

# Install dependencies first (layer caching)
COPY agents/retrieve_rank_agent/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy shared modules (pipeline, vectorstore, s3_storage, config, dynamodb_session)
COPY shared/ .

# Copy agent entrypoint
COPY agents/retrieve_rank_agent/agent.py .

# AgentCore Runtime calls /invocations (POST) and /ping (GET)
# bedrock-agentcore SDK starts the HTTP server on port 8080
EXPOSE 8080

CMD ["python", "agent.py"]
