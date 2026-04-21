"""
Lambda 1 — Upload & Index

Triggered by: API Gateway POST /api/upload-and-index (multipart/form-data)
Timeout: 900s  Memory: 2048MB

Flow:
  1. Parse multipart body from base64-encoded API Gateway event
  2. Validate CSV extension and size
  3. Generate session_id
  4. Upload CSV to S3 via S3CSVStorage
  5. Create DynamoDB session record (status=INDEXING)
  6. Run pipeline.build_index() → IndexResult
  7. Update DynamoDB (status=INDEXED)
  8. Return JSON with session_id + index stats
"""

import json
import os
import uuid
import logging
import base64
import io
from typing import Any

# Lambda Layer adds /opt/python to sys.path — shared/ modules importable directly
from s3_storage import S3CSVStorage, ValidationError, ProcessingError
from pipeline import LiteratureReviewPipeline, PipelineConfig
from dynamodb_session import DynamoDBSessionStore, SessionStatus

logger = logging.getLogger()
logger.setLevel(logging.INFO)

S3_CSV_BUCKET = os.environ["S3_DATA_CSV"]
S3_VECTOR_BUCKET = os.environ["S3_VECTOR"]
S3_DATA_BUCKET = os.environ["BEDROCK_KNOWLEDGE_S3_DATA"]


# ---------------------------------------------------------------------------
# Multipart parsing
# ---------------------------------------------------------------------------

def _parse_multipart(event: dict) -> tuple[bytes, str]:
    """
    Extract CSV file bytes and filename from an API Gateway multipart/form-data event.

    Returns:
        (file_content_bytes, original_filename)

    Raises:
        ValueError: if content-type header is missing or no CSV file part found
    """
    # API Gateway v2 (HTTP API) puts headers in lowercase
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    content_type = headers.get("content-type", "")

    if "multipart/form-data" not in content_type:
        raise ValueError(f"Expected multipart/form-data, got: {content_type}")

    # Extract boundary
    boundary = None
    for part in content_type.split(";"):
        part = part.strip()
        if part.startswith("boundary="):
            boundary = part[len("boundary="):].strip().strip('"')
            break

    if not boundary:
        raise ValueError("Missing boundary in Content-Type header")

    # Decode body
    body = event.get("body", "")
    is_b64 = event.get("isBase64Encoded", False)
    if is_b64:
        raw = base64.b64decode(body)
    else:
        raw = body.encode("utf-8") if isinstance(body, str) else body

    # Split on boundary lines
    delimiter = f"--{boundary}".encode()
    terminator = f"--{boundary}--".encode()

    parts = raw.split(delimiter)
    for part in parts:
        if not part or part.strip() == b"--" or part.strip() == b"":
            continue
        if part.startswith(b"--"):  # terminator remnant
            continue

        # Split headers and body at the first blank line (\r\n\r\n)
        if b"\r\n\r\n" in part:
            header_block, file_body = part.split(b"\r\n\r\n", 1)
        elif b"\n\n" in part:
            header_block, file_body = part.split(b"\n\n", 1)
        else:
            continue

        header_text = header_block.decode("utf-8", errors="replace")

        # Only process the file field (contains 'filename=')
        if "filename=" not in header_text:
            continue

        # Extract filename from Content-Disposition
        filename = "upload.csv"
        for line in header_text.splitlines():
            if "Content-Disposition" in line and "filename=" in line:
                for token in line.split(";"):
                    token = token.strip()
                    if token.startswith("filename="):
                        filename = token[len("filename="):].strip().strip('"')
                        break

        # Strip trailing CRLF added by multipart encoding
        file_bytes = file_body.rstrip(b"\r\n")

        return file_bytes, filename

    raise ValueError("No file part found in multipart body")


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------

def _ok(data: Any, status: int = 200) -> dict:
    return {
        "statusCode": status,
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
# Lambda handler
# ---------------------------------------------------------------------------

def lambda_handler(event: dict, context: Any) -> dict:
    logger.info("upload-and-index invoked")

    session_store = DynamoDBSessionStore()
    session_id = None

    try:
        # --- Step 1: Parse multipart body ---
        try:
            file_content, filename = _parse_multipart(event)
        except ValueError as e:
            return _error(str(e), 400)

        logger.info("Received file: %s (%d bytes)", filename, len(file_content))

        # --- Step 2: Validate CSV extension and size ---
        if not filename.lower().endswith(".csv"):
            return _error("Only CSV files are accepted.", 400)

        max_bytes = 50 * 1024 * 1024  # 50 MB
        if len(file_content) > max_bytes:
            return _error(
                f"File too large ({len(file_content) / 1024 / 1024:.1f} MB). Maximum is 50 MB.",
                400,
            )

        # --- Step 3: Generate session_id ---
        session_id = str(uuid.uuid4())[:8]
        logger.info("session_id=%s", session_id)

        # --- Step 4: Upload CSV to S3 ---
        csv_storage = S3CSVStorage(bucket_name=S3_CSV_BUCKET)
        s3_csv_uri = csv_storage.upload_csv(
            session_id=session_id,
            file_content=file_content,
            filename=filename,
        )
        logger.info("CSV uploaded to %s", s3_csv_uri)

        # --- Step 5: Create DynamoDB session (status=INDEXING) ---
        session_store.create_session(
            session_id=session_id,
            filename=filename,
            s3_csv_uri=s3_csv_uri,
            s3_vector_bucket=S3_VECTOR_BUCKET,
            s3_data_bucket=S3_DATA_BUCKET,
        )

        # --- Step 6: Build index ---
        config = PipelineConfig(
            session_id=session_id,
            s3_vector_bucket=S3_VECTOR_BUCKET,
            s3_data_bucket=S3_DATA_BUCKET,
            recreate_index=True,
        )
        pipeline = LiteratureReviewPipeline(config)

        try:
            index_result = pipeline.build_index(s3_csv_uri)
        except ValidationError as e:
            session_store.update_status(session_id, SessionStatus.ERROR, str(e))
            return _error(f"CSV validation failed: {e}", 400)
        except ProcessingError as e:
            session_store.update_status(session_id, SessionStatus.ERROR, str(e))
            return _error(f"Indexing failed: {e}", 500)

        # --- Step 7: Update DynamoDB (status=INDEXED) ---
        session_store.update_after_indexing(
            session_id=session_id,
            index_name=index_result.index_name,
            total_abstracts=index_result.total_abstracts,
            chunks_created=index_result.chunks_created,
        )

        # --- Step 8: Return success ---
        return _ok(
            {
                "session_id": session_id,
                "filename": filename,
                "s3_csv_uri": s3_csv_uri,
                "index_name": index_result.index_name,
                "total_abstracts": index_result.total_abstracts,
                "chunks_created": index_result.chunks_created,
                "total_indexed": index_result.total_indexed,
                "status": SessionStatus.INDEXED,
            },
            201,
        )

    except Exception as e:
        logger.exception("Unexpected error in upload-and-index")
        if session_id:
            try:
                session_store.update_status(session_id, SessionStatus.ERROR, str(e))
            except Exception:
                pass
        return _error(f"Internal server error: {e}", 500)
