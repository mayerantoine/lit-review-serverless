"""
AgentCore Retrieve-Rank Agent

Deployed as an ARM64 container on AWS AgentCore Runtime.
Invoked by Lambda 2 via bedrock-agentcore boto3 client.

Flow:
  1. Receive payload from Lambda 2
     { session_id, research_idea, hybrid_k, index_name, s3_vector_bucket, s3_data_bucket }
  2. Load papers from S3 (S3DocumentStore.load_session_papers)
  3. Reconnect to existing S3 Vectors index
  4. Run retrieve_and_rank_papers()
  5. Save ranked papers JSON to S3
  6. Update DynamoDB session to RANKED
  7. Return { status: "success", data: { ... } }
"""

import os
import json
import logging

from bedrock_agentcore.runtime import BedrockAgentCoreApp

# Shared modules — copied into the container image alongside agent.py
from pipeline import (
    LiteratureReviewPipeline,
    PipelineConfig,
)
from s3_storage import S3DocumentStore
from dynamodb_session import (
    DynamoDBSessionStore,
    SessionStatus,
    save_ranked_papers_to_s3,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = BedrockAgentCoreApp()

S3_DATA_BUCKET = os.environ["BEDROCK_KNOWLEDGE_S3_DATA"]
S3_VECTOR_BUCKET = os.environ["S3_VECTOR"]
REGION = os.environ.get("DEFAULT_AWS_REGION", "us-east-2")


@app.entrypoint
async def invoke(payload: dict, context):
    """
    Main entrypoint called by AgentCore Runtime.

    Returns a single JSON result (non-streaming).
    Yields one chunk so bedrock-agentcore can send it back to the caller.
    """
    session_id = payload.get("session_id", "")
    research_idea = payload.get("research_idea", "")
    hybrid_k = int(payload.get("hybrid_k", 50))
    index_name = payload.get("index_name", "")
    s3_vector_bucket = payload.get("s3_vector_bucket", S3_VECTOR_BUCKET)
    s3_data_bucket = payload.get("s3_data_bucket", S3_DATA_BUCKET)

    logger.info(
        "retrieve-rank-agent: session_id=%s hybrid_k=%d index=%s",
        session_id, hybrid_k, index_name,
    )

    session_store = DynamoDBSessionStore()

    try:
        # --- Step 1: Load papers from S3 ---
        logger.info("Loading papers from S3 for session %s", session_id)
        s3_store = S3DocumentStore(bucket_name=s3_data_bucket)
        # load_session_papers returns a DataFrame with id, title, abstract, title_abstract
        all_abstracts = s3_store.load_session_papers(session_id=session_id)
        logger.info("Loaded %d papers", len(all_abstracts))

        # --- Step 2: Reconnect to existing S3 Vectors index ---
        config = PipelineConfig(
            session_id=session_id,
            s3_vector_bucket=s3_vector_bucket,
            s3_data_bucket=s3_data_bucket,
            hybrid_k=hybrid_k,
            recreate_index=False,  # never recreate — index was built by Lambda 1
        )
        pipeline = LiteratureReviewPipeline(config)

        # Inject pre-loaded abstracts so retrieve_and_rank_papers doesn't reload from S3
        pipeline.all_abstracts = all_abstracts

        # --- Step 3: Retrieve and rank papers ---
        logger.info("Running retrieve_and_rank_papers")
        top_k_abstracts, all_scored_papers, retrieval_stats, scoring_stats = (
            pipeline.retrieve_and_rank_papers(research_idea)
        )

        logger.info(
            "Ranking complete: retrieved=%d top_k=%d",
            retrieval_stats.papers_retrieved,
            len(top_k_abstracts),
        )

        # --- Step 4: Build serialisable ranked data ---
        ranked_data = {
            "top_k_papers": _df_to_records(top_k_abstracts),
            "all_scored_papers": _df_to_records(all_scored_papers),
            "retrieval_stats": {
                "total_papers_in_corpus": retrieval_stats.total_papers_in_corpus,
                "papers_retrieved": retrieval_stats.papers_retrieved,
                "retrieval_rate": retrieval_stats.retrieval_rate,
                "retrieval_k": retrieval_stats.retrieval_k,
            },
            "scoring_stats": {
                "papers_scored": scoring_stats.papers_scored,
                "mean_score": scoring_stats.mean_score,
                "std_score": scoring_stats.std_score,
                "min_score": scoring_stats.min_score,
                "max_score": scoring_stats.max_score,
                "median_score": scoring_stats.median_score,
            },
        }

        # --- Step 5: Save ranked papers to S3 ---
        ranked_papers_s3_key = save_ranked_papers_to_s3(
            s3_bucket=s3_data_bucket,
            session_id=session_id,
            ranked_data=ranked_data,
        )
        logger.info("Saved ranked papers to S3 key: %s", ranked_papers_s3_key)

        # --- Step 6: Update DynamoDB to RANKED ---
        session_store.save_ranked_papers(
            session_id=session_id,
            query=research_idea,
            hybrid_k=hybrid_k,
            ranked_papers_s3_key=ranked_papers_s3_key,
        )

        # --- Step 7: Yield result back to Lambda 2 ---
        result = {
            "status": "success",
            "data": {
                **ranked_data,
                "ranked_papers_s3_key": ranked_papers_s3_key,
            },
        }
        yield json.dumps(result)

    except Exception as e:
        logger.exception("retrieve-rank-agent failed: %s", e)
        try:
            session_store.update_status(session_id, SessionStatus.ERROR, str(e))
        except Exception:
            pass
        yield json.dumps({"status": "error", "error": str(e)})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _df_to_records(df) -> list:
    """Convert DataFrame to JSON-serialisable list of dicts."""
    records = []
    for _, row in df.iterrows():
        record = {}
        for col in df.columns:
            val = row[col]
            # Convert numpy types to native Python
            if hasattr(val, "item"):
                val = val.item()
            record[col] = val
        records.append(record)
    return records


if __name__ == "__main__":
    app.run()
