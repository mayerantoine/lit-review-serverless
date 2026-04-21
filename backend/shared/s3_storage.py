import time
import json
import re
import io
from pathlib import Path
from typing import List, Dict, Any, Tuple, Optional

import boto3
import pandas as pd

# ============================================================================
# Exception Classes
# ============================================================================

class ValidationError(Exception):
    """Raised when data validation fails."""
    pass


class ProcessingError(Exception):
    """Raised when processing fails."""
    pass


# ============================================================================
# S3 CSV Storage (Lambda-Compatible)
# ============================================================================

def sanitize_csv_filename(filename: str) -> str:
    """
    Sanitize CSV filename for S3 storage.

    Args:
        filename: Original filename

    Returns:
        Sanitized filename safe for S3
    """
    # Remove path components
    filename = Path(filename).name

    # Ensure .csv extension
    if not filename.lower().endswith('.csv'):
        filename = filename + '.csv'

    # Replace unsafe characters with underscore
    safe = re.sub(r'[^a-zA-Z0-9._-]', '_', filename)

    # Limit length
    return safe[:100]


class S3CSVStorage:
    """Handle temporary CSV storage in S3 (Lambda-compatible)."""

    def __init__(self, bucket_name: str = "lit-review-upload"):
        self.s3 = boto3.client('s3')
        self.bucket = bucket_name

    def upload_csv(
        self,
        session_id: str,
        file_content: bytes,
        filename: str
    ) -> str:
        """
        Upload CSV file to S3.

        Args:
            session_id: Session identifier
            file_content: CSV file content (bytes)
            filename: Original filename

        Returns:
            S3 URI (s3://bucket/session_id/filename.csv)
        """
        # Sanitize filename
        safe_filename = sanitize_csv_filename(filename)
        s3_key = f"{session_id}/{safe_filename}"

        # Upload to S3
        self.s3.put_object(
            Bucket=self.bucket,
            Key=s3_key,
            Body=file_content,
            ContentType='text/csv',
            Metadata={
                'session_id': session_id,
                'original_filename': filename,
                'upload_timestamp': str(int(time.time()))
            }
        )

        s3_uri = f"s3://{self.bucket}/{s3_key}"
        return s3_uri

    def download_csv_to_memory(self, s3_uri: str) -> bytes:
        """
        Download CSV from S3 to memory.

        Args:
            s3_uri: S3 URI (s3://bucket/key)

        Returns:
            CSV content as bytes

        Raises:
            ProcessingError: If download fails
        """
        try:
            # Parse S3 URI
            if not s3_uri.startswith('s3://'):
                raise ValueError(f"Invalid S3 URI: {s3_uri}")

            parts = s3_uri[5:].split('/', 1)
            bucket = parts[0]
            key = parts[1] if len(parts) > 1 else ''

            # Download from S3
            response = self.s3.get_object(Bucket=bucket, Key=key)
            return response['Body'].read()

        except self.s3.exceptions.NoSuchKey:
            raise ProcessingError(f"CSV file not found in S3: {s3_uri}")
        except self.s3.exceptions.NoSuchBucket:
            raise ProcessingError(f"S3 bucket not found: {bucket}")
        except Exception as e:
            raise ProcessingError(f"Failed to download CSV from S3: {str(e)}")

    def delete_csv(self, s3_uri: str):
        """
        Delete CSV file from S3 (cleanup).

        Args:
            s3_uri: S3 URI (s3://bucket/key)
        """
        try:
            parts = s3_uri[5:].split('/', 1)
            bucket = parts[0]
            key = parts[1]
            self.s3.delete_object(Bucket=bucket, Key=key)
        except Exception as e:
            # Log but don't fail - cleanup is best effort
            print(f"Warning: Failed to delete S3 CSV {s3_uri}: {e}")


# ============================================================================
# Data Loading and Preparation
# ============================================================================

def validate_csv_path(csv_path: str) -> None:
    """Validate that CSV file exists and has correct extension."""
    path = Path(csv_path)

    if not path.exists():
        raise ValidationError(f"CSV file not found: {csv_path}")

    if not csv_path.lower().endswith('.csv'):
        raise ValidationError(f"File must be a CSV file (.csv): {csv_path}")


