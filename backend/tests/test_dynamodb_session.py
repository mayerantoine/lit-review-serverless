"""
Quick smoke-test for dynamodb_session.py against DynamoDB Local.

Start DynamoDB Local first:
  docker run -d -p 8001:8000 amazon/dynamodb-local

Then run:
  AWS_DEFAULT_REGION=us-east-2 \
  DYNAMODB_TABLE=LitReviewSessions \
  python shared/test_dynamodb_session.py
"""

import os
import boto3
import uuid

# Point to DynamoDB Local
os.environ.setdefault("DYNAMODB_TABLE", "LitReviewSessions")
os.environ.setdefault("DEFAULT_AWS_REGION", "us-east-2")

LOCAL_ENDPOINT = "http://localhost:8001"

# Override boto3 to use local endpoint
import dynamodb_session as ds  # noqa: E402 (must set env vars first)

# Patch client to use local endpoint
ds.REGION = "us-east-2"
_orig_client = boto3.client

def _local_client(service, **kwargs):
    if service == "dynamodb":
        kwargs["endpoint_url"] = LOCAL_ENDPOINT
    return _orig_client(service, **kwargs)

import unittest.mock as mock


def create_local_table(endpoint: str, table_name: str):
    ddb = boto3.client("dynamodb", region_name="us-east-2", endpoint_url=endpoint)
    existing = ddb.list_tables()["TableNames"]
    if table_name in existing:
        print(f"Table '{table_name}' already exists.")
        return
    ddb.create_table(
        TableName=table_name,
        AttributeDefinitions=[{"AttributeName": "session_id", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "session_id", "KeyType": "HASH"}],
        BillingMode="PAY_PER_REQUEST",
    )
    print(f"Created table '{table_name}'.")


def run_tests():
    table_name = os.environ["DYNAMODB_TABLE"]
    create_local_table(LOCAL_ENDPOINT, table_name)

    session_id = str(uuid.uuid4())[:8]
    print(f"\nTesting with session_id={session_id}")

    with mock.patch("dynamodb_session.boto3") as mock_boto3:
        # Wire mock to local DynamoDB
        real_ddb = boto3.client("dynamodb", region_name="us-east-2", endpoint_url=LOCAL_ENDPOINT)
        real_s3 = boto3.client("s3", region_name="us-east-2")

        def client_side_effect(service, **kwargs):
            if service == "dynamodb":
                return real_ddb
            return real_s3

        mock_boto3.client.side_effect = client_side_effect

        store = ds.DynamoDBSessionStore()
        # Force client refresh
        store._client = real_ddb

        # 1. Create session
        store.create_session(
            session_id=session_id,
            filename="papers.csv",
            s3_csv_uri=f"s3://lit-review-upload/{session_id}/papers.csv",
            s3_vector_bucket="lit-llm-s3-vectors-test",
            s3_data_bucket="lit-review-papers",
        )
        print("create_session: OK")

        # 2. get_session
        sess = store.get_session(session_id)
        assert sess["status"] == "INDEXING", f"Expected INDEXING, got {sess['status']}"
        assert sess["filename"] == "papers.csv"
        print(f"get_session: OK (status={sess['status']})")

        # 3. update_after_indexing
        store.update_after_indexing(session_id, "my-index", total_abstracts=42, chunks_created=168)
        sess = store.get_session(session_id)
        assert sess["status"] == "INDEXED"
        assert sess["total_abstracts"] == 42
        print(f"update_after_indexing: OK (status={sess['status']}, abstracts={sess['total_abstracts']})")

        # 4. update_status
        store.update_status(session_id, ds.SessionStatus.RANKING)
        sess = store.get_session(session_id)
        assert sess["status"] == "RANKING"
        print("update_status -> RANKING: OK")

        # 5. save_ranked_papers
        store.save_ranked_papers(session_id, "my research idea", 50, "ranked/abc/ranked_papers.json")
        sess = store.get_session(session_id)
        assert sess["status"] == "RANKED"
        assert sess["ranked_papers_s3_key"] == "ranked/abc/ranked_papers.json"
        print(f"save_ranked_papers: OK (status={sess['status']})")

        # 6. get_ranked_papers_key
        key = store.get_ranked_papers_key(session_id)
        assert key == "ranked/abc/ranked_papers.json"
        print(f"get_ranked_papers_key: OK (key={key})")

        # 7. SessionNotFoundError
        try:
            store.get_session("no-such-id")
            assert False, "Should have raised SessionNotFoundError"
        except ds.SessionNotFoundError:
            print("SessionNotFoundError: OK")

    print("\nAll tests passed.")


if __name__ == "__main__":
    run_tests()
