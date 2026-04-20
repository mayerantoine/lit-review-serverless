 1. **Upload Phase:**
    - User uploads CSV
    - Lambda writes to /tmp/papers.csv
    - S3 upload: s3://lit-review-data/{session_id}/paper_*.json
    - DynamoDB: Create session record
    - Bedrock KB: Start ingestion job
    - DynamoDB: Update session status = "indexed"
2. **Ranking Phase:**
    - User submits query
    - Lambda reads session from DynamoDB
    - Bedrock KB: Retrieve with session_id filter
    - OpenAI: Score relevance (parallel)
    - S3: Save ranked_papers.json
    - DynamoDB: Update session with S3 reference
3. **Generation Phase:**
    - User clicks Generate
    - Lambda reads session from DynamoDB
    - S3: Load ranked_papers.json
    - Bedrock: Stream text generation
    - Return SSE stream to user