def load_abstracts_from_csv(csv_path: str) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    """
    Load and validate abstracts from CSV file.

    Args:
        csv_path: Path to the CSV file

    Returns:
        Tuple of (DataFrame, metadata dict)

    Raises:
        ValidationError: If file doesn't exist or validation fails
    """
    validate_csv_path(csv_path)

    # File size guard (50MB max)
    file_size_mb = Path(csv_path).stat().st_size / (1024 * 1024)
    if file_size_mb > 50:
        raise ValidationError(
            f"CSV file too large ({file_size_mb:.1f}MB). Maximum allowed size is 50MB."
        )

    # Read with explicit encoding, fall back to latin-1 for academic exports
    try:
        df = pd.read_csv(csv_path, encoding='utf-8')
    except UnicodeDecodeError:
        try:
            df = pd.read_csv(csv_path, encoding='latin-1')
        except Exception as e:
            raise ValidationError(f"Failed to read CSV (encoding issue): {str(e)}")
    except Exception as e:
        raise ValidationError(f"Failed to read CSV: {str(e)}")

    # Validate required columns
    required_columns = ['id', 'title', 'abstract']
    missing_columns = [col for col in required_columns if col not in df.columns]
    if missing_columns:
        raise ValidationError(
            f"CSV missing required columns: {', '.join(missing_columns)}. "
            f"Required: {', '.join(required_columns)}. "
            f"Found: {', '.join(df.columns)}"
        )

    # Empty file check
    if len(df) == 0:
        raise ValidationError("CSV file contains no rows.")

    # Minimum row count
    if len(df) < 3:
        raise ValidationError(
            f"CSV must contain at least 3 papers. Found: {len(df)}."
        )

    # Maximum row count (MVP limit)
    if len(df) > 300:
        raise ValidationError(
            f"CSV contains too many papers ({len(df)}). Maximum allowed for MVP is 300 papers."
        )

    # Null checks on required columns
    null_counts = {col: int(df[col].isna().sum()) for col in required_columns}
    cols_with_nulls = {col: count for col, count in null_counts.items() if count > 0}
    if cols_with_nulls:
        raise ValidationError(
            "CSV contains missing values: "
            + ", ".join(f"'{col}' ({n} null(s))" for col, n in cols_with_nulls.items())
        )

    # id must be numeric (coerce to int)
    try:
        df['id'] = pd.to_numeric(df['id'], errors='raise').astype(int)
    except (ValueError, TypeError):
        non_numeric = df['id'][pd.to_numeric(df['id'], errors='coerce').isna()].unique()[:5].tolist()
        raise ValidationError(
            f"Column 'id' must contain numeric values only. "
            f"Found non-numeric values: {non_numeric}"
        )

    # id must be unique
    duplicate_ids = df['id'][df['id'].duplicated()].tolist()
    if duplicate_ids:
        raise ValidationError(
            f"Column 'id' contains duplicate values: {duplicate_ids[:10]}. All IDs must be unique."
        )

    # title and abstract must not be blank strings
    for col in ['title', 'abstract']:
        empty_mask = df[col].astype(str).str.strip() == ''
        if empty_mask.any():
            raise ValidationError(
                f"Column '{col}' contains {int(empty_mask.sum())} empty string(s). "
                "All titles and abstracts must be non-empty."
            )

    metadata = {
        'count': len(df),
        'columns': list(df.columns),
        'required_columns': required_columns,
        'file_size_mb': round(file_size_mb, 2),
        'null_counts': null_counts,
    }

    return df, metadata


def load_abstracts_from_s3_csv(s3_uri: str) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    """
    Load and validate abstracts from CSV file stored in S3 (Lambda-compatible).

    Args:
        s3_uri: S3 URI (s3://bucket/key)

    Returns:
        Tuple of (DataFrame, metadata dict)

    Raises:
        ValidationError: If validation fails
        ProcessingError: If S3 download fails
    """
    # Download CSV from S3
    s3_csv = S3CSVStorage()
    csv_content = s3_csv.download_csv_to_memory(s3_uri)

    # Get file size
    file_size_mb = len(csv_content) / (1024 * 1024)
    if file_size_mb > 50:
        raise ValidationError(
            f"CSV file too large ({file_size_mb:.1f}MB). Maximum allowed size is 50MB."
        )

    # Read CSV from bytes using io.BytesIO
    csv_buffer = io.BytesIO(csv_content)

    # Read with explicit encoding, fall back to latin-1 for academic exports
    try:
        df = pd.read_csv(csv_buffer, encoding='utf-8')
    except UnicodeDecodeError:
        csv_buffer.seek(0)  # Reset buffer position
        try:
            df = pd.read_csv(csv_buffer, encoding='latin-1')
        except Exception as e:
            raise ValidationError(f"Failed to read CSV (encoding issue): {str(e)}")
    except Exception as e:
        raise ValidationError(f"Failed to read CSV from S3: {str(e)}")

    # Validate required columns (same as load_abstracts_from_csv)
    required_columns = ['id', 'title', 'abstract']
    missing_columns = [col for col in required_columns if col not in df.columns]
    if missing_columns:
        raise ValidationError(
            f"CSV missing required columns: {', '.join(missing_columns)}. "
            f"Required: {', '.join(required_columns)}. "
            f"Found: {', '.join(df.columns)}"
        )

    # Empty file check
    if len(df) == 0:
        raise ValidationError("CSV file contains no rows.")

    # Minimum row count
    if len(df) < 3:
        raise ValidationError(
            f"CSV must contain at least 3 papers. Found: {len(df)}."
        )

    # Maximum row count (MVP limit)
    if len(df) > 300:
        raise ValidationError(
            f"CSV contains too many papers ({len(df)}). Maximum allowed for MVP is 300 papers."
        )

    # Null checks on required columns
    null_counts = {col: int(df[col].isna().sum()) for col in required_columns}
    cols_with_nulls = {col: count for col, count in null_counts.items() if count > 0}
    if cols_with_nulls:
        raise ValidationError(
            "CSV contains missing values: "
            + ", ".join(f"'{col}' ({n} null(s))" for col, n in cols_with_nulls.items())
        )

    # id must be numeric (coerce to int)
    try:
        df['id'] = pd.to_numeric(df['id'], errors='raise').astype(int)
    except (ValueError, TypeError):
        non_numeric = df['id'][pd.to_numeric(df['id'], errors='coerce').isna()].unique()[:5].tolist()
        raise ValidationError(
            f"Column 'id' must contain numeric values only. "
            f"Found non-numeric values: {non_numeric}"
        )

    # id must be unique
    duplicate_ids = df['id'][df['id'].duplicated()].tolist()
    if duplicate_ids:
        raise ValidationError(
            f"Column 'id' contains duplicate values: {duplicate_ids[:10]}. All IDs must be unique."
        )

    # title and abstract must not be blank strings
    for col in ['title', 'abstract']:
        empty_mask = df[col].astype(str).str.strip() == ''
        if empty_mask.any():
            raise ValidationError(
                f"Column '{col}' contains {int(empty_mask.sum())} empty string(s). "
                "All titles and abstracts must be non-empty."
            )

    metadata = {
        'count': len(df),
        'columns': list(df.columns),
        'required_columns': required_columns,
        'file_size_mb': round(file_size_mb, 2),
        'null_counts': null_counts,
        's3_uri': s3_uri,
    }

    return df, metadata


