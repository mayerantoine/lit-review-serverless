import os
import uuid
import re
import asyncio
from datetime import datetime
from typing import Dict, Any

from fastapi import FastAPI, UploadFile, File, HTTPException, Request
from fastapi.responses import PlainTextResponse, StreamingResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from openai import OpenAI
from pydantic import BaseModel
import time
import shutil
from pathlib import Path
from pipeline import LiteratureReviewPipeline, PipelineConfig, ValidationError, ProcessingError
from dataclasses import asdict
from config import config, get_logger

# Initialize logger
logger = get_logger(__name__)



class ResearchIdeaRequest(BaseModel):
    research_idea: str
    hybrid_k: int | None = 50
    selected_paper_ids: list[int] | None = None

app = FastAPI()

# Configure CORS - Read allowed origins from environment
# Default to localhost for development, specify production domains via ALLOWED_ORIGINS env var
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "http://localhost:3000,http://localhost:3001").split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in ALLOWED_ORIGINS],  # Explicit whitelist
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],  # Only needed methods
    allow_headers=["Content-Type", "Authorization"],  # Only needed headers
)

# Session middleware
@app.middleware("http")
async def session_middleware(request: Request, call_next):
    """Attach session ID to all requests via cookie"""
    session_id = request.cookies.get("session_id")
    if not session_id:
        session_id = str(uuid.uuid4())[:8]  # 8-char session ID

    # Attach to request state
    request.state.session_id = session_id

    # Process request
    response = await call_next(request)

    # Set cookie on response (24 hour expiry)
    response.set_cookie(
        key="session_id",
        value=session_id,
        max_age=86400,  # 24 hours
        httponly=True,
        secure=False,  # False for localhost (HTTP), set to True in production (HTTPS)
        samesite="none"  # Required for cross-origin cookies
    )

    return response

def sanitize_filename(filename: str) -> str:
    """Convert filename to safe collection name"""
    base = Path(filename).stem  # Remove extension
    # Replace special chars with underscore, keep alphanumeric
    safe = re.sub(r'[^a-zA-Z0-9_]', '_', base)
    # Lowercase and limit length
    return safe.lower()[:50]


# Session store (in-memory - upgrade to Redis/DB for production)
SESSIONS: Dict[str, Dict[str, Any]] = {}
# Format: {session_id: {s3_csv_uri, pipeline, index_result, timestamp, filename}}


# ============================================================================
# Session Cleanup - Prevents Memory Leak
# ============================================================================

async def cleanup_expired_sessions():
    """
    Background task to clean up expired sessions from memory.

    Runs every hour and removes sessions older than 24 hours (86400 seconds).
    This prevents memory leak from sessions accumulating indefinitely.
    """
    SESSION_EXPIRY_SECONDS = 86400  # 24 hours
    CLEANUP_INTERVAL_SECONDS = 3600  # 1 hour

    logger.info("Session cleanup task started (runs every hour)")

    while True:
        try:
            await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)

            current_time = datetime.now().timestamp()
            sessions_before = len(SESSIONS)

            # Find expired sessions
            expired_session_ids = [
                session_id for session_id, data in SESSIONS.items()
                if current_time - data.get('timestamp', 0) > SESSION_EXPIRY_SECONDS
            ]

            # Remove expired sessions
            for session_id in expired_session_ids:
                try:
                    session_data = SESSIONS[session_id]

                    # Clean up S3 CSV file if exists
                    if 's3_csv_uri' in session_data:
                        try:
                            from s3_storage import S3CSVStorage
                            s3_csv_bucket = os.getenv('S3_DATA_CSV')
                            if s3_csv_bucket:
                                s3_csv = S3CSVStorage(bucket_name=s3_csv_bucket)
                                s3_csv.delete_csv(session_data['s3_csv_uri'])
                                logger.info(f"Deleted S3 CSV for session {session_id}: {session_data['s3_csv_uri']}")
                        except Exception as e:
                            logger.warning(f"Failed to delete S3 CSV for session {session_id}: {e}")

                    # Optional: Clean up S3 vector store and paper documents
                    # if 'pipeline' in session_data:
                    #     session_data['pipeline'].vector_store.delete_session_documents()

                    del SESSIONS[session_id]
                    logger.info(f"Cleaned up expired session: {session_id}")
                except Exception as e:
                    logger.error(f"Error cleaning up session {session_id}: {e}")

            sessions_after = len(SESSIONS)

            if expired_session_ids:
                logger.info(
                    f"Session cleanup complete: Removed {len(expired_session_ids)} expired sessions "
                    f"({sessions_before} -> {sessions_after} active sessions)"
                )
            else:
                logger.debug(f"Session cleanup: No expired sessions found ({sessions_after} active)")

        except Exception as e:
            logger.error(f"Error in session cleanup task: {e}")
            # Continue running even if cleanup fails
            await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)


