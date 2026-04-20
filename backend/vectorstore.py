import os
import time
import logging
import numpy as np
import boto3
from typing import List, Dict, Any, Optional
from tqdm import tqdm

from langchain_core.documents import Document
from langchain_openai import OpenAIEmbeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class VectorStoreAbstract:
    """
    Vector store using AWS S3 Vectors with session-based isolation.
    Each session gets a unique index in S3 Vectors.
    """

    def __init__(
        self,
        session_id: str,
        s3_bucket_name: str,
        abstracts: List = None,
        recreate_index: bool = True,
        storage_mode: str = "s3_vectors"
    ):
        """
        Initialize S3 Vectors-based vector store.

        Args:
            session_id: Unique session identifier
            s3_bucket_name: S3 bucket name for vector storage
            abstracts: List of abstract documents to index
            recreate_index: Whether to recreate existing index
            storage_mode: Storage mode (default: "s3_vectors")
        """
        self.session_id = session_id
        self.s3_bucket_name = s3_bucket_name
        self.abstracts = abstracts
        self.recreate_index = recreate_index
        self.storage_mode = storage_mode
        # S3 Vectors index name: lowercase, no underscores, must start/end with letter/number
        self.index_name = f"{session_id}-index".lower().replace('_', '-')

        # Initialize components
        self.embeddings: Optional[OpenAIEmbeddings] = None
        self.text_splitter: Optional[RecursiveCharacterTextSplitter] = None
        self._document_embeddings: Dict[str, np.ndarray] = {}  # Cache for embeddings

        # AWS clients with region configuration
        region = os.getenv('DEFAULT_AWS_REGION', 'us-east-1')
        self.s3_client = boto3.client('s3', region_name=region)
        self.s3vectors_client = boto3.client('s3vectors', region_name=region)

        self.initialize_store()

    def initialize_store(self):
        """Initialize S3 Vectors store and components."""
        logger.info(f"Initializing S3 Vectors store for session: {self.session_id}")

        # Text splitter
        self.text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=150,
            chunk_overlap=20,
            length_function=len,
            separators=["\n\n", "\n", ".", "!", "?", ",", " ", ""]
        )

        # OpenAI Embeddings
        self.embeddings = OpenAIEmbeddings(
            model="text-embedding-3-small"
        )

        # Create or get S3 Vectors index
        # Note: Vector bucket existence is validated implicitly by list_indexes()
        self.index_exists = self._check_s3_index_exists()

        if self.recreate_index and self.index_exists:
            logger.info(f"Recreating index: {self.index_name}")
            self._delete_s3_index()
            self._create_s3_index()
        elif not self.index_exists:
            logger.info(f"Creating new index: {self.index_name}")
            self._create_s3_index()
        else:
            logger.info(f"Using existing index: {self.index_name}")

    def _check_s3_index_exists(self) -> bool:
        """
        Check if S3 Vectors index exists for this session.
        This also validates that the vector bucket exists.
        """
        try:
            response = self.s3vectors_client.list_indexes(
                vectorBucketName=self.s3_bucket_name
            )
            indexes = response.get('indexes', [])
            exists = any(idx['indexName'] == self.index_name for idx in indexes)

            if exists:
                logger.info(f"S3 Vectors index exists: {self.index_name}")
            else:
                logger.info(f"S3 Vectors bucket exists: {self.s3_bucket_name}, but index {self.index_name} not found")

            return exists
        except Exception as e:
            logger.warning(f"Could not check index existence (bucket may not exist): {e}")
            return False

    def _create_s3_index(self):
        """Create S3 Vectors index for session."""
        try:
            # Get embedding dimension (OpenAI text-embedding-3-small = 1536)
            test_embedding = self.embeddings.embed_query("test")
            dimension = len(test_embedding)

            self.s3vectors_client.create_index(
                vectorBucketName=self.s3_bucket_name,
                indexName=self.index_name,
                dataType='float32',
                dimension=dimension,
                distanceMetric='cosine'
            )
            logger.info(f"Created S3 Vectors index: {self.index_name} (dim={dimension})")

            # Wait for index to be ready
            time.sleep(2)
        except Exception as e:
            logger.error(f"Failed to create S3 index: {e}")
            raise

    def _delete_s3_index(self):
        """Delete existing S3 Vectors index."""
        try:
            self.s3vectors_client.delete_index(
                vectorBucketName=self.s3_bucket_name,
                indexName=self.index_name
            )
            logger.info(f"Deleted S3 Vectors index: {self.index_name}")
            time.sleep(1)
        except Exception as e:
            logger.warning(f"Could not delete index: {e}")

    def chunking(self) -> List[Document]:
        """
        Chunk abstracts into smaller documents.

        Returns:
            List of chunked Document objects
        """
        all_chunked_documents: List[Document] = []

        if self.abstracts is None:
            return all_chunked_documents

        total_articles = len(self.abstracts)

        try:
            progress_bar = tqdm(total=total_articles, desc="Chunking documents", unit="article")
        except ImportError:
            progress_bar = None
            logger.info("Processing articles...")

        try:
            for content in self.abstracts:
                # Apply RecursiveCharacterTextSplitter
                size_constrained_chunks = self.text_splitter.split_text(content['title_abstract'])

                for i, chunk in enumerate(size_constrained_chunks):
                    chunked_document = Document(
                        page_content=chunk,
                        metadata={"id": content['id'], "chunk_index": i}
                    )
                    all_chunked_documents.append(chunked_document)

                if progress_bar:
                    progress_bar.update(1)

        finally:
            if progress_bar:
                progress_bar.close()

        return all_chunked_documents

    def index_document(self, all_chunked_documents: List[Document], batch_size: int = 50):
        """
        Index documents by creating embeddings and uploading to S3 Vectors.

        Args:
            all_chunked_documents: List of chunked documents
            batch_size: Number of documents to process per batch
        """
        total_docs = len(all_chunked_documents)
        processed_count = 0

        try:
            progress_bar = tqdm(total=total_docs, desc="Creating embeddings", unit="doc")
        except ImportError:
            progress_bar = None
            logger.info("Processing in batches...")

        try:
            # Process documents in batches
            for i in range(0, len(all_chunked_documents), batch_size):
                batch = all_chunked_documents[i:i + batch_size]

                # Generate embeddings for batch
                texts = [doc.page_content for doc in batch]
                embeddings = self.embeddings.embed_documents(texts)

                # Prepare vectors for S3 Vectors
                vectors = []
                for idx, (doc, embedding) in enumerate(zip(batch, embeddings)):
                    vector_key = f"{self.session_id}_doc_{processed_count + idx}"

                    # Store in cache
                    self._document_embeddings[vector_key] = np.array(embedding, dtype=np.float32)

                    vectors.append({
                        "key": vector_key,
                        "data": {"float32": embedding},
                        "metadata": {
                            "paper_id": str(doc.metadata.get('id')),
                            "session_id": self.session_id,
                            "source_text": doc.page_content[:500],  # Truncate for metadata
                            "chunk_index": str(doc.metadata.get('chunk_index', 0))
                        }
                    })

                # Upload to S3 Vectors
                self._upload_vectors_to_s3(vectors)

                processed_count += len(batch)

                if progress_bar:
                    progress_bar.update(len(batch))
                else:
                    logger.info(f"Processed {processed_count}/{total_docs} documents")

        finally:
            if progress_bar:
                progress_bar.close()

        logger.info(f"Indexed {total_docs} documents to S3 Vectors")

    def _upload_vectors_to_s3(self, vectors: List[Dict]):
        """Upload batch of vectors to S3 Vectors."""
        if not vectors:
            logger.warning("No vectors to upload")
            return

        try:
            response = self.s3vectors_client.put_vectors(
                vectorBucketName=self.s3_bucket_name,
                indexName=self.index_name,
                vectors=vectors
            )
            logger.debug(f"Successfully uploaded {len(vectors)} vectors")
        except Exception as e:
            logger.error(f"Failed to upload {len(vectors)} vectors to S3: {e}")
            # Log first vector for debugging
            if vectors:
                logger.error(f"Sample vector structure: {vectors[0]}")
            raise


    def _s3_vector_search(self, query: str, k: int = 20) -> List[Document]:
        """
        Perform semantic search using S3 Vectors.

        Args:
            query: Search query
            k: Number of results

        Returns:
            List of Document objects
        """
        try:
            # Generate embedding for query
            query_embedding = self.embeddings.embed_query(query)

            # Query S3 Vectors
            response = self.s3vectors_client.query_vectors(
                vectorBucketName=self.s3_bucket_name,
                indexName=self.index_name,
                queryVector={"float32": query_embedding},
                topK=k,
                returnDistance=True,
                returnMetadata=True
            )

            # Handle empty results
            if not response or 'vectors' not in response:
                logger.warning("No vectors returned from S3 query")
                return []

            vectors = response.get('vectors', [])
            if not vectors:
                logger.info("Query returned no matching vectors")
                return []

            # Convert to Document format
            results = []
            for vector in vectors:
                # Parse vector response structure
                vector_key = vector.get('key', '')
                distance = vector.get('distance', 0.0)
                metadata = vector.get('metadata', {})

                # Handle case where metadata might be empty
                if not metadata:
                    logger.debug(f"Vector {vector_key} has no metadata")
                    continue

                doc = Document(
                    page_content=metadata.get('source_text', ''),
                    metadata={
                        'id': metadata.get('paper_id', ''),
                        'session_id': metadata.get('session_id', ''),
                        'score': distance,
                        'chunk_index': int(metadata.get('chunk_index', 0)) if metadata.get('chunk_index') else 0,
                        'vector_key': vector_key
                    }
                )
                results.append(doc)

            logger.info(f"Retrieved {len(results)} documents from S3 Vectors")
            return results[:k]

        except Exception as e:
            logger.error(f"S3 vector search failed: {e}", exc_info=True)
            return []

    def hybrid_search(self, query: str, k: int = 20) -> List[Document]:
        """
        Perform semantic search using S3 Vectors.

        Note: This method previously used hybrid search (semantic + BM25).
        Now it only uses semantic search for simplicity.

        Args:
            query: Search query
            k: Number of results to return

        Returns:
            List of Document objects
        """
        return self._s3_vector_search(query, k=k)

    def semantic_search(self, query: str, k: int = 5) -> List[Dict[str, Any]]:
        """
        Perform semantic search on indexed documents.

        Args:
            query: Search query string
            k: Number of results to return

        Returns:
            List of search results with content and metadata
        """
        docs = self._s3_vector_search(query, k=k)

        # Format results
        results: List[Dict[str, Any]] = []
        for doc in docs:
            result = {
                "content": doc.page_content,
                "metadata": doc.metadata,
            }
            results.append(result)

        return results


    def should_process_documents(self) -> bool:
        """Determine if documents should be processed (chunked and indexed)."""
        return self.recreate_index or not self.index_exists

    def get_document_count(self) -> int:
        """Get the number of documents in the existing index."""
        try:
            response = self.s3vectors_client.describe_index(
                vectorBucketName=self.s3_bucket_name,
                indexName=self.index_name
            )
            return response.get('vectorCount', 0)
        except Exception:
            return 0

    def delete_session_documents(self):
        """Clean up session documents from S3 Vectors."""
        try:
            self._delete_s3_index()
            logger.info(f"Deleted session documents for: {self.session_id}")
        except Exception as e:
            logger.error(f"Failed to delete session documents: {e}")
