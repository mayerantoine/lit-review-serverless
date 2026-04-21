"""
AgentCore Generate Agent

Deployed as an ARM64 container on AWS AgentCore Runtime.
Invoked by Lambda 3 via bedrock-agentcore boto3 client (streaming).

Flow:
  1. Receive payload from Lambda 3
     { session_id, research_idea, selected_paper_ids, ranked_papers_s3_key, s3_data_bucket }
  2. Load ranked papers JSON from S3
  3. Build selected_papers DataFrame (filter by selected_paper_ids if provided)
  4. Call generate_related_work_text(stream=True) -> generator
  5. Yield each SSE chunk back to Lambda 3 (streaming)
  6. Update DynamoDB status=DONE on [DONE] sentinel
"""

import os
import json
import logging

import pandas as pd

from bedrock_agentcore.runtime import BedrockAgentCoreApp

# Shared modules — copied into the container image alongside agent.py
from pipeline import generate_related_work_text
from dynamodb_session import (
    DynamoDBSessionStore,
    SessionStatus,
    load_ranked_papers_from_s3,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = BedrockAgentCoreApp()

S3_DATA_BUCKET = os.environ["BEDROCK_KNOWLEDGE_S3_DATA"]
GENERATION_MODEL = os.environ.get("GENERATION_MODEL", "gpt-4o-mini")
REGION = os.environ.get("DEFAULT_AWS_REGION", "us-east-2")


@app.entrypoint
async def invoke(payload: dict, context):
    """
    Streaming entrypoint — yields SSE chunks back to Lambda 3.

    Each yielded string is forwarded as-is to the frontend via Lambda 3's
    response stream. The generator from generate_related_work_text() already
    produces properly formatted SSE lines ("data: ...\n\n").
    """
    session_id = payload.get("session_id", "")
    research_idea = payload.get("research_idea", "")
    selected_paper_ids = payload.get("selected_paper_ids", [])
    ranked_papers_s3_key = payload.get("ranked_papers_s3_key", "")
    s3_data_bucket = payload.get("s3_data_bucket", S3_DATA_BUCKET)

    logger.info(
        "generate-agent: session_id=%s selected_ids=%s",
        session_id, selected_paper_ids,
    )

    session_store = DynamoDBSessionStore()

    try:
        # --- Step 1: Load ranked papers from S3 ---
        logger.info("Loading ranked papers from s3://%s/%s", s3_data_bucket, ranked_papers_s3_key)
        ranked_data = load_ranked_papers_from_s3(
            s3_bucket=s3_data_bucket,
            s3_key=ranked_papers_s3_key,
        )

        # --- Step 2: Build selected_papers DataFrame ---
        # Use top_k_papers by default; filter to selected_paper_ids if provided
        top_k_records = ranked_data.get("top_k_papers", [])
        all_scored_records = ranked_data.get("all_scored_papers", [])

        if selected_paper_ids:
            # Frontend sent explicit paper IDs — filter from all scored papers
            id_set = set(int(i) for i in selected_paper_ids)
            records = [r for r in all_scored_records if int(r["id"]) in id_set]
            if not records:
                # Fall back to top_k if none matched
                logger.warning(
                    "selected_paper_ids %s matched no papers — falling back to top_k",
                    selected_paper_ids,
                )
                records = top_k_records
        else:
            records = top_k_records

        if not records:
            yield "data: [ERROR]{\"type\":\"error\",\"message\":\"No papers available for generation\"}\n\n"
            return

        selected_papers = pd.DataFrame(records)
        logger.info("Generating review for %d papers", len(selected_papers))

        # --- Step 3: Stream generation ---
        # generate_related_work_text(stream=True) returns a generator of SSE strings
        for chunk in generate_related_work_text(
            query=research_idea,
            selected_papers=selected_papers,
            generation_model=GENERATION_MODEL,
            stream=True,
        ):
            yield chunk

            # Watch for [DONE] to update DynamoDB
            if "[DONE]" in chunk:
                session_store.update_status(session_id, SessionStatus.DONE)
                logger.info("Generation complete for session %s", session_id)

    except Exception as e:
        logger.exception("generate-agent failed: %s", e)
        try:
            session_store.update_status(session_id, SessionStatus.ERROR, str(e))
        except Exception:
            pass
        yield f"data: [ERROR]{json.dumps({'type': 'error', 'message': str(e)})}\n\n"


if __name__ == "__main__":
    app.run()