@app.on_event("startup")
async def startup_event():
    """Start background tasks when the application starts."""
    logger.info("Starting FastAPI application...")

    # Start session cleanup task in the background
    asyncio.create_task(cleanup_expired_sessions())

    logger.info("Application startup complete")

@app.post("/api/upload-and-index")
async def upload_and_index(file: UploadFile = File(...), request: Request = None):
    """
    Upload a CSV file and build the vector index for literature review.

    Returns:
        JSON with indexing statistics
    """
    global SESSIONS

    # Get session ID
    session_id = request.state.session_id

    try:
        # Validate file extension
        if not file.filename.endswith('.csv'):
            raise HTTPException(
                status_code=400,
                detail="File must be a CSV (.csv extension required)"
            )

        # Upload CSV to S3 (Lambda-compatible)
        from s3_storage import S3CSVStorage

        timestamp = int(time.time())

        # Read file content into memory
        csv_content = await file.read()

        # Get S3 CSV bucket configuration
        s3_csv_bucket = os.getenv('S3_DATA_CSV')
        if not s3_csv_bucket:
            raise HTTPException(
                status_code=500,
                detail="S3_DATA_CSV bucket not configured. Check environment variables."
            )

        # Upload to S3
        s3_csv = S3CSVStorage(bucket_name=s3_csv_bucket)
        s3_csv_uri = s3_csv.upload_csv(
            session_id=session_id,
            file_content=csv_content,
            filename=file.filename
        )

        logger.info(f"Session {session_id}: Uploaded CSV to {s3_csv_uri}")

        # Get S3 configuration from environment
        s3_vector_bucket = os.getenv('S3_VECTOR')
        s3_data_bucket = os.getenv('BEDROCK_KNOWLEDGE_S3_DATA')

        if not s3_vector_bucket or not s3_data_bucket:
            raise HTTPException(
                status_code=500,
                detail="S3 configuration missing. Check S3_VECTOR and BEDROCK_KNOWLEDGE_S3_DATA environment variables."
            )

        # Create pipeline configuration
        pipeline_config = PipelineConfig(
            session_id=session_id,
            s3_vector_bucket=s3_vector_bucket,
            s3_data_bucket=s3_data_bucket,
            storage_mode="s3_vectors",
            recreate_index=True
        )

        # Initialize pipeline
        pipeline = LiteratureReviewPipeline(pipeline_config)

        # Build index (Steps 1-3: Load CSV from S3, create embeddings, index to S3 Vectors)
        logger.info(f"Session {session_id}: Building index from {s3_csv_uri}")
        index_result = pipeline.build_index(s3_csv_uri)

        # Store session data
        SESSIONS[session_id] = {
            's3_csv_uri': s3_csv_uri,  # S3 URI instead of local path
            'session_id': session_id,
            'pipeline': pipeline,
            'index_result': index_result,
            'timestamp': timestamp,
            'filename': file.filename
        }

        logger.info(f"Session {session_id}: Index built successfully. {index_result.total_abstracts} papers indexed.")

        # Return indexing statistics
        return JSONResponse({
            "status": "success",
            "message": f"Successfully indexed {index_result.total_abstracts} papers",
            "data": {
                "session_id": index_result.session_id,
                "index_name": index_result.index_name,
                "total_abstracts": index_result.total_abstracts,
                "chunks_created": index_result.chunks_created,
                "total_indexed": index_result.total_indexed,
                "s3_uri": index_result.s3_uri,
                "s3_vector_bucket": s3_vector_bucket,
                "s3_data_bucket": s3_data_bucket,
                "recreated": index_result.recreated
            }
        })

    except ValidationError as e:
        logger.error(f"Session {session_id}: Validation error - {e}")
        raise HTTPException(status_code=400, detail=str(e))
    except ProcessingError as e:
        logger.error(f"Session {session_id}: Processing error - {e}")
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        logger.error(f"Session {session_id}: Unexpected error - {e}")
        raise HTTPException(status_code=500, detail=f"Unexpected error: {str(e)}")

