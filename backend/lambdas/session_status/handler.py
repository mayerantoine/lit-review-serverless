import json
import logging
from typing import Any

import boto3

from dynamodb_session import DynamoDBSessionStore, SessionNotFoundError, load_ranked_papers_from_s3

logger = logging.getLogger()
logger.setLevel(logging.INFO)


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


def lambda_handler(event: dict, context: Any) -> dict:
    session_id = (event.get("pathParameters") or {}).get("session_id")
    if not session_id:
        return _error("Missing session_id", 400)

    store = DynamoDBSessionStore()
    try:
        session = store.get_session(session_id)
    except SessionNotFoundError:
        return _error(f"Session {session_id} not found", 404)

    return _ok({
        "session_id": session_id,
        "status": session.get("status"),
        "filename": session.get("filename"),
        "total_abstracts": session.get("total_abstracts"),
        "chunks_created": session.get("chunks_created"),
        "index_name": session.get("index_name"),
        "error_message": session.get("error_message"),
    })


def ranked_papers_handler(event: dict, context: Any) -> dict:
    """GET /api/session/{session_id}/ranked-papers — load ranked papers from S3."""
    session_id = (event.get("pathParameters") or {}).get("session_id")
    if not session_id:
        return _error("Missing session_id", 400)

    store = DynamoDBSessionStore()
    try:
        session = store.get_session(session_id)
    except SessionNotFoundError:
        return _error(f"Session {session_id} not found", 404)

    if session.get("status") != "RANKED":
        return _error(f"Session is not ranked yet (status={session.get('status')})", 400)

    s3_key = session.get("ranked_papers_s3_key")
    s3_bucket = session.get("s3_data_bucket")
    if not s3_key or not s3_bucket:
        return _error("Ranked papers not found in session", 404)

    ranked_data = load_ranked_papers_from_s3(s3_bucket, s3_key)
    return _ok(ranked_data)
