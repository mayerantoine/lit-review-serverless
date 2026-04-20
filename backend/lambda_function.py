"""
AWS Lambda Handler for Literature Review Serverless API

This module provides the Lambda entry point that wraps the FastAPI application
using Mangum adapter. The actual FastAPI app is defined in server.py.

Lambda Configuration:
---------------------
- Runtime: Python 3.12
- Handler: lambda_handler.handler
- Timeout: 300 seconds (5 minutes) - Required for indexing operations
- Memory: 2048 MB or higher - Required for embedding generation
- Environment Variables: See .env.example for required variables

Deployment:
-----------
1. Build package: ./build-lambda.sh
2. Upload to S3 or deploy directly (if < 50MB)
3. Create Lambda function with handler: lambda_handler.handler
4. Set environment variables (OPENAI_API_KEY, S3_VECTOR, etc.)
5. Configure API Gateway or Function URL

Notes:
------
- Session storage uses in-memory dict (not suitable for production)
- For production, replace with DynamoDB or ElastiCache Redis
- Background cleanup tasks are disabled (lifespan="off")
- CORS is configured in server.py middleware
"""

# Import the Mangum handler from server.py
# This handler wraps the FastAPI app for Lambda compatibility
from server import handler

# Lambda entry point
# AWS Lambda will call this function for each request
# handler is a Mangum instance that adapts FastAPI to Lambda's event format
def lambda_handler(event, context):
    """
    AWS Lambda entry point.

    Args:
        event: Lambda event dict (API Gateway proxy format)
        context: Lambda context object

    Returns:
        API Gateway proxy response dict
    """
    return handler(event, context)
