#!/bin/bash

# ============================================================================
# AWS Lambda Package Builder Script
# ============================================================================
# Purpose: Build x86_64 Lambda deployment package using Docker
# Compatible with: M1/M2/M3 Macs (ARM) and Intel Macs (x86_64)
#
# Usage:
#   ./build-lambda.sh [options]
#
# Options:
#   -c, --clean     Clean previous builds before building
#   -n, --no-cache  Build without Docker cache
#   -h, --help      Show this help message
#
# Requirements:
#   - Docker Desktop installed and running
#   - Sufficient disk space (~500MB for build)
#
# Output:
#   - dist/lambda_package.zip - Ready to deploy to AWS Lambda
# ============================================================================

set -e  # Exit on error

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Configuration
IMAGE_NAME="lambda-builder"
CONTAINER_NAME="lambda-extract"
DIST_DIR="./dist"
OUTPUT_FILE="${DIST_DIR}/lambda_package.zip"
DOCKERFILE="Dockerfile.lambda"

# Lambda size limits (in bytes)
LAMBDA_MAX_SIZE_ZIPPED=$((50 * 1024 * 1024))      # 50MB
LAMBDA_MAX_SIZE_S3=$((250 * 1024 * 1024))          # 250MB unzipped

# Parse command line arguments
CLEAN=false
NO_CACHE=""

while [[ $# -gt 0 ]]; do
    case $1 in
        -c|--clean)
            CLEAN=true
            shift
            ;;
        -n|--no-cache)
            NO_CACHE="--no-cache"
            shift
            ;;
        -h|--help)
            head -n 24 "$0" | tail -n 21
            exit 0
            ;;
        *)
            echo -e "${RED}Unknown option: $1${NC}"
            echo "Use --help for usage information"
            exit 1
            ;;
    esac
done

# ============================================================================
# Helper Functions
# ============================================================================

print_header() {
    echo -e "\n${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${BLUE}  $1${NC}"
    echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}\n"
}

print_success() {
    echo -e "${GREEN}✓ $1${NC}"
}

print_warning() {
    echo -e "${YELLOW}⚠ $1${NC}"
}

print_error() {
    echo -e "${RED}✗ $1${NC}"
}

print_info() {
    echo -e "${BLUE}ℹ $1${NC}"
}

format_bytes() {
    local bytes=$1
    if [ $bytes -lt 1024 ]; then
        echo "${bytes}B"
    elif [ $bytes -lt 1048576 ]; then
        echo "$(( bytes / 1024 ))KB"
    else
        echo "$(( bytes / 1048576 ))MB"
    fi
}

cleanup_docker() {
    if docker ps -a --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
        print_info "Cleaning up container: ${CONTAINER_NAME}"
        docker rm -f "${CONTAINER_NAME}" > /dev/null 2>&1 || true
    fi
}

# ============================================================================
# Pre-flight Checks
# ============================================================================

print_header "AWS Lambda Package Builder"

# Check if Docker is installed
if ! command -v docker &> /dev/null; then
    print_error "Docker is not installed. Please install Docker Desktop first."
    echo "Download from: https://www.docker.com/products/docker-desktop"
    exit 1
fi

# Check if Docker daemon is running
if ! docker info > /dev/null 2>&1; then
    print_error "Docker daemon is not running. Please start Docker Desktop."
    exit 1
fi

print_success "Docker is available and running"

# Check if required files exist
if [ ! -f "$DOCKERFILE" ]; then
    print_error "Dockerfile not found: $DOCKERFILE"
    exit 1
fi

if [ ! -f "pyproject.toml" ]; then
    print_error "pyproject.toml not found. Are you in the backend directory?"
    exit 1
fi

print_success "Required files found"

# ============================================================================
# Clean Previous Builds (if requested)
# ============================================================================

if [ "$CLEAN" = true ]; then
    print_header "Cleaning Previous Builds"

    # Remove dist directory
    if [ -d "$DIST_DIR" ]; then
        print_info "Removing ${DIST_DIR}/"
        rm -rf "${DIST_DIR}"
    fi

    # Remove Docker image
    if docker images --format '{{.Repository}}' | grep -q "^${IMAGE_NAME}$"; then
        print_info "Removing Docker image: ${IMAGE_NAME}"
        docker rmi -f "${IMAGE_NAME}" > /dev/null 2>&1 || true
    fi

    print_success "Cleanup complete"
fi

# ============================================================================
# Create Output Directory
# ============================================================================

mkdir -p "${DIST_DIR}"
print_success "Output directory ready: ${DIST_DIR}/"

# ============================================================================
# Build Docker Image
# ============================================================================

print_header "Building Docker Image"

