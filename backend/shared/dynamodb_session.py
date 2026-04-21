"""
DynamoDB session store for LitReview serverless pipeline.

Table: LitReviewSessions
PK: session_id (String)
TTL: ttl attribute (auto-deleted after 24h)
"""

import os
import json
import time
import logging
import boto3
from botocore.exceptions import ClientError
from typing import Optional

logger = logging.getLogger(__name__)

TABLE_NAME = os.environ.get("DYNAMODB_TABLE", "LitReviewSessions")
REGION = os.environ.get("DEFAULT_AWS_REGION", "us-east-2")

# Session status values
class SessionStatus:
    INDEXING = "INDEXING"
    INDEXED = "INDEXED"
    RANKING = "RANKING"
    RANKED = "RANKED"
    GENERATING = "GENERATING"
    DONE = "DONE"
    ERROR = "ERROR"


class SessionError(Exception):
    """Base exception for session errors."""


class SessionNotFoundError(SessionError):
    """Raised when session_id does not exist in DynamoDB."""


class SessionStateError(SessionError):
    """Raised when session is in an unexpected state for the requested operation."""


class DynamoDBSessionStore:
    """Wraps all DynamoDB operations for LitReview sessions."""

    def __init__(self):
        self._client = None

    @property
    def client(self):
        if self._client is None:
            self._client = boto3.client("dynamodb", region_name=REGION)
        return self._client

    def create_session(
        self,
        session_id: str,
        filename: str,
        s3_csv_uri: str,
        s3_vector_bucket: str,
        s3_data_bucket: str,
    ) -> None:
        """Create a new session record with status=INDEXING."""
        now = int(time.time())
        ttl = now + 86400  # 24h

        self.client.put_item(
            TableName=TABLE_NAME,
            Item={
                "session_id": {"S": session_id},
                "ttl": {"N": str(ttl)},
                "created_at": {"N": str(now)},
                "status": {"S": SessionStatus.INDEXING},
                "filename": {"S": filename},
                "s3_csv_uri": {"S": s3_csv_uri},
                "s3_vector_bucket": {"S": s3_vector_bucket},
                "s3_data_bucket": {"S": s3_data_bucket},
            },
            ConditionExpression="attribute_not_exists(session_id)",
        )
        logger.info("Created session %s (status=INDEXING)", session_id)

    def update_after_indexing(
        self,
        session_id: str,
        index_name: str,
        total_abstracts: int,
        chunks_created: int,
    ) -> None:
        """Update session after build_index completes; set status=INDEXED."""
        self.client.update_item(
            TableName=TABLE_NAME,
            Key={"session_id": {"S": session_id}},
            UpdateExpression=(
                "SET #st = :st, index_name = :idx, "
                "total_abstracts = :ta, chunks_created = :cc"
            ),
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={
                ":st": {"S": SessionStatus.INDEXED},
                ":idx": {"S": index_name},
                ":ta": {"N": str(total_abstracts)},
                ":cc": {"N": str(chunks_created)},
            },
        )
        logger.info("Session %s updated to INDEXED (abstracts=%d)", session_id, total_abstracts)

    def get_session(self, session_id: str) -> dict:
        """Return the raw DynamoDB item as a flat Python dict. Raises SessionNotFoundError if missing."""
        response = self.client.get_item(
            TableName=TABLE_NAME,
            Key={"session_id": {"S": session_id}},
            ConsistentRead=True,
        )
        item = response.get("Item")
        if not item:
            raise SessionNotFoundError(f"Session not found: {session_id}")
        return _deserialize_item(item)

    def update_status(
        self,
        session_id: str,
        status: str,
        error_message: Optional[str] = None,
    ) -> None:
        """Update only the status field (and optionally error_message)."""
        update_expr = "SET #st = :st"
        expr_values = {":st": {"S": status}}
        if error_message:
            update_expr += ", error_message = :err"
            expr_values[":err"] = {"S": error_message}

        self.client.update_item(
            TableName=TABLE_NAME,
            Key={"session_id": {"S": session_id}},
            UpdateExpression=update_expr,
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues=expr_values,
        )
        logger.info("Session %s status -> %s", session_id, status)

    def save_ranked_papers(
        self,
        session_id: str,
        query: str,
        hybrid_k: int,
        ranked_papers_s3_key: str,
    ) -> None:
        """Save ranking metadata and S3 key; set status=RANKED."""
        self.client.update_item(
            TableName=TABLE_NAME,
            Key={"session_id": {"S": session_id}},
            UpdateExpression=(
                "SET #st = :st, #q = :q, hybrid_k = :k, "
                "ranked_papers_s3_key = :rk"
            ),
            ExpressionAttributeNames={"#st": "status", "#q": "query"},
            ExpressionAttributeValues={
                ":st": {"S": SessionStatus.RANKED},
                ":q": {"S": query},
                ":k": {"N": str(hybrid_k)},
                ":rk": {"S": ranked_papers_s3_key},
            },
        )
        logger.info("Session %s updated to RANKED (s3_key=%s)", session_id, ranked_papers_s3_key)

    def get_ranked_papers_key(self, session_id: str) -> str:
        """Return the S3 key for the ranked_papers.json file."""
        session = self.get_session(session_id)
        key = session.get("ranked_papers_s3_key")
        if not key:
            raise SessionStateError(
                f"Session {session_id} has no ranked_papers_s3_key "
                f"(status={session.get('status')})"
            )
        return key


# ---------------------------------------------------------------------------
# Module-level helpers for ranked papers S3 storage
# ---------------------------------------------------------------------------

def save_ranked_papers_to_s3(s3_bucket: str, session_id: str, ranked_data: dict) -> str:
    """
    Serialize ranked_data dict to JSON and store in S3.

    Returns the S3 key (e.g. 'ranked/{session_id}/ranked_papers.json').
    """
    s3 = boto3.client("s3", region_name=REGION)
    s3_key = f"ranked/{session_id}/ranked_papers.json"
    body = json.dumps(ranked_data, default=str).encode("utf-8")
    s3.put_object(Bucket=s3_bucket, Key=s3_key, Body=body, ContentType="application/json")
    logger.info("Saved ranked papers to s3://%s/%s", s3_bucket, s3_key)
    return s3_key


def load_ranked_papers_from_s3(s3_bucket: str, s3_key: str) -> dict:
    """Download and deserialize ranked papers JSON from S3."""
    s3 = boto3.client("s3", region_name=REGION)
    response = s3.get_object(Bucket=s3_bucket, Key=s3_key)
    body = response["Body"].read()
    data = json.loads(body)
    logger.info("Loaded ranked papers from s3://%s/%s", s3_bucket, s3_key)
    return data


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _deserialize_item(item: dict) -> dict:
    """Convert DynamoDB typed attribute map to plain Python dict."""
    result = {}
    for key, typed_val in item.items():
        if "S" in typed_val:
            result[key] = typed_val["S"]
        elif "N" in typed_val:
            # Return as int if whole number, else float
            val = typed_val["N"]
            result[key] = int(val) if "." not in val else float(val)
        elif "BOOL" in typed_val:
            result[key] = typed_val["BOOL"]
        elif "NULL" in typed_val:
            result[key] = None
        elif "L" in typed_val:
            result[key] = [_deserialize_item({"v": v})["v"] for v in typed_val["L"]]
        elif "M" in typed_val:
            result[key] = _deserialize_item(typed_val["M"])
        else:
            result[key] = typed_val  # fallback
    return result
