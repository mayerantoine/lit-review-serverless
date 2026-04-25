"""
Lambda 3 — Generate (direct pipeline call, no AgentCore)

Triggered by: Lambda Function URL with InvokeMode=BUFFERED
Timeout: 900s  Memory: 512MB

Flow:
  1. Validate JSON input (session_id, research_idea, selected_paper_ids)
  2. Load session from DynamoDB, assert status=RANKED
  3. Load ranked papers from S3 (top_k_papers / all_scored_papers)
  4. Filter to selected_paper_ids (or fall back to top_k)
  5. Call generate_related_work_text(stream=False) directly
  6. Build references list from cited paper IDs
  7. Set status=DONE, return {text, references}
"""

import json
import os
import logging
from typing import Any

import boto3
import pandas as pd

from pipeline import generate_related_work_text
from dynamodb_session import (
    DynamoDBSessionStore,
    SessionStatus,
    SessionNotFoundError,
    load_ranked_papers_from_s3,
)

logger = logging.getLogger()
logger.setLevel(logging.INFO)

GENERATION_MODEL = os.environ.get("GENERATION_MODEL", "gpt-4o-mini")


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

    s3_data_bucket = session.get("s3_data_bucket")

    # --- Set status=GENERATING ---
    session_store.update_status(session_id, SessionStatus.GENERATING)

    try:
        # --- Load ranked papers from S3 ---
        ranked_data = load_ranked_papers_from_s3(
            s3_bucket=s3_data_bucket,
            s3_key=ranked_papers_s3_key,
        )

        top_k_records = ranked_data.get("top_k_papers", [])
        all_scored_records = ranked_data.get("all_scored_papers", [])

        # --- Filter to selected_paper_ids (fall back to top_k) ---
        if selected_paper_ids:
            id_set = set(int(i) for i in selected_paper_ids)
            records = [r for r in all_scored_records if int(r["id"]) in id_set]
            if not records:
                logger.warning(
                    "selected_paper_ids %s matched no papers — falling back to top_k",
                    selected_paper_ids,
                )
                records = top_k_records
        else:
            records = top_k_records

        if not records:
            session_store.update_status(session_id, SessionStatus.ERROR, "No papers available for generation")
            return _error_response(500, "No papers available for generation")

        selected_papers = pd.DataFrame(records)
        logger.info("Generating review for %d papers", len(selected_papers))

        # --- Generate related work text ---
        generated_text, metadata = generate_related_work_text(
            query=research_idea,
            selected_papers=selected_papers,
            generation_model=GENERATION_MODEL,
            stream=False,
        )

        # --- Build references from cited paper IDs ---
        references = []
        for paper_id in metadata.cited_paper_ids:
            paper = selected_papers[selected_papers["id"] == paper_id]
            if not paper.empty:
                references.append({
                    "id": int(paper_id),
                    "title": str(paper.iloc[0]["title"]),
                    "abstract": str(paper.iloc[0]["abstract"]),
                })

    except Exception as e:
        logger.exception("Generation failed")
        session_store.update_status(session_id, SessionStatus.ERROR, str(e))
        return _error_response(500, f"Generation failed: {e}")

    session_store.update_status(session_id, SessionStatus.DONE)

    response_body = {"text": generated_text, "references": references}
    logger.info("LAMBDA OUTPUT: text_length=%d references_count=%d", len(generated_text), len(references))
    logger.info("LAMBDA OUTPUT BODY: %s", json.dumps(response_body))

    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(response_body),
    }