print_info "Building for platform: linux/amd64 (x86_64)"
print_info "This may take 3-5 minutes on first build..."

BUILD_START=$(date +%s)

# Build with platform specification for M1/M2 Macs
if docker build \
    --platform linux/amd64 \
    -f "${DOCKERFILE}" \
    -t "${IMAGE_NAME}" \
    ${NO_CACHE} \
    . ; then

    BUILD_END=$(date +%s)
    BUILD_TIME=$((BUILD_END - BUILD_START))
    print_success "Docker image built successfully (${BUILD_TIME}s)"
else
    print_error "Docker build failed"
    exit 1
fi

# ============================================================================
# Extract Lambda Package
# ============================================================================

print_header "Extracting Lambda Package"

# Cleanup any existing container
cleanup_docker

# Create container
print_info "Creating temporary container..."
if ! docker create --name "${CONTAINER_NAME}" "${IMAGE_NAME}" > /dev/null; then
    print_error "Failed to create container"
    exit 1
fi

# Copy ZIP file
print_info "Extracting lambda_package.zip..."
if ! docker cp "${CONTAINER_NAME}:/asset/lambda_package.zip" "${OUTPUT_FILE}"; then
    print_error "Failed to extract package"
    cleanup_docker
    exit 1
fi

# Cleanup container
cleanup_docker

print_success "Package extracted to: ${OUTPUT_FILE}"

# ============================================================================
# Analyze Package Size
# ============================================================================

print_header "Package Analysis"

if [ -f "${OUTPUT_FILE}" ]; then
    FILE_SIZE=$(stat -f%z "${OUTPUT_FILE}" 2>/dev/null || stat -c%s "${OUTPUT_FILE}" 2>/dev/null)
    FILE_SIZE_MB=$((FILE_SIZE / 1048576))

    echo -e "${BLUE}Package Size:${NC} $(format_bytes ${FILE_SIZE}) (${FILE_SIZE} bytes)"
    echo ""

    # Check against Lambda limits
    if [ ${FILE_SIZE} -lt ${LAMBDA_MAX_SIZE_ZIPPED} ]; then
        print_success "Package is under 50MB limit - Direct upload supported"
        echo ""
        print_info "Deployment Options:"
        echo "  1. AWS Console: Upload ZIP directly in Lambda console"
        echo "  2. AWS CLI: aws lambda update-function-code --function-name <name> --zip-file fileb://${OUTPUT_FILE}"
        echo "  3. Terraform/CDK: Use local file upload"
    else
        print_warning "Package exceeds 50MB - Must use S3 upload"
        echo ""
        print_info "Required Deployment Steps:"
        echo "  1. Upload to S3:"
        echo "     aws s3 cp ${OUTPUT_FILE} s3://your-bucket/lambda_package.zip"
        echo ""
        echo "  2. Update Lambda function from S3:"
        echo "     aws lambda update-function-code \\"
        echo "       --function-name your-function-name \\"
        echo "       --s3-bucket your-bucket \\"
        echo "       --s3-key lambda_package.zip"

        if [ ${FILE_SIZE} -gt ${LAMBDA_MAX_SIZE_S3} ]; then
            echo ""
            print_error "Package exceeds 250MB unzipped limit!"
            echo ""
            print_warning "Recommended Solutions:"
            echo "  1. Split dependencies into Lambda Layer"
            echo "  2. Remove unnecessary packages from pyproject.toml"
            echo "  3. Use Lambda Container Images instead (up to 10GB)"
        fi
    fi

    echo ""
    print_info "Package Contents:"
    unzip -l "${OUTPUT_FILE}" | head -n 20
    echo "..."
    echo ""
    TOTAL_FILES=$(unzip -l "${OUTPUT_FILE}" | tail -1 | awk '{print $2}')
    echo "Total files: ${TOTAL_FILES}"

else
    print_error "Output file not found: ${OUTPUT_FILE}"
    exit 1
fi

# ============================================================================
# Success Summary
# ============================================================================

print_header "Build Complete ✓"

echo -e "${GREEN}Lambda package ready for deployment!${NC}"
echo ""
echo "📦 Package: ${OUTPUT_FILE}"
echo "📏 Size: $(format_bytes ${FILE_SIZE})"
echo "🎯 Handler: lambda_handler.handler"
echo "🐍 Runtime: python3.12"
echo ""
echo -e "${BLUE}Next Steps:${NC}"
echo "1. Set environment variables in Lambda console (.env.example)"
echo "2. Configure Lambda timeout: 300 seconds (5 min)"
echo "3. Configure Lambda memory: 2048 MB or higher"
echo "4. Deploy package using method above"
echo "5. Test with API Gateway or Lambda Function URL"
echo ""
print_success "Done!"
