# FastAPI Literature Review Service - API Documentation

This document describes the three-phase API for automated literature review generation using AWS S3 Vectors and OpenAI.

## Overview

The API follows a stateful, session-based workflow with three main endpoints:

1. **Upload & Index** - Upload CSV and build vector index
2. **Retrieve & Rank** - Retrieve and score papers using LLM
3. **Generate** - Generate literature review text (streaming)

## Session Management

Sessions are managed automatically via cookies:
- Each client receives a unique `session_id` cookie
- Session data is stored in-memory on the server
- Session expires after 24 hours

## Endpoints

### 1. POST /api/upload-and-index

Upload a CSV file containing academic papers and build a vector search index.

**Request:**
- Method: `POST`
- Content-Type: `multipart/form-data`
- Body: `file` (CSV file)

**CSV Requirements:**
- Required columns: `id`, `title`, `abstract`
- Maximum 300 papers (MVP limit)
- Maximum file size: 50MB
- IDs must be unique integers

**Response:**
```json
{
  "status": "success",
  "message": "Successfully indexed 150 papers",
  "data": {
    "session_id": "abc123",
    "index_name": "session-abc123-index",
    "total_abstracts": 150,
    "chunks_created": 450,
    "total_indexed": 450,
    "s3_uri": "s3://lit-review-papers/session-abc123/",
    "s3_vector_bucket": "lit-llm-s3-vectors-979294212144",
    "s3_data_bucket": "lit-review-papers",
    "recreated": true
  }
}
```

**Example (curl):**
```bash
curl -X POST http://localhost:8000/api/upload-and-index \
  -F "file=@papers.csv" \
  -c cookies.txt
```

---

### 2. POST /api/retrieve-and-rank

Retrieve relevant papers and score them using LLM-based relevance assessment.

**Request:**
- Method: `POST`
- Content-Type: `application/json`
- Requires: Prior call to `/api/upload-and-index`

**Body:**
```json
{
  "research_idea": "Your research query or abstract here...",
  "hybrid_k": 50  // Optional: Number of papers to retrieve (default: 50)
}
```

**Response:**
```json
{
  "status": "success",
  "message": "Retrieved and ranked 50 papers",
  "data": {
    "query": "Your research query...",
    "retrieval_stats": {
      "total_papers_in_corpus": 150,
      "papers_retrieved": 50,
      "retrieval_rate": 33.33,
      "retrieval_k": 50
    },
    "scoring_stats": {
      "papers_scored": 50,
      "mean_score": 45.23,
      "std_score": 12.34,
      "min_score": 15.50,
      "max_score": 89.75,
      "median_score": 44.00
    },
    "top_k_papers": [
      {
        "id": 123,
        "title": "Paper title...",
        "abstract": "Paper abstract...",
        "relevance_score": 89.75
      }
    ],
    "all_scored_papers": [...]
  }
}
```

**Example (curl):**
```bash
curl -X POST http://localhost:8000/api/retrieve-and-rank \
  -H "Content-Type: application/json" \
  -b cookies.txt \
  -d '{
    "research_idea": "Retrieval-Augmented Generation (RAG) combines neural language models with external knowledge retrieval to improve factual accuracy and reduce hallucinations in large language models.",
    "hybrid_k": 50
  }'
```

---

### 3. POST /api/generate

Generate a literature review section using the ranked papers (Server-Sent Events streaming).

**Request:**
- Method: `POST`
- Content-Type: `application/json`
- Requires: Prior call to `/api/retrieve-and-rank`

**Body:**
```json
{
  "research_idea": "Your research query or abstract here...",
  "selected_paper_ids": [123, 456, 789]  // Optional: Specific papers to use
}
```

If `selected_paper_ids` is not provided, the top-k papers from the ranking step will be used automatically.

**Response:**
Server-Sent Events (SSE) stream with:
1. Text chunks as they're generated
2. Metadata event with citations and references
3. Done event

**SSE Event Format:**
```
data: This literature review explores...

data: [METADATA]{"type":"metadata","length_chars":1234,...}

data: [DONE]
```

**Example (curl):**
```bash
curl -X POST http://localhost:8000/api/generate \
  -H "Content-Type: application/json" \
  -b cookies.txt \
  -N \
  -d '{
    "research_idea": "Retrieval-Augmented Generation (RAG)..."
  }'
```

**Example (Python):**
```python
import requests
import json

response = requests.post(
    'http://localhost:8000/api/generate',
    json={'research_idea': 'Your research query...'},
    stream=True,
    cookies=session_cookies
)

for line in response.iter_lines():
    if line:
        data = line.decode('utf-8')
        if data.startswith('data: '):
            content = data[6:]  # Remove 'data: ' prefix

            if content == '[DONE]':
                break
            elif content.startswith('[METADATA]'):
                metadata = json.loads(content[10:])
                print(f"\nCitations: {metadata['cited_paper_ids']}")
            else:
                print(content, end='', flush=True)
```

---

## Complete Workflow Example

```bash
# Step 1: Upload and index papers
curl -X POST http://localhost:8000/api/upload-and-index \
  -F "file=@papers.csv" \
  -c cookies.txt

# Step 2: Retrieve and rank relevant papers
curl -X POST http://localhost:8000/api/retrieve-and-rank \
  -H "Content-Type: application/json" \
  -b cookies.txt \
  -d '{
    "research_idea": "Retrieval-Augmented Generation combines neural language models with external knowledge retrieval.",
    "hybrid_k": 50
  }' | jq

# Step 3: Generate literature review (streaming)
curl -X POST http://localhost:8000/api/generate \
  -H "Content-Type: application/json" \
  -b cookies.txt \
  -N \
  -d '{
    "research_idea": "Retrieval-Augmented Generation combines neural language models with external knowledge retrieval."
  }'
```

---

## Error Responses

All endpoints return error responses in this format:

```json
{
  "detail": "Error message describing what went wrong"
}
```

**Common Status Codes:**
- `400` - Validation error (invalid CSV, missing session, etc.)
- `500` - Processing error (S3 failure, OpenAI API error, etc.)

---

## Environment Variables

Required configuration in `.env`:

```bash
# OpenAI API
OPENAI_API_KEY=sk-...

# AWS Configuration
DEFAULT_AWS_REGION=us-east-2
AWS_ACCOUNT_ID=979294212144

# S3 Buckets
S3_VECTOR=lit-llm-s3-vectors-979294212144
BEDROCK_KNOWLEDGE_S3_DATA=lit-review-papers
```

---

## Running the Server

```bash
# Install dependencies
cd backend
pip install -r requirements.txt  # or use uv

# Start server
uvicorn server:app --reload --host 0.0.0.0 --port 8000

# Server will be available at:
# - API: http://localhost:8000
# - Docs: http://localhost:8000/docs
# - Health: http://localhost:8000/health
```

---

## CLI Alternative

For non-web usage, use the CLI tool:

```bash
# Index papers
python test_kb.py index --csv papers.csv

# Retrieve and rank
python test_kb.py retrieve --session-id session-123 --query "Your query"

# Generate review
python test_kb.py generate --session-id session-123 --query "Your query"
```

See `test_kb.py --help` for full CLI documentation.