def prepare_abstracts_for_indexing(
    df: pd.DataFrame,
    random_seed: int = 42
) -> Tuple[pd.DataFrame, List[Dict[str, Any]]]:
    """
    Prepare abstracts for indexing by shuffling and combining title+abstract.

    Args:
        df: DataFrame with abstracts
        random_seed: Random seed for shuffling

    Returns:
        Tuple of (processed DataFrame, list of dicts for indexing)
    """
    # Shuffle for variety
    df_processed = df.sample(frac=1, random_state=random_seed).reset_index(drop=True).copy()

    # Concatenate title and abstract
    df_processed['title_abstract'] = df_processed['title'] + df_processed['abstract']

    # Convert to list of dictionaries
    samples_abstracts = [
        v for k, v in df_processed[['title_abstract', 'id']].reset_index(drop=True).T.to_dict().items()
    ]

    return df_processed, samples_abstracts



class S3DocumentStore:
    def __init__(self, bucket_name: str = "lit-review-papers"):
        self.s3 = boto3.client('s3')
        self.bucket = bucket_name

    def upload_session_papers(self, session_id: str, csv_path: str, df: pd.DataFrame) -> str:
        """Upload CSV papers to S3 with session prefix"""
        # Create one JSON document per paper (required for Bedrock KB)
        for _, row in df.iterrows():
            paper_doc = {
                "id": int(row['id']),
                "title": str(row['title']),
                "abstract": str(row['abstract']),
                "content": f"{row['title']}\n\n{row['abstract']}",  # Combined text
                "metadata": {
                    "session_id": session_id,  # ← KEY for isolation
                    "paper_id": int(row['id']),
                    "upload_timestamp": int(time.time())
                }
            }

            # Upload to S3: s3://lit-review-papers/{session_id}/paper_{id}.json
            key = f"{session_id}/paper_{row['id']}.json"
            self.s3.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=json.dumps(paper_doc),
                ContentType='application/json',
                Metadata={
                    'session_id': session_id,
                    'paper_id': str(row['id'])
                }
            )

        return f"s3://{self.bucket}/{session_id}/"

    def load_session_papers(self, session_id: str) -> pd.DataFrame:
        """
        Load papers from S3 for a given session.

        Args:
            session_id: Session identifier

        Returns:
            DataFrame with papers (id, title, abstract, title_abstract)

        Raises:
            ProcessingError: If session not found or load fails
        """
        try:
            # List all paper files for this session
            prefix = f"{session_id}/paper_"
            response = self.s3.list_objects_v2(
                Bucket=self.bucket,
                Prefix=prefix
            )

            if 'Contents' not in response:
                raise ProcessingError(f"No papers found for session: {session_id}")

            # Load all paper JSON files
            papers = []
            for obj in response['Contents']:
                key = obj['Key']

                # Download and parse JSON
                obj_response = self.s3.get_object(
                    Bucket=self.bucket,
                    Key=key
                )
                content = obj_response['Body'].read().decode('utf-8')
                paper_doc = json.loads(content)

                # Extract paper data
                papers.append({
                    'id': paper_doc['id'],
                    'title': paper_doc['title'],
                    'abstract': paper_doc['abstract']
                })

            # Convert to DataFrame
            df = pd.DataFrame(papers)

            # Add title_abstract column (required for scoring)
            df['title_abstract'] = df['title'] + '\n\n' + df['abstract']

            print(f"Loaded {len(df)} papers from s3://{self.bucket}/{session_id}/")
            return df

        except self.s3.exceptions.NoSuchBucket:
            raise ProcessingError(f"S3 bucket not found: {self.bucket}")
        except Exception as e:
            if "No papers found" in str(e):
                raise
            raise ProcessingError(f"Failed to load papers from S3: {str(e)}")
