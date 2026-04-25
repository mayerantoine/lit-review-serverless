"""
Lambda 3 — Generate (AgentCore buffered wrapper)

Triggered by: Lambda Function URL with InvokeMode=BUFFERED
Timeout: 900s  Memory: 512MB

Flow:
  1. Validate JSON input (session_id, research_idea, selected_paper_ids)
  2. Load session from DynamoDB, assert status=RANKED
  3. Get ranked_papers_s3_key from session
  4. Set status=GENERATING
  5. Invoke AgentCore generate agent, collect all SSE chunks
  6. Parse chunks into text tokens and [METADATA] references
  7. Return {"text": "...", "references": [...]}
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
AGENTCORE_ARN = os.environ["AGENTCORE_GENERATE_ARN"]


def _error_response(status_code: int, message: str) -> dict:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"error": message}),
    }


def lambda_handler(event: dict, context: Any) -> dict:
    # --- Parse and validate JSON body ---
    try:
        body = event.get("body") or "{}"
        if event.get("isBase64Encoded") and isinstance(body, str):
            import base64
            body = base64.b64decode(body).decode("utf-8")
        if isinstance(body, str):
            body = json.loads(body)
    except json.JSONDecodeError:
        return _error_response(400, "Request body must be valid JSON")

    session_id = body.get("session_id", "").strip()
    research_idea = body.get("research_idea", "").strip()
    selected_paper_ids = body.get("selected_paper_ids", [])

    if not session_id:
        return _error_response(400, "Missing required field: session_id")
    if not research_idea:
        return _error_response(400, "Missing required field: research_idea")
    if not isinstance(selected_paper_ids, list):
        return _error_response(400, "selected_paper_ids must be a list")

    # --- Load session, assert status=RANKED ---
    session_store = DynamoDBSessionStore()
    try:
        session = session_store.get_session(session_id)
    except SessionNotFoundError:
        return _error_response(404, f"Session not found: {session_id}")

    current_status = session.get("status")
    if current_status != SessionStatus.RANKED:
        return _error_response(
            400,
            f"Session is not ready for generation (status={current_status}). "
            "Run retrieve-and-rank first.",
        )

    ranked_papers_s3_key = session.get("ranked_papers_s3_key")
    if not ranked_papers_s3_key:
        return _error_response(500, "Session has no ranked_papers_s3_key — ranking may have failed.")

    # --- Set status=GENERATING ---
    session_store.update_status(session_id, SessionStatus.GENERATING)

    # --- Invoke AgentCore, collect all chunks ---
    agent_payload = {
        "session_id": session_id,
        "research_idea": research_idea,
        "selected_paper_ids": selected_paper_ids,
        "ranked_papers_s3_key": ranked_papers_s3_key,
        "s3_data_bucket": session.get("s3_data_bucket"),
    }

    try:
        text, references = _collect_from_agentcore(agent_payload)
    except Exception as e:
        logger.exception("Generation failed")
        session_store.update_status(session_id, SessionStatus.ERROR, str(e))
        return _error_response(500, f"Generation failed: {e}")

    response_body = {"text": text, "references": references}
    logger.info("LAMBDA OUTPUT: text_length=%d references_count=%d", len(text), len(references))
    logger.info("LAMBDA OUTPUT BODY: %s", json.dumps(response_body))

    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(response_body),
    }


def _collect_from_agentcore(payload: dict) -> tuple[str, list]:
    """
    Invoke AgentCore generate agent, collect all SSE chunks, and return
    (text, references) where text is the full generated review and references
    is the list from the [METADATA] chunk.
    """
    client = boto3.client("bedrock-agentcore", region_name=REGION)
    session_id = payload["session_id"]
    runtime_session_id = f"session-{session_id}-{'x' * 25}"

    response = client.invoke_agent_runtime(
        agentRuntimeArn=AGENTCORE_ARN,
        runtimeSessionId=runtime_session_id,
        payload=json.dumps(payload).encode("utf-8"),
        contentType="application/json",
        accept="text/event-stream",
    )

    text_tokens = []
    references = []

    if "response" in response:
        chunks = response["response"].iter_chunks(chunk_size=1024)
    else:
        chunks = (
            event.get("chunk", {}).get("bytes", b"")
            for event in response.get("outputStream", [])
        )

    for chunk_bytes in chunks:
        if not chunk_bytes:
            continue
        logger.info("AGENT RAW CHUNK: %s", chunk_bytes.decode("utf-8", errors="replace"))
        for content in _unwrap_agentcore_chunk(chunk_bytes):
            logger.info("AGENT CONTENT TOKEN: %r", content)
            if content.startswith("[METADATA]"):
                try:
                    meta = json.loads(content[len("[METADATA]"):])
                    references = meta.get("references", [])
                except json.JSONDecodeError:
                    pass
            elif content == "[DONE]":
                break
            elif content.startswith("[ERROR]"):
                try:
                    err = json.loads(content[len("[ERROR]"):])
                    raise RuntimeError(err.get("message", "Agent returned error"))
                except json.JSONDecodeError:
                    raise RuntimeError("Agent returned error")
            else:
                text_tokens.append(content)

    return "".join(text_tokens), references


def _unwrap_agentcore_chunk(chunk_bytes: bytes):
    """
    Unwrap AgentCore's double-SSE envelope and yield clean content strings.

    AgentCore wraps the agent's SSE output in an outer SSE envelope:
      outer:  data: "<JSON-encoded inner SSE line>"\n\n
      inner:  data: <agent-text>\n\n
    """
    chunk_text = chunk_bytes.decode("utf-8")
    for outer_line in chunk_text.splitlines():
        outer_line = outer_line.strip()
        if not outer_line.startswith("data:"):
            continue
        raw_value = outer_line[len("data:"):].strip()
        try:
            inner_text = json.loads(raw_value)
        except (json.JSONDecodeError, ValueError):
            inner_text = raw_value
        if not (isinstance(inner_text, str) and inner_text.startswith("data:")):
            continue
        content = inner_text[len("data:"):]
        if content.startswith(" "):
            content = content[1:]  # strip exactly the one SSE separator space
        content = content.rstrip("\n")
        content = content.replace("\\n", "\n")
        yield content
