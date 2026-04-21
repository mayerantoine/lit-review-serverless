"""
Lambda 2 — Retrieve & Rank (AgentCore wrapper)

Triggered by: API Gateway POST /api/retrieve-and-rank (JSON body)
Timeout: 300s  Memory: 512MB

Flow:
  1. Validate JSON input (session_id, research_idea, hybrid_k)
  2. Load session from DynamoDB, assert status=INDEXED
  3. Set status=RANKING
  4. Invoke AgentCore retrieve-rank agent (synchronous, collect full response)
  5. Parse JSON result from agent
  6. Return ranked papers JSON to frontend
"""

import json
import os
import logging
from typing import Any

import boto3
from botocore.exceptions import ClientError

from dynamodb_session import (
    DynamoDBSessionStore,
    SessionStatus,
    SessionNotFoundError,
    SessionStateError,
)

logger = logging.getLogger()
logger.setLevel(logging.INFO)

REGION = os.environ.get("DEFAULT_AWS_REGION", "us-east-2")
AGENTCORE_ARN = os.environ["AGENTCORE_RETRIEVE_RANK_ARN"]


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------

def _ok(data: Any) -> dict:
    return {
        "statusCode": 200,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
        },
        "body": json.dumps(data),
    }


def _error(message: str, status: int = 400) -> dict:
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
        },
        "body": json.dumps({"error": message}),
    }


# ---------------------------------------------------------------------------
# AgentCore invocation
# ---------------------------------------------------------------------------

def _invoke_agentcore(session_id: str, payload: dict) -> dict:
    """
    Invoke the retrieve-rank AgentCore Runtime agent and collect the full response.

    AgentCore returns a streaming outputStream — we collect all chunks
    and parse the final JSON result.
    """
    client = boto3.client("bedrock-agentcore", region_name=REGION)

    response = client.invoke_agent_runtime(
        agentRuntimeArn=AGENTCORE_ARN,
        runtimeSessionId=session_id,
        payload=json.dumps(payload).encode("utf-8"),
        contentType="application/json",
        acceptContentType="application/json",
    )

    # Collect all chunks from the streaming outputStream
    result_bytes = b""
    for event in response["outputStream"]:
        chunk = event.get("chunk", {}).get("bytes", b"")
        if chunk:
            result_bytes += chunk

    if not result_bytes:
        raise RuntimeError("AgentCore returned an empty response")

    return json.loads(result_bytes.decode("utf-8"))


# ---------------------------------------------------------------------------
# Lambda handler
# ---------------------------------------------------------------------------

def lambda_handler(event: dict, context: Any) -> dict:
    logger.info("retrieve-and-rank invoked")

    session_store = DynamoDBSessionStore()

    # --- Step 1: Parse and validate JSON body ---
    try:
        body = event.get("body") or "{}"
        if isinstance(body, str):
            body = json.loads(body)
    except json.JSONDecodeError:
        return _error("Request body must be valid JSON", 400)

    session_id = body.get("session_id", "").strip()
    research_idea = body.get("research_idea", "").strip()
    hybrid_k = body.get("hybrid_k", 50)

    if not session_id:
        return _error("Missing required field: session_id", 400)
    if not research_idea:
        return _error("Missing required field: research_idea", 400)
    if not isinstance(hybrid_k, int) or hybrid_k < 1:
        return _error("hybrid_k must be a positive integer", 400)

    # --- Step 2: Load session, assert status=INDEXED ---
    try:
        session = session_store.get_session(session_id)
    except SessionNotFoundError:
        return _error(f"Session not found: {session_id}", 404)

    current_status = session.get("status")
    if current_status != SessionStatus.INDEXED:
        return _error(
            f"Session is not ready for ranking (status={current_status}). "
            "Upload and index a CSV first.",
            400,
        )

    # --- Step 3: Set status=RANKING ---
    session_store.update_status(session_id, SessionStatus.RANKING)

    # --- Step 4: Invoke AgentCore retrieve-rank agent ---
    agent_payload = {
        "session_id": session_id,
        "research_idea": research_idea,
        "hybrid_k": hybrid_k,
        # Pass session metadata so agent can reconnect to S3 Vectors index
        "index_name": session.get("index_name"),
        "s3_vector_bucket": session.get("s3_vector_bucket"),
        "s3_data_bucket": session.get("s3_data_bucket"),
    }

    try:
        result = _invoke_agentcore(session_id, agent_payload)
    except ClientError as e:
        error_code = e.response["Error"]["Code"]
        logger.exception("AgentCore ClientError: %s", error_code)
        session_store.update_status(session_id, SessionStatus.ERROR, str(e))
        return _error(f"Failed to invoke ranking agent: {error_code}", 502)
    except Exception as e:
        logger.exception("AgentCore invocation failed")
        session_store.update_status(session_id, SessionStatus.ERROR, str(e))
        return _error(f"Ranking agent error: {e}", 502)

    # --- Step 5: Validate agent result ---
    if result.get("status") != "success":
        error_msg = result.get("error", "Unknown agent error")
        session_store.update_status(session_id, SessionStatus.ERROR, error_msg)
        return _error(f"Ranking failed: {error_msg}", 500)

    # --- Step 6: Return ranked papers to frontend ---
    # DynamoDB is updated to RANKED by the agent itself (it has the S3 key)
    return _ok({
        "session_id": session_id,
        "status": SessionStatus.RANKED,
        "data": result.get("data", {}),
    })
