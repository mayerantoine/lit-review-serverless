"""
Lambda 3 — Generate (AgentCore streaming wrapper)

Triggered by: Lambda Function URL with InvokeMode=RESPONSE_STREAM
Timeout: 900s  Memory: 512MB

Flow:
  1. Validate JSON input (session_id, research_idea, selected_paper_ids)
  2. Load session from DynamoDB, assert status=RANKED
  3. Get ranked_papers_s3_key from session
  4. Set status=GENERATING
  5. Invoke AgentCore generate agent (streaming)
  6. Forward each SSE chunk directly to the Lambda response stream
  7. Set status=DONE when [DONE] chunk received
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
AGENTCORE_ARN = os.environ["AGENTCORE_GENERATE_ARN"]


# ---------------------------------------------------------------------------
# SSE helpers
# ---------------------------------------------------------------------------

def _sse(data: str) -> bytes:
    """Format a string as an SSE data line."""
    return f"data: {data}\n\n".encode("utf-8")


def _sse_error(message: str) -> bytes:
    return _sse(f"[ERROR]{json.dumps({'type': 'error', 'message': message})}")


# ---------------------------------------------------------------------------
# Lambda streaming handler
#
# Lambda Function URL with InvokeMode=RESPONSE_STREAM passes a
# `responseStream` object as the third positional argument when the handler
# is wrapped with awslambdaric's `lambda_handler_streaming` decorator.
# Without awslambdaric the runtime calls handler(event, context) and collects
# the return value — streaming is not available in that path.
#
# For local SAM testing (non-streaming), the handler falls back to returning
# a standard API Gateway-style response dict.
# ---------------------------------------------------------------------------

def lambda_handler(event: dict, context: Any) -> dict:
    """
    Standard entry point — used by SAM local and as the SAM template Handler value.

    For production streaming, Lambda runtime calls this via the streaming
    wrapper registered below. The response_stream path is handled by
    _streaming_handler(); this fallback is for local/non-streaming invocations.
    """
    logger.info("generate invoked (non-streaming fallback)")
    result = _core_logic(event)
    # Collect generator output into a single string for non-streaming response
    chunks = list(result["stream"])
    body = "".join(c.decode("utf-8") if isinstance(c, bytes) else c for c in chunks)
    return {
        "statusCode": result["statusCode"],
        "headers": {
            "Content-Type": "text/event-stream",
            "Access-Control-Allow-Origin": "*",
        },
        "body": body,
    }


def _core_logic(event: dict) -> dict:
    """
    Parse input, validate session, invoke AgentCore, return a dict with:
      - statusCode (int)
      - stream (generator of bytes) — SSE chunks to forward to client
    """
    session_store = DynamoDBSessionStore()

    # --- Step 1: Parse and validate JSON body ---
    try:
        body = event.get("body") or "{}"
        if isinstance(body, str):
            body = json.loads(body)
    except json.JSONDecodeError:
        return {"statusCode": 400, "stream": iter([_sse_error("Request body must be valid JSON")])}

    session_id = body.get("session_id", "").strip()
    research_idea = body.get("research_idea", "").strip()
    selected_paper_ids = body.get("selected_paper_ids", [])

    if not session_id:
        return {"statusCode": 400, "stream": iter([_sse_error("Missing required field: session_id")])}
    if not research_idea:
        return {"statusCode": 400, "stream": iter([_sse_error("Missing required field: research_idea")])}
    if not isinstance(selected_paper_ids, list):
        return {"statusCode": 400, "stream": iter([_sse_error("selected_paper_ids must be a list")])}

    # --- Step 2: Load session, assert status=RANKED ---
    try:
        session = session_store.get_session(session_id)
    except SessionNotFoundError:
        return {"statusCode": 404, "stream": iter([_sse_error(f"Session not found: {session_id}")])}

    current_status = session.get("status")
    if current_status != SessionStatus.RANKED:
        return {
            "statusCode": 400,
            "stream": iter([_sse_error(
                f"Session is not ready for generation (status={current_status}). "
                "Run retrieve-and-rank first."
            )]),
        }

    # --- Step 3: Get ranked_papers_s3_key ---
    ranked_papers_s3_key = session.get("ranked_papers_s3_key")
    if not ranked_papers_s3_key:
        return {
            "statusCode": 500,
            "stream": iter([_sse_error("Session has no ranked_papers_s3_key — ranking may have failed.")]),
        }

    # --- Step 4: Set status=GENERATING ---
    session_store.update_status(session_id, SessionStatus.GENERATING)

    # --- Step 5 + 6 + 7: Invoke AgentCore and stream back chunks ---
    agent_payload = {
        "session_id": session_id,
        "research_idea": research_idea,
        "selected_paper_ids": selected_paper_ids,
        "ranked_papers_s3_key": ranked_papers_s3_key,
        "s3_data_bucket": session.get("s3_data_bucket"),
    }

    return {
        "statusCode": 200,
        "stream": _stream_from_agentcore(session_id, agent_payload, session_store),
    }


def _stream_from_agentcore(session_id: str, payload: dict, session_store: DynamoDBSessionStore):
    """
    Generator: invokes AgentCore generate agent and yields SSE bytes chunk by chunk.
    Sets DynamoDB status=DONE when [DONE] received, ERROR on failure.
    """
    client = boto3.client("bedrock-agentcore", region_name=REGION)

    try:
        response = client.invoke_agent_runtime(
            agentRuntimeArn=AGENTCORE_ARN,
            runtimeSessionId=session_id,
            payload=json.dumps(payload).encode("utf-8"),
            contentType="application/json",
            acceptContentType="text/event-stream",
        )

        for event in response["outputStream"]:
            chunk_bytes = event.get("chunk", {}).get("bytes", b"")
            if not chunk_bytes:
                continue

            # Decode and forward — agent already formats as SSE "data: ...\n\n"
            chunk_text = chunk_bytes.decode("utf-8")
            yield chunk_bytes

            # Watch for [DONE] sentinel to update status
            if "[DONE]" in chunk_text:
                session_store.update_status(session_id, SessionStatus.DONE)

    except ClientError as e:
        error_code = e.response["Error"]["Code"]
        logger.exception("AgentCore ClientError: %s", error_code)
        session_store.update_status(session_id, SessionStatus.ERROR, str(e))
        yield _sse_error(f"Generation agent invocation failed: {error_code}")

    except Exception as e:
        logger.exception("Unexpected error during generation streaming")
        session_store.update_status(session_id, SessionStatus.ERROR, str(e))
        yield _sse_error(f"Generation failed: {e}")


# ---------------------------------------------------------------------------
# Streaming entry point (awslambdaric — production only)
#
# SAM template sets Handler: handler.lambda_handler for local testing.
# For production streaming, configure the Function URL and deploy with
# awslambdaric installed; the runtime will call this wrapper directly.
# ---------------------------------------------------------------------------

try:
    from awslambdaric.lambda_context import LambdaContext  # noqa: F401
    from awslambdaric import bootstrap

    @bootstrap.lambda_handler_streaming
    def streaming_handler(event: dict, context: Any, response_stream):
        """Production streaming handler — called by Lambda runtime via Function URL."""
        logger.info("generate invoked (streaming)")
        result = _core_logic(event)

        response_stream.set_response_headers(
            status_code=result["statusCode"],
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Access-Control-Allow-Origin": "*",
            },
        )

        for chunk in result["stream"]:
            response_stream.write(chunk if isinstance(chunk, bytes) else chunk.encode("utf-8"))

        response_stream.close()

except ImportError:
    # awslambdaric not installed (local dev / SAM local) — streaming_handler not available
    pass