@app.post("/api/retrieve-and-rank")
def retrieve_and_rank(request: ResearchIdeaRequest, http_request: Request = None):
    """
    Retrieve and rank papers for a given research idea (Steps 4-6 of pipeline).

    This endpoint must be called after /api/upload-and-index and before /api/generate.
    It performs retrieval, relevance scoring, and top-k selection.

    Returns:
        JSON with top-k papers, retrieval stats, and scoring stats
    """
    global SESSIONS

    # Get session ID
    session_id = http_request.state.session_id

    # Check if session has uploaded data
    if session_id not in SESSIONS:
        raise HTTPException(
            status_code=400,
            detail="No active session. Please upload a CSV file first using 'Upload & Index File'."
        )

    # Get session data
    session_data = SESSIONS[session_id]
    pipeline = session_data['pipeline']

    try:
        # Get query from request
        query = request.research_idea.strip()
        if not query:
            raise HTTPException(status_code=400, detail="Research idea cannot be empty")

        # Get optional parameters
        hybrid_k = request.hybrid_k or 50
        num_to_score = None  # Score all retrieved papers by default

        # Update pipeline config with request parameters
        pipeline.config.hybrid_k = hybrid_k
        pipeline.config.num_abstracts_to_score = num_to_score

        logger.info(f"Session {session_id}: Retrieving and ranking papers for query (length={len(query)}, hybrid_k={hybrid_k})")

        # Steps 4-6: Retrieve, score, and select top papers
        top_k_abstracts, all_scored_papers, retrieval_stats, scoring_stats = pipeline.retrieve_and_rank_papers(query)

        logger.info(f"Session {session_id}: Retrieved {retrieval_stats.papers_retrieved} papers, scored {scoring_stats.papers_scored}, selected top {len(top_k_abstracts)}")

        # Convert DataFrames to list of dicts for JSON serialization
        top_k_papers_list = []
        for idx, row in top_k_abstracts.iterrows():
            top_k_papers_list.append({
                'id': int(row['id']),
                'title': str(row['title']),
                'abstract': str(row['abstract']),
                'relevance_score': float(row['relevance_score'])
            })

        all_scored_papers_list = []
        for idx, row in all_scored_papers.iterrows():
            all_scored_papers_list.append({
                'id': int(row['id']),
                'title': str(row['title']),
                'abstract': str(row['abstract']),
                'relevance_score': float(row['relevance_score'])
            })

        # Store ranked papers in session for next step (generation)
        SESSIONS[session_id]['ranked_papers'] = {
            'query': query,
            'top_k_abstracts': top_k_abstracts,
            'all_scored_papers': all_scored_papers,
            'retrieval_stats': retrieval_stats,
            'scoring_stats': scoring_stats
        }

        # Return results
        return JSONResponse({
            "status": "success",
            "message": f"Retrieved and ranked {scoring_stats.papers_scored} papers",
            "data": {
                "query": query,
                "retrieval_stats": {
                    "total_papers_in_corpus": retrieval_stats.total_papers_in_corpus,
                    "papers_retrieved": retrieval_stats.papers_retrieved,
                    "retrieval_rate": retrieval_stats.retrieval_rate,
                    "retrieval_k": retrieval_stats.retrieval_k
                },
                "scoring_stats": {
                    "papers_scored": scoring_stats.papers_scored,
                    "mean_score": scoring_stats.mean_score,
                    "std_score": scoring_stats.std_score,
                    "min_score": scoring_stats.min_score,
                    "max_score": scoring_stats.max_score,
                    "median_score": scoring_stats.median_score
                },
                "top_k_papers": top_k_papers_list,
                "all_scored_papers": all_scored_papers_list
            }
        })

    except ValidationError as e:
        logger.error(f"Session {session_id}: Validation error - {e}")
        raise HTTPException(status_code=400, detail=str(e))
    except ProcessingError as e:
        logger.error(f"Session {session_id}: Processing error - {e}")
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        logger.error(f"Session {session_id}: Unexpected error - {e}")
        raise HTTPException(status_code=500, detail=f"Unexpected error: {str(e)}")



