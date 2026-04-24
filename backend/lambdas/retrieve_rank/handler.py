"""
Lambda 2 — Retrieve & Rank (AgentCore wrapper)

Triggered by: API Gateway POST /api/retrieve-and-rank (JSON body)
Timeout: 29s for API handler, 300s for async worker

Flow (async):
  lambda_handler (fast, <30s):
    1. Validate input, assert session=INDEXED
    2. Set status=RANKING
    3. Async-invoke RankWorkerFunction (InvocationType=Event)
    4. Return 202 immediately — frontend polls /api/session/{id}/status

  rank_worker_handler (async, 300s):
    1. Invoke AgentCore retrieve-rank agent
    2. Parse SSE response
    3. DynamoDB updated to RANKED by the agent itself
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
)

logger = logging.getLogger()
logger.setLevel(logging.INFO)

REGION = os.environ.get("DEFAULT_AWS_REGION", "us-east-2")
AGENTCORE_ARN = os.environ["AGENTCORE_RETRIEVE_RANK_ARN"]
RANK_WORKER_FUNCTION = os.environ.get("RANK_WORKER_FUNCTION", "lit-review-rank-worker")


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------

def _ok(data: Any, status: int = 200) -> dict:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"},
        "body": json.dumps(data),
    }


def _error(message: str, status: int = 400) -> dict:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"},
        "body": json.dumps({"error": message}),
    }


# ---------------------------------------------------------------------------
# API Gateway handler (fast, <30s)
# ---------------------------------------------------------------------------

def lambda_handler(event: dict, context: Any) -> dict:
    logger.info("retrieve-and-rank invoked")
    session_store = DynamoDBSessionStore()

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

    session_store.update_status(session_id, SessionStatus.RANKING)

    # Async invoke the worker
    boto3.client("lambda").invoke(
        FunctionName=RANK_WORKER_FUNCTION,
        InvocationType="Event",
        Payload=json.dumps({
            "session_id": session_id,
            "research_idea": research_idea,
            "hybrid_k": hybrid_k,
            "index_name": session.get("index_name"),
            "s3_vector_bucket": session.get("s3_vector_bucket"),
            "s3_data_bucket": session.get("s3_data_bucket"),
        }).encode(),
    )
    logger.info("RankWorkerFunction invoked async for session_id=%s", session_id)

    return _ok({
        "session_id": session_id,
        "status": SessionStatus.RANKING,
        "message": "Ranking started. Poll /api/session/{session_id}/status for completion.",
    }, 202)


# ---------------------------------------------------------------------------
# Async worker handler (300s timeout, no API GW)
# ---------------------------------------------------------------------------

def rank_worker_handler(event: dict, context: Any) -> None:
    session_id = event["session_id"]
    session_store = DynamoDBSessionStore()
    logger.info("rank_worker_handler started for session_id=%s", session_id)

    try:
        result = _invoke_agentcore(session_id, event)
        if result.get("status") != "success":
            error_msg = result.get("error", "Unknown agent error")
            session_store.update_status(session_id, SessionStatus.ERROR, error_msg)
            logger.error("Agent returned error for session %s: %s", session_id, error_msg)
        else:
            logger.info("Ranking complete for session_id=%s", session_id)
            # DynamoDB already updated to RANKED by the agent
    except Exception as e:
        logger.exception("rank_worker_handler failed for session_id=%s", session_id)
        session_store.update_status(session_id, SessionStatus.ERROR, str(e))


# ---------------------------------------------------------------------------
# AgentCore invocation
# ---------------------------------------------------------------------------

def _invoke_agentcore(session_id: str, payload: dict) -> dict:
    client = boto3.client("bedrock-agentcore", region_name=REGION)
    runtime_session_id = f"session-{session_id}-{'x' * 25}"

    response = client.invoke_agent_runtime(
        agentRuntimeArn=AGENTCORE_ARN,
        runtimeSessionId=runtime_session_id,
        payload=json.dumps(payload).encode("utf-8"),
        contentType="application/json",
        accept="application/json",
    )

    logger.info("AgentCore response keys: %s", list(response.keys()))
    logger.info("AgentCore statusCode: %s contentType: %s", response.get("statusCode"), response.get("contentType"))

    result_bytes = b""
    if "response" in response:
        result_bytes = response["response"].read()
    elif "outputStream" in response:
        for event in response["outputStream"]:
            chunk = event.get("chunk", {}).get("bytes", b"")
            if chunk:
                result_bytes += chunk
    else:
        raise RuntimeError(f"Unexpected response shape. Keys: {list(response.keys())}")

    logger.info("AgentCore raw response (%d bytes): %s", len(result_bytes), result_bytes[:500])

    if not result_bytes:
        raise RuntimeError("AgentCore returned an empty response")

    body_text = result_bytes.decode("utf-8")
    json_parts = []
    for line in body_text.splitlines():
        if line.startswith("data: "):
            json_parts.append(line[6:])
    combined = "".join(json_parts)

    parsed = json.loads(combined)
    if isinstance(parsed, str):
        parsed = json.loads(parsed)
    return parsed
