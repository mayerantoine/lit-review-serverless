# AWS Lambda Deployment Guide

Complete guide to deploying the Literature Review Serverless API to AWS Lambda.

## Table of Contents

1. [Prerequisites](#prerequisites)
2. [Build Lambda Package](#build-lambda-package)
3. [Deploy to AWS](#deploy-to-aws)
4. [Configuration](#configuration)
5. [Testing](#testing)
6. [Troubleshooting](#troubleshooting)

---

## Prerequisites

### Local Requirements

- **Docker Desktop**: For building Lambda package
  - Download: https://www.docker.com/products/docker-desktop
  - Must be running before building
- **AWS CLI** (optional): For command-line deployment
  - Install: `brew install awscli` (macOS)
  - Configure: `aws configure`

### AWS Requirements

- **AWS Account** with appropriate permissions
- **S3 Buckets** (create before deployment):
  - Vector storage: `lit-llm-s3-vectors-{account_id}`
  - Paper data: `lit-review-papers`
  - CSV uploads: `lit-review-upload`
- **OpenAI API Key**: For embeddings and generation

### Estimated Costs

- **Lambda**: ~$0.20 per 1M requests + compute time
- **S3**: ~$0.023 per GB/month
- **OpenAI API**: ~$0.10-1.00 per request (varies by query)

---

## Build Lambda Package

### Quick Build

```bash
cd backend
./build-lambda.sh
```

This will:
1. Build Docker image with Linux x86_64 binaries
2. Install and optimize dependencies
3. Create `dist/lambda_package.zip`
4. Display package size and deployment instructions

### Build Options

```bash
# Clean previous builds first
./build-lambda.sh --clean

# Build without Docker cache (force fresh build)
./build-lambda.sh --no-cache

# Show help
./build-lambda.sh --help
```

### Expected Package Size

- **Target**: < 100MB (compressed)
- **Actual**: 60-90MB typically
- **Limit**: 50MB for direct upload, 250MB via S3

**Note**: If package > 50MB, you must upload to S3 first (script will show instructions).

---

## Deploy to AWS

### Option 1: AWS Console (Easiest)

#### Step 1: Create Lambda Function

1. Go to [AWS Lambda Console](https://console.aws.amazon.com/lambda)
2. Click **Create function**
3. Choose **Author from scratch**
4. Configure:
   - **Function name**: `lit-review-api`
   - **Runtime**: Python 3.12
   - **Architecture**: x86_64
5. Click **Create function**

#### Step 2: Upload Package

**If package < 50MB:**
1. In function page, click **Upload from** → **.zip file**
2. Select `dist/lambda_package.zip`
3. Click **Save**

**If package > 50MB:**
1. Upload to S3:
   ```bash
   aws s3 cp dist/lambda_package.zip s3://your-deployment-bucket/
   ```
2. In Lambda console, click **Upload from** → **Amazon S3 location**
3. Enter S3 URL: `s3://your-deployment-bucket/lambda_package.zip`
4. Click **Save**

#### Step 3: Configure Handler

1. In **Runtime settings**, click **Edit**
2. Set **Handler**: `lambda_function.handler`
3. Click **Save**

#### Step 4: Configure Memory and Timeout

1. Go to **Configuration** → **General configuration**
2. Click **Edit**
3. Set:
   - **Memory**: 2048 MB (or higher for large datasets)
   - **Timeout**: 5 minutes (300 seconds)
4. Click **Save**

#### Step 5: Set Environment Variables

1. Go to **Configuration** → **Environment variables**
2. Click **Edit** → **Add environment variable**
3. Add these variables (see [Configuration](#configuration) for values):

| Key | Example Value | Required |
|-----|---------------|----------|
| `OPENAI_API_KEY` | `sk-proj-...` | ✅ Yes |
| `DEFAULT_AWS_REGION` | `us-east-2` | ✅ Yes |
| `AWS_ACCOUNT_ID` | `979294212144` | ✅ Yes |
| `S3_VECTOR` | `lit-llm-s3-vectors-979294212144` | ✅ Yes |
| `BEDROCK_KNOWLEDGE_S3_DATA` | `lit-review-papers` | ✅ Yes |
| `S3_DATA_CSV` | `lit-review-upload` | ✅ Yes |
| `ALLOWED_ORIGINS` | `https://yourdomain.com` | ⚠️ Recommended |
| `ENVIRONMENT` | `production` | ⚠️ Recommended |

4. Click **Save**

#### Step 6: Create Function URL (for direct HTTP access)

1. Go to **Configuration** → **Function URL**
2. Click **Create function URL**
3. Configure:
   - **Auth type**: `NONE` (or `AWS_IAM` for secured access)
   - **CORS**: Enable
     - **Allow origin**: `*` or your domain
     - **Allow methods**: `GET, POST, OPTIONS`
     - **Allow headers**: `Content-Type, Authorization`
4. Click **Save**
5. **Copy the Function URL** - This is your API endpoint!

---

### Option 2: AWS CLI (Recommended for CI/CD)

#### Create Function

```bash
# Upload package to S3 first (if > 50MB)
aws s3 cp dist/lambda_package.zip s3://your-deployment-bucket/

# Create Lambda function
aws lambda create-function \
  --function-name lit-review-api \
  --runtime python3.12 \
  --role arn:aws:iam::YOUR_ACCOUNT_ID:role/lambda-execution-role \
  --handler lambda_function.handler \
  --timeout 300 \
  --memory-size 2048 \
  --code S3Bucket=your-deployment-bucket,S3Key=lambda_package.zip \
  --environment Variables="{
    OPENAI_API_KEY=sk-proj-...,
    DEFAULT_AWS_REGION=us-east-2,
    AWS_ACCOUNT_ID=979294212144,
    S3_VECTOR=lit-llm-s3-vectors-979294212144,
    BEDROCK_KNOWLEDGE_S3_DATA=lit-review-papers,
    S3_DATA_CSV=lit-review-upload
  }"
```

#### Update Function Code

```bash
# Upload new package
aws s3 cp dist/lambda_package.zip s3://your-deployment-bucket/

# Update Lambda
aws lambda update-function-code \
  --function-name lit-review-api \
  --s3-bucket your-deployment-bucket \
  --s3-key lambda_package.zip
```

#### Create Function URL

```bash
aws lambda create-function-url-config \
  --function-name lit-review-api \
  --auth-type NONE \
  --cors '{
    "AllowOrigins": ["*"],
    "AllowMethods": ["GET", "POST", "OPTIONS"],
    "AllowHeaders": ["Content-Type", "Authorization"],
    "MaxAge": 86400
  }'

# Get the URL
aws lambda get-function-url-config --function-name lit-review-api
```

---

### Option 3: API Gateway (Production Setup)

For production deployments with custom domains, rate limiting, and API keys:

1. Create Lambda function (as above)
2. Create API Gateway REST API
3. Create resource and methods (POST /api/*)
4. Set up Lambda integration
5. Deploy to stage (e.g., `prod`)
6. Configure custom domain (optional)

See AWS documentation: https://docs.aws.amazon.com/apigateway/latest/developerguide/

---

## Configuration

### Environment Variables

Copy from `.env.example` and set in Lambda console:

```bash
# OpenAI API
OPENAI_API_KEY=sk-proj-YOUR_KEY_HERE

# AWS Configuration
DEFAULT_AWS_REGION=us-east-2
AWS_ACCOUNT_ID=979294212144

# S3 Buckets (must be created first)
S3_VECTOR=lit-llm-s3-vectors-979294212144
BEDROCK_KNOWLEDGE_S3_DATA=lit-review-papers
S3_DATA_CSV=lit-review-upload

# Optional: CORS (comma-separated origins)
ALLOWED_ORIGINS=https://yourdomain.com,https://www.yourdomain.com

# Optional: Environment type
ENVIRONMENT=production
```

### IAM Role Permissions

Lambda execution role needs these policies:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:PutObject",
        "s3:DeleteObject",
        "s3:ListBucket"
      ],
      "Resource": [
        "arn:aws:s3:::lit-llm-s3-vectors-*",
        "arn:aws:s3:::lit-llm-s3-vectors-*/*",
        "arn:aws:s3:::lit-review-papers",
        "arn:aws:s3:::lit-review-papers/*",
        "arn:aws:s3:::lit-review-upload",
        "arn:aws:s3:::lit-review-upload/*"
      ]
    },
    {
      "Effect": "Allow",
      "Action": [
        "s3vectors:*"
      ],
      "Resource": "*"
    },
    {
      "Effect": "Allow",
      "Action": [
        "logs:CreateLogGroup",
        "logs:CreateLogStream",
        "logs:PutLogEvents"
      ],
      "Resource": "arn:aws:logs:*:*:*"
    }
  ]
}
```

---

## Testing

### Test via Function URL

```bash
# Health check
curl https://YOUR_FUNCTION_URL.lambda-url.us-east-2.on.aws/health

# Upload and index (replace with your CSV)
curl -X POST https://YOUR_FUNCTION_URL.lambda-url.us-east-2.on.aws/api/upload-and-index \
  -F "file=@papers.csv" \
  -c cookies.txt

# Retrieve and rank
curl -X POST https://YOUR_FUNCTION_URL.lambda-url.us-east-2.on.aws/api/retrieve-and-rank \
  -H "Content-Type: application/json" \
  -b cookies.txt \
  -d '{
    "research_idea": "Your research query here",
    "hybrid_k": 50
  }'

# Generate literature review (streaming)
curl -X POST https://YOUR_FUNCTION_URL.lambda-url.us-east-2.on.aws/api/generate \
  -H "Content-Type: application/json" \
  -b cookies.txt \
  -N \
  -d '{
    "research_idea": "Your research query here"
  }'
```

### Monitor Logs

```bash
# Watch logs in real-time
aws logs tail /aws/lambda/lit-review-api --follow

# View recent errors
aws logs filter-log-events \
  --log-group-name /aws/lambda/lit-review-api \
  --filter-pattern "ERROR"
```

### Test with Frontend

Update frontend `.env.local`:

```bash
NEXT_PUBLIC_API_URL=https://YOUR_FUNCTION_URL.lambda-url.us-east-2.on.aws
```

---

## Troubleshooting

### Package Too Large (> 50MB)

**Solution 1**: Upload via S3 (recommended)
- See [Deploy to AWS](#deploy-to-aws) instructions above

**Solution 2**: Use Lambda Layers
- Extract dependencies to layer
- Keep only app code in function package

**Solution 3**: Use Lambda Container Images
- Build Docker image with all dependencies
- Push to Amazon ECR
- Deploy as container (up to 10GB)

### Import Errors

**Error**: `ModuleNotFoundError: No module named 'agents'`

**Solution**: Ensure `openai-agents` is in `pyproject.toml` dependencies

### S3 Access Denied

**Error**: `botocore.exceptions.ClientError: An error occurred (AccessDenied)`

**Solution**:
1. Check IAM role has S3 permissions
2. Verify bucket names in environment variables
3. Ensure buckets exist in correct region

### Timeout Errors

**Error**: `Task timed out after 3.00 seconds`

**Solution**:
1. Increase timeout to 300 seconds (5 minutes)
2. Increase memory to 2048 MB or higher
3. Consider splitting large operations

### Cold Start Issues

Lambda may take 5-10 seconds on first request after idle.

**Solutions**:
- Enable **Provisioned Concurrency** (costs extra)
- Use **Lambda SnapStart** (Python 3.12 doesn't support yet)
- Optimize import statements (lazy imports)

### Session Storage Issues

**Problem**: In-memory sessions don't persist across Lambda invocations

**Solution**: Replace with DynamoDB or Redis (see server.py comments)

---

## Production Checklist

Before going to production:

- [ ] Replace in-memory sessions with DynamoDB/Redis
- [ ] Set `secure=True` for cookies (requires HTTPS)
- [ ] Configure proper CORS origins (not `*`)
- [ ] Set up CloudWatch alarms for errors/timeouts
- [ ] Enable X-Ray tracing for debugging
- [ ] Configure dead letter queue (DLQ)
- [ ] Set up API Gateway with rate limiting
- [ ] Configure custom domain with SSL
- [ ] Implement proper error handling and logging
- [ ] Set up CI/CD pipeline for deployments
- [ ] Configure backup/restore for S3 buckets
- [ ] Document API for team/users

---

## Additional Resources

- [AWS Lambda Documentation](https://docs.aws.amazon.com/lambda/)
- [FastAPI on Lambda Guide](https://mangum.io/)
- [API Gateway Setup](https://docs.aws.amazon.com/apigateway/)
- [S3 Vectors Documentation](https://docs.aws.amazon.com/s3/)

---

## Support

For issues:
1. Check CloudWatch logs: `/aws/lambda/lit-review-api`
2. Review error messages in Lambda console
3. Test endpoints with curl/Postman
4. Verify environment variables are set correctly
5. Check IAM permissions