@app.post("/api/generate")
def lit_review(request: ResearchIdeaRequest, http_request: Request = None):
    """
    Generate related work section using pre-ranked papers (streaming).

    Requires that /api/retrieve-and-rank has been called first to rank papers.
    This endpoint only performs Step 7 (text generation) using the pre-ranked papers.
    """
    global SESSIONS

    # Get session ID
    session_id = http_request.state.session_id

    # Verify session exists
    if session_id not in SESSIONS:
        raise HTTPException(
            status_code=400,
            detail="No active session. Please upload and rank papers first."
        )

    # Get session data
    session_data = SESSIONS[session_id]
    pipeline = session_data['pipeline']

    # Check if papers have been ranked
    if 'ranked_papers' not in session_data:
        raise HTTPException(
            status_code=400,
            detail="No ranked papers found. Please call /api/retrieve-and-rank first."
        )

    try:
        # Get the research idea from request
        query = request.research_idea.strip()
        if not query:
            raise HTTPException(status_code=400, detail="Research idea cannot be empty")

        # Get ranked papers from session
        ranked_data = session_data['ranked_papers']

        # Check if user provided specific paper IDs to use
        if request.selected_paper_ids and len(request.selected_paper_ids) > 0:
            # Use user-selected papers
            all_scored_papers = ranked_data['all_scored_papers']
            selected_papers = all_scored_papers[all_scored_papers['id'].isin(request.selected_paper_ids)].copy()

            if len(selected_papers) == 0:
                raise HTTPException(
                    status_code=400,
                    detail="None of the selected paper IDs were found in the ranked papers."
                )

            # Sort by relevance score
            selected_papers = selected_papers.sort_values('relevance_score', ascending=False)
            top_k_abstracts = selected_papers

            logger.info(f"Session {session_id}: Using {len(selected_papers)} user-selected papers for generation")
        else:
            # Use the automatically selected top-k papers
            top_k_abstracts = ranked_data['top_k_abstracts']
            logger.info(f"Session {session_id}: Using top {len(top_k_abstracts)} automatically selected papers for generation")

        # Update pipeline config with request parameters (if provided)
        if request.selected_paper_ids and len(request.selected_paper_ids) > 0:
            pipeline.config.top_k = len(top_k_abstracts)

        logger.info(f"Session {session_id}: Generating literature review for query (length={len(query)})")

        # Import generate_related_work_text for direct streaming
        from pipeline import generate_related_work_text

        # Step 7: Generate related work text (streaming mode)
        # Call generate_related_work_text directly to use pre-ranked papers
        streaming_response = generate_related_work_text(
            query=query,
            selected_papers=top_k_abstracts,
            generation_model=pipeline.config.generation_model,
            stream=True
        )

        logger.info(f"Session {session_id}: Started streaming generation")

        return streaming_response

    except ValidationError as e:
        logger.error(f"Session {session_id}: Validation error - {e}")
        raise HTTPException(status_code=400, detail=str(e))
    except ProcessingError as e:
        logger.error(f"Session {session_id}: Processing error - {e}")
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        logger.error(f"Session {session_id}: Unexpected error - {e}")
        raise HTTPException(status_code=500, detail=f"Unexpected error: {str(e)}")


@app.get("/health")
def health_check():
    """Health check endpoint for AWS App Runner and Lambda"""
    return {"status": "healthy"}


# ============================================================================
# AWS Lambda Handler (using Mangum)
# ============================================================================

from mangum import Mangum

# Lambda handler - wraps FastAPI app for AWS Lambda
# NOTE: lifespan="off" disables startup/shutdown events because:
# - Lambda doesn't support long-running background tasks
# - The cleanup_expired_sessions() background task won't work in Lambda
# - Session storage should use DynamoDB or Redis for Lambda deployment
handler = Mangum(app, lifespan="off")

# PRODUCTION DEPLOYMENT NOTES:
# =============================
# 1. Cookie Security:
#    - Update server.py line 65: secure=False → secure=True (requires HTTPS)
#
# 2. Session Storage (CRITICAL for Lambda):
#    - In-memory SESSIONS dict won't persist across Lambda invocations
#    - Recommended solutions:
#      a) AWS DynamoDB with TTL for automatic expiration
#      b) AWS ElastiCache (Redis) for fast session storage
#    - Replace SESSIONS dict and cleanup_expired_sessions() function
#
# 3. Lambda Configuration:
#    - Timeout: Set to at least 300 seconds (5 min) for indexing operations
#    - Memory: Recommended 2048 MB or higher for embedding generation
#    - Environment variables: Set all required env vars in Lambda console
#
# 4. API Gateway:
#    - Enable binary media types for file uploads
#    - Configure CORS if using API Gateway (already handled by FastAPI)
#    - Consider timeout limits (API Gateway max: 30 seconds, use Lambda Function URLs for longer)
