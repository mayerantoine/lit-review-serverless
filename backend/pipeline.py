import asyncio
import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple, Annotated, Union

from fastapi.responses import StreamingResponse
import pandas as pd
import numpy as np
from pydantic import BaseModel, Field
import openai
from agents import Agent, Runner

from vectorstore import VectorStoreAbstract
from s3_storage import (
    S3DocumentStore,
    ValidationError,
    ProcessingError,
    load_abstracts_from_csv,
    load_abstracts_from_s3_csv,
    prepare_abstracts_for_indexing
)

from config import get_logger

# Initialize logger
logger = get_logger(__name__)


# ============================================================================
# Models and Configuration
# ============================================================================

class AbstractRelevance(BaseModel):
    """Structured relevance assessment for a candidate paper."""
    id: int
    arguments_for: str
    arguments_for_quotes: list[str]
    arguments_against: str
    arguments_against_quotes: list[str]
    probability_score: Annotated[
        float,
        Field(ge=1.0, le=100.0, description="A relevance score between 1 and 100.")
    ]


@dataclass
class PipelineConfig:
    """Configuration for the literature review pipeline with S3 Vectors storage."""
    # S3 Configuration
    session_id: Optional[str] = None  # Auto-generate if None
    s3_vector_bucket: str = ""  # Required: e.g., "lit-llm-s3-vectors-979294212144"
    s3_data_bucket: str = ""  # Required: e.g., "lit-review-papers"
    storage_mode: str = "s3_vectors"  # Fixed to "s3_vectors"
    recreate_index: bool = False

    # Pipeline parameters
    hybrid_k: int = 50
    num_abstracts_to_score: Optional[int] = None
    top_k: int = 3
    relevance_model: str = "gpt-4o-mini"
    generation_model: str = "gpt-4o-mini"
    random_seed: int = 42


@dataclass
class RetrievalStats:
    """Statistics from the retrieval phase."""
    total_papers_in_corpus: int
    papers_retrieved: int
    retrieval_rate: float
    retrieval_k: int


@dataclass
class ScoringStats:
    """Statistics from the relevance scoring phase."""
    papers_scored: int
    mean_score: float
    std_score: float
    min_score: float
    max_score: float
    median_score: float


@dataclass
class GenerationMetadata:
    """Metadata from the generation phase."""
    length_chars: int
    length_words: int
    total_citations: int
    unique_citations: int
    cited_paper_ids: List[int]


@dataclass
class IndexResult:
    """Result from the index building phase."""
    csv_path: str
    total_abstracts: int
    chunks_created: int
    total_indexed: int
    session_id: str
    s3_uri: str  # S3 location where raw papers are stored
    index_name: str  # S3 Vectors index name
    recreated: bool
    config: PipelineConfig


@dataclass
class PipelineResult:
    """Complete result from the pipeline execution."""
    query: str
    generated_text: str
    top_k_abstracts: pd.DataFrame
    retrieval_stats: RetrievalStats
    scoring_stats: ScoringStats
    generation_metadata: GenerationMetadata
    all_abstracts: pd.DataFrame
    config: PipelineConfig


# ============================================================================
# Vector Store Initialization
# ============================================================================


def initialize_vector_store(
    session_id: str,
    s3_vector_bucket: str,
    recreate_index: bool = True,
    storage_mode: str = "s3_vectors"
) -> VectorStoreAbstract:
    """
    Create and return VectorStoreAbstract instance without indexing documents.

    This function only creates the vector store instance. To load and index
    documents, use process_and_index_documents() separately.

    Args:
        session_id: Unique session identifier
        s3_vector_bucket: S3 bucket name for vector embeddings storage
        recreate_index: Whether to recreate existing index (default: True)
        storage_mode: Storage mode (default: "s3_vectors")

    Returns:
        VectorStoreAbstract instance

    Example:
        # Create vector store
        vector_store = initialize_vector_store("session-123", "my-s3-bucket")

        # Later, load and index documents
        metadata = process_and_index_documents(vector_store, csv_path, df, s3_data_bucket)
    """
    vector_store = VectorStoreAbstract(
        session_id=session_id,
        s3_bucket_name=s3_vector_bucket,
        abstracts=None,  # No abstracts at initialization
        recreate_index=recreate_index,
        storage_mode=storage_mode
    )
    return vector_store


def process_and_index_documents(
    vector_store: VectorStoreAbstract,
    csv_path: str,
    df: pd.DataFrame,
    s3_data_bucket: str
) -> Dict[str, Any]:
    """
    Load, prepare, chunk, and index documents into the vector store.

    Args:
        vector_store: VectorStoreAbstract instance to index documents into
        csv_path: Path to the CSV file
        df: DataFrame with paper data
        s3_data_bucket: S3 bucket name for raw paper data storage

    Returns:
        Metadata dict with indexing details including:
        - storage_mode: Storage mode used
        - session_id: Session identifier
        - s3_vector_bucket: Vector bucket name
        - s3_data_bucket: Data bucket name
        - index_name: S3 Vectors index name
        - s3_uri: URI where raw papers are stored
        - document_count: Number of chunked documents indexed

    Example:
        vector_store = initialize_vector_store("session-123", "my-vector-bucket")
        metadata = process_and_index_documents(vector_store, "data.csv", df, "my-data-bucket")
    """
    # Prepare abstracts for indexing
    _, samples_abstracts = prepare_abstracts_for_indexing(df)

    # Update vector store with abstracts
    vector_store.abstracts = samples_abstracts

    # Chunk documents
    chunked_documents = vector_store.chunking()

    # Index documents (creates embeddings and uploads to S3 Vectors)
    vector_store.index_document(chunked_documents)

    # Upload raw papers to S3 data bucket for reference
    s3_store = S3DocumentStore(bucket_name=s3_data_bucket)
    s3_uri = s3_store.upload_session_papers(vector_store.session_id, csv_path, df)

    metadata = {
        'storage_mode': vector_store.storage_mode,
        'session_id': vector_store.session_id,
        's3_vector_bucket': vector_store.s3_bucket_name,
        's3_data_bucket': s3_data_bucket,
        'index_name': vector_store.index_name,
        's3_uri': s3_uri,
        'document_count': len(chunked_documents)
    }

    return metadata


# ============================================================================
# Retrieval
# ============================================================================

def retrieve_relevant_papers(
    vector_store: VectorStoreAbstract,
    all_abstracts: pd.DataFrame,
    query: str,
    k: int
) -> Tuple[pd.DataFrame, RetrievalStats]:
    """
    Perform hybrid retrieval to find relevant papers.

    Returns:
        Tuple of (retrieved abstracts DataFrame, RetrievalStats)
    """
    try:
        rs = vector_store.hybrid_search(query, k=k)

        if rs is None:
            raise ProcessingError("Hybrid search returned no results")

        # Extract unique document IDs and convert to int
        # Note: S3 Vectors stores metadata as strings, but DataFrame has int IDs
        retrieved_docs_raw = [item.metadata.get('id') for item in rs if item.metadata.get('id')]
        logger.info(f"Raw retrieved IDs from S3 Vectors: {retrieved_docs_raw[:5]} (type: {type(retrieved_docs_raw[0]) if retrieved_docs_raw else 'N/A'})")

        # Convert string IDs to integers
        retrieved_docs = set()
        failed_conversions = []
        for doc_id in retrieved_docs_raw:
            try:
                retrieved_docs.add(int(doc_id))
            except (ValueError, TypeError) as e:
                failed_conversions.append((doc_id, str(e)))
                logger.warning(f"Failed to convert ID '{doc_id}' to int: {e}")

        if failed_conversions:
            logger.warning(f"Failed to convert {len(failed_conversions)} IDs: {failed_conversions[:5]}")

        logger.info(f"Converted retrieved IDs: {list(retrieved_docs)[:5]} (type: int)")
        logger.info(f"Sample all_abstracts IDs: {all_abstracts['id'].head(5).tolist()} (type: {type(all_abstracts['id'].iloc[0]) if len(all_abstracts) > 0 else 'N/A'})")

        # Filter abstracts DataFrame
        retrieved_abstracts = all_abstracts[all_abstracts['id'].isin(retrieved_docs)].copy()

        stats = RetrievalStats(
            total_papers_in_corpus=len(all_abstracts),
            papers_retrieved=len(retrieved_abstracts),
            retrieval_rate=len(retrieved_abstracts) / len(all_abstracts) * 100,
            retrieval_k=k
        )

        return retrieved_abstracts, stats

    except Exception as e:
        raise ProcessingError(f"Failed to perform hybrid retrieval: {str(e)}")


# ============================================================================
# Relevance Scoring
# ============================================================================

def create_relevance_agent(model: str) -> Agent:
    """Create an agent that scores paper relevance using debate-style reasoning."""

    INSTRUCTIONS_DEBATE_RANKING = """
    You are a helpful research assistant who is helping with literature review of a research idea.
    You will be given a query or research idea and a candidate reference abstract.
    Your task is to score reference abstract based on their relevance to the query. Please make sure you read and understand these instructions carefully.
    Please keep this document open while reviewing, and refer to it as needed.

    ## Instruction:
    Use the following steps to rank the reference papers:

    1. Generate arguments for including this reference abstract in the literature review.

    2. Generate arguments against including this reference abstract in the literature review.

    3. Extract relevant sentences from the candidate paper abstract to support each argument.

    4. Then, provide a score between 1 and 100 (up to two decimal places) that is proportional to the probability
    of a paper with the given query including the candidate reference paper in its literature review.

    Important:
    - Put the extracted sentences in quotes
    - You can use the information in other candidate papers when generating the arguments for a candidate paper
    - Generate arguments and probability for each paper separately
    - Do not generate anything else apart from the probability and the arguments
    - Follow this process even if a candidate paper happens to be identical or near-perfect match to the query abstract

    Your Response: """

    relevance_agent = Agent(
        name="RelevanceAgent",
        instructions=INSTRUCTIONS_DEBATE_RANKING,
        model=model,
        output_type=AbstractRelevance
    )

    return relevance_agent


async def score_single_paper(
    id: int,
    query: str,
    reference_paper: str,
    model: str
) -> Optional[AbstractRelevance]:
    """
    Score a single paper's relevance to the query.

    Returns:
        AbstractRelevance object if successful, None if scoring fails
    """
    try:
        # Log inputs
        logger.debug(f"Paper {id}: Starting scoring (query_len={len(query)}, ref_paper_len={len(reference_paper)}, model={model})")

        relevance_agent = create_relevance_agent(model)

        user_instructions = f"""
For this query abstract with id={id}

Given the query abstract: {query}

Given the candidate reference paper abstract: {reference_paper}

Your Reference Abstract Relevance:
"""

        result = await Runner.run(relevance_agent, input=user_instructions)

        # Validate result
        if result is None or result.final_output is None:
            logger.error(f"Paper {id}: Runner.run() returned None")
            return None

        return result.final_output

    except Exception as e:
        logger.error(f"Paper {id}: Scoring failed - {type(e).__name__}: {str(e)}")
        return None


async def score_papers_async(
    retrieved_abstracts: pd.DataFrame,
    query: str,
    model: str,
    num_to_score: Optional[int] = None
) -> List[AbstractRelevance]:
    """
    Score multiple abstracts in parallel.

    Returns:
        List of successfully scored AbstractRelevance objects (excludes failures)
    """

    # Select subset if specified
    abstracts_to_score = (
        retrieved_abstracts.head(num_to_score)
        if num_to_score is not None
        else retrieved_abstracts
    )

    total_to_score = len(abstracts_to_score)
    logger.info(f"Starting async scoring for {total_to_score} papers...")

    # Log sample of data being scored
    if len(abstracts_to_score) > 0:
        first_paper = abstracts_to_score.iloc[0]
        logger.info(f"Sample paper to score: ID={first_paper['id']}, title_abstract length={len(str(first_paper.get('title_abstract', '')))}")
        logger.info(f"First 100 chars of title_abstract: {str(first_paper.get('title_abstract', ''))[:100]}...")

    # Create async tasks for parallel execution
    tasks = [
        asyncio.create_task(
            score_single_paper(
                id=item['id'],
                query=query,
                reference_paper=item['title_abstract'],
                model=model
            )
        )
        for _, item in abstracts_to_score[['id', 'title_abstract']].iterrows()
    ]

    # Gather all results (including exceptions and None values)
    all_results = await asyncio.gather(*tasks, return_exceptions=True)

    # Filter out failures (None values and exceptions)
    successful_results = []
    failed_count = 0

    for result in all_results:
        if isinstance(result, Exception):
            # Exception was raised
            logger.error(f"Scoring exception: {type(result).__name__}: {str(result)}")
            failed_count += 1
        elif result is None:
            # score_single_paper returned None (already logged in that function)
            failed_count += 1
        else:
            # Successful result
            successful_results.append(result)

    # Log summary
    success_count = len(successful_results)
    logger.info(f"Scoring complete: {success_count}/{total_to_score} successful, {failed_count}/{total_to_score} failed")

    return successful_results


def score_papers_relevance(
    retrieved_abstracts: pd.DataFrame,
    query: str,
    relevance_model: str,
    num_to_score: Optional[int] = None
) -> Tuple[List[AbstractRelevance], ScoringStats]:
    """
    Score papers for relevance using the relevance agent.

    Returns:
        Tuple of (list of AbstractRelevance objects, ScoringStats)

    Raises:
        ProcessingError: If all scoring attempts fail or other errors occur
    """
    try:
        # Handle async execution
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                import nest_asyncio
                nest_asyncio.apply()
                results = loop.run_until_complete(
                    score_papers_async(retrieved_abstracts, query, relevance_model, num_to_score)
                )
            else:
                results = loop.run_until_complete(
                    score_papers_async(retrieved_abstracts, query, relevance_model, num_to_score)
                )
        except RuntimeError:
            results = asyncio.run(
                score_papers_async(retrieved_abstracts, query, relevance_model, num_to_score)
            )

        # Validate results before calculating statistics
        if not results or len(results) == 0:
            # Determine what was attempted
            num_attempted = num_to_score if num_to_score is not None else len(retrieved_abstracts)

            raise ProcessingError(
                f"All {num_attempted} paper scoring attempts failed. "
                "Possible causes:\n"
                "  1. OpenAI API key not set or invalid (check OPENAI_API_KEY environment variable)\n"
                "  2. Rate limiting from OpenAI API (try reducing --num-to-score or wait)\n"
                "  3. Network connectivity issues\n"
                "  4. Model name incorrect (check --model parameter)\n"
                "  5. Pydantic validation failures in AbstractRelevance model\n"
                "Check the error logs above for specific failure details."
            )

        # Calculate statistics
        scores = [abs.probability_score for abs in results]

        stats = ScoringStats(
            papers_scored=len(scores),
            mean_score=float(np.mean(scores)),
            std_score=float(np.std(scores)),
            min_score=float(np.min(scores)),
            max_score=float(np.max(scores)),
            median_score=float(np.median(scores))
        )

        return results, stats

    except ProcessingError:
        # Re-raise ProcessingError with full context
        raise
    except Exception as e:
        raise ProcessingError(f"Failed to score abstracts: {str(e)}")


def select_top_papers(
    results: List[AbstractRelevance],
    retrieved_abstracts: pd.DataFrame,
    k: int
) -> pd.DataFrame:
    """
    Select top-k papers by relevance score.

    Returns:
        DataFrame with top-k papers and relevance scores
    """
    try:
        # Get top-k scores
        scores = [(abs.id, abs.probability_score) for abs in results]
        sorted_scores = sorted(scores, key=lambda x: x[1], reverse=True)
        top_k_scores = sorted_scores[:k]

        # Extract IDs and get full information
        top_k_id = [id for id, score in top_k_scores]
        top_k_abstracts = retrieved_abstracts[retrieved_abstracts['id'].isin(top_k_id)].copy()

        # Add scores
        score_dict = {id: score for id, score in top_k_scores}
        top_k_abstracts['relevance_score'] = top_k_abstracts['id'].map(score_dict)
        top_k_abstracts = top_k_abstracts.sort_values('relevance_score', ascending=False)

        return top_k_abstracts

    except Exception as e:
        raise ProcessingError(f"Failed to select top-k papers: {str(e)}")


# ============================================================================
# Text Generation
# ============================================================================

def generate_related_work_text(
    query: str,
    selected_papers: pd.DataFrame,
    generation_model: str,
    stream: bool = True
) -> Union[Tuple[str, GenerationMetadata], 'StreamingResponse']:
    """
    Generate related work section.

    Args:
        query: Research query/abstract
        selected_papers: Selected papers to use for generation
        generation_model: OpenAI model to use
        stream: If True, returns StreamingResponse. If False, returns (text, metadata) tuple

    Returns:
        If stream=False: Tuple of (generated text, GenerationMetadata)
        If stream=True: StreamingResponse with SSE events
    """
    INSTRUCTIONS_RELATED_WORK = """
    You are an expert research assistant who is helping with literature review for a research idea or abstract.
    You will be provided with an abstract or research idea and a list of reference abstracts.
    Your task is to write the related work section of the document using only the provided reference abstracts.
    Please write the related work section creating a cohesive storyline by doing a critical analysis of prior work
    in the reference abstracts comparing the strengths and weaknesses while also motivating the proposed approach.
    You should cite the reference abstracts as [id] whenever you are referring it in the related work.
    Do not write it as Reference #. Do not cite abstract or research Idea.
    Do not include any extra notes or newline characters at the end.
    Do not copy the abstracts of reference papers directly but compare and contrast to the main work concisely.
    Do not provide the output in bullet points or markdown.
    Do not provide references at the end.
    Please cite all the provided reference papers if needed.
    """

    try:
        # Build input
        input_related_work = f"Given the Research Idea or abstract: {query}"
        input_related_work += "\n\n## Given references abstracts list below:"

        for index, item in selected_papers[['id', 'title_abstract']].iterrows():
            input_related_work += f"\n\n[{item['id']}]: {item['title_abstract']}"

        input_related_work += "\n\nWrite the related work section summarizing in a cohesive story prior works relevant to the research idea."
        input_related_work += "\n\n## Related Work:"

        # Generate
        openai_client = openai.OpenAI()
        prompt = [
            {"role": "system", "content": INSTRUCTIONS_RELATED_WORK},
            {"role": "user", "content": input_related_work},
        ]

        if stream:
            # Streaming mode
            response = openai_client.chat.completions.create(
                model=generation_model,
                messages=prompt,
                stream=True
            )

            def event_stream():
                """Generator that yields SSE-formatted events and accumulates text for metadata."""
                accumulated_text = []

                try:
                    for chunk in response:
                        if chunk.choices[0].delta.content:
                            text = chunk.choices[0].delta.content
                            accumulated_text.append(text)
                            # Send the text chunk as SSE event
                            # Escape newlines in the data to maintain SSE format
                            yield f"data: {text}\n\n"

                    # Calculate metadata from accumulated text
                    full_text = ''.join(accumulated_text)
                    citations = re.findall(r'\[(\d+)\]', full_text)
                    unique_citations = sorted(set(int(c) for c in citations))

                    # Extract paper details for cited papers
                    references = []
                    for paper_id in unique_citations:
                        paper = selected_papers[selected_papers['id'] == paper_id]
                        if not paper.empty:
                            references.append({
                                "id": int(paper_id),
                                "title": str(paper.iloc[0]['title']),
                                "abstract": str(paper.iloc[0]['abstract'])
                            })

                    # Send metadata as final event (optional)
                    metadata_dict = {
                        "type": "metadata",
                        "length_chars": len(full_text),
                        "length_words": len(full_text.split()),
                        "total_citations": len(citations),
                        "unique_citations": len(unique_citations),
                        "cited_paper_ids": unique_citations,
                        "references": references
                    }

                    import json
                    yield f"data: [METADATA]{json.dumps(metadata_dict)}\n\n"
                    yield "data: [DONE]\n\n"

                except Exception as e:
                    # Send error event
                    import json
                    error_data = {"type": "error", "message": str(e)}
                    yield f"data: [ERROR]{json.dumps(error_data)}\n\n"

            return StreamingResponse(event_stream(), media_type="text/event-stream")

        else:
            # Non-streaming mode (backward compatibility)
            response = openai_client.chat.completions.create(
                model=generation_model,
                messages=prompt,
                stream=False
            )

            generated_text = response.choices[0].message.content

            # Extract citation information
            citations = re.findall(r'\[(\d+)\]', generated_text)
            unique_citations = sorted(set(int(c) for c in citations))

            metadata = GenerationMetadata(
                length_chars=len(generated_text),
                length_words=len(generated_text.split()),
                total_citations=len(citations),
                unique_citations=len(unique_citations),
                cited_paper_ids=unique_citations
            )

            return generated_text, metadata

    except Exception as e:
        raise ProcessingError(f"Failed to generate related work: {str(e)}")


# ============================================================================
# Output Formatting
# ============================================================================

def format_output_for_file(
    query: str,
    generated_text: str,
    selected_papers: pd.DataFrame
) -> str:
    """
    Format the complete output for saving to file.

    Returns:
        Formatted string ready to write to file
    """
    citations = re.findall(r'\[(\d+)\]', generated_text)
    unique_citations = sorted(set(int(c) for c in citations))

    output = []
    output.append("=" * 80)
    output.append("AUTOMATED LITERATURE REVIEW GENERATION")
    output.append("=" * 80 + "\n")

    output.append("RESEARCH QUERY:")
    output.append("-" * 80)
    output.append(query)
    output.append("\n" + "=" * 80 + "\n")

    output.append("RELATED WORK:")
    output.append("-" * 80)
    output.append(generated_text)
    output.append("\n" + "=" * 80 + "\n")

    output.append("REFERENCES:")
    output.append("-" * 80)
    for paper_id in unique_citations:
        paper = selected_papers[selected_papers['id'] == paper_id]
        if not paper.empty:
            output.append(f"[{paper_id}] {paper.iloc[0]['title']}")
            output.append(f"    {paper.iloc[0]['abstract'][:200]}...\n")

    output.append("=" * 80)

    return "\n".join(output)


# ============================================================================
# Main Pipeline Orchestrator
# ============================================================================

class LiteratureReviewPipeline:
    """
    Main orchestrator for the literature review generation pipeline.

    This class provides a high-level interface to run the entire pipeline
    without any UI concerns. Perfect for use in notebooks, APIs, or testing.
    """

    def __init__(self, config: PipelineConfig):
        """Initialize pipeline with configuration."""
        self.config = config
        self.vector_store = None
        self.all_abstracts = None


    def build_index(self, csv_source: str) -> IndexResult:
        """
        Build or update the vector store index from CSV data.

        This method performs Steps 1-3 of the pipeline:
        1. Load and prepare data from CSV (local path or S3 URI)
        2. Initialize vector store
        3. Process and index documents

        Args:
            csv_source: Path to CSV file (local path or S3 URI starting with s3://)

        Returns:
            IndexResult with indexing statistics and metadata

        Raises:
            ValidationError: If inputs are invalid
            ProcessingError: If processing fails
        """
        # Step 1: Load and prepare data
        # Detect S3 URI vs local path
        if csv_source.startswith('s3://'):
            logger.info(f"Loading CSV from S3: {csv_source}")
            self.all_abstracts, _ = load_abstracts_from_s3_csv(csv_source)
        else:
            logger.info(f"Loading CSV from local path: {csv_source}")
            self.all_abstracts, _ = load_abstracts_from_csv(csv_source)

        self.all_abstracts, _ = prepare_abstracts_for_indexing(
            self.all_abstracts,
            self.config.random_seed
        )

        # Step 2: Initialize vector store (S3 Vectors)
        # Auto-generate session_id if not provided
        if not self.config.session_id:
            timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            unique_id = str(uuid.uuid4())[:8]
            self.config.session_id = f"session-{timestamp}-{unique_id}"

        self.vector_store = initialize_vector_store(
            session_id=self.config.session_id,
            s3_vector_bucket=self.config.s3_vector_bucket,
            recreate_index=self.config.recreate_index,
            storage_mode=self.config.storage_mode
        )

        # Step 3: Process and index documents
        index_metadata = process_and_index_documents(
            vector_store=self.vector_store,
            csv_path=csv_source,
            df=self.all_abstracts,
            s3_data_bucket=self.config.s3_data_bucket
        )

        # Build result
        return IndexResult(
            csv_path=csv_source,
            total_abstracts=len(self.all_abstracts),
            chunks_created=index_metadata['document_count'],
            total_indexed=self.vector_store.get_document_count(),
            session_id=self.config.session_id,
            s3_uri=index_metadata['s3_uri'],
            index_name=index_metadata['index_name'],
            recreated=self.config.recreate_index,
            config=self.config
        )

    def load_abstracts_only(self, csv_path: str) -> Dict[str, Any]:
        """
        Load abstracts from CSV without creating or updating the index.

        This is useful when you want to generate reviews from an existing S3 Vectors
        index but you have the original CSV file. For loading from S3 without CSV,
        use load_abstracts_from_s3() instead.

        Args:
            csv_path: Path to CSV file containing abstracts

        Returns:
            Dict with metadata about loaded abstracts

        Raises:
            ValidationError: If CSV is invalid
        """
        # Load and prepare abstracts (same as build_index steps 1)
        self.all_abstracts, _ = load_abstracts_from_csv(csv_path)
        self.all_abstracts, _ = prepare_abstracts_for_indexing(
            self.all_abstracts,
            self.config.random_seed
        )

        return {
            'csv_path': csv_path,
            'total_abstracts': len(self.all_abstracts),
            'columns': list(self.all_abstracts.columns)
        }

    def load_abstracts_from_s3(self, session_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Load abstracts from S3 data bucket for an existing session.

        This method is useful when you want to generate reviews from an existing
        index without having the original CSV file. It loads papers directly from
        S3 where they were stored during indexing.

        Args:
            session_id: Session identifier (uses config.session_id if not provided)

        Returns:
            Dict with metadata about loaded abstracts

        Raises:
            ProcessingError: If session not found or papers cannot be loaded
            ValueError: If session_id not provided and not in config
        """
        # Determine session_id
        sid = session_id or self.config.session_id
        if not sid:
            raise ValueError(
                "session_id must be provided either as parameter or in config. "
                "Cannot load abstracts from S3 without a session_id."
            )

        # Load papers from S3
        s3_store = S3DocumentStore(bucket_name=self.config.s3_data_bucket)
        self.all_abstracts = s3_store.load_session_papers(session_id=sid)

        # Update config session_id if it was provided as parameter
        if session_id and not self.config.session_id:
            self.config.session_id = session_id

        return {
            'session_id': sid,
            'total_abstracts': len(self.all_abstracts),
            'columns': list(self.all_abstracts.columns),
            's3_data_bucket': self.config.s3_data_bucket
        }

    def retrieve_and_rank_papers(self, query: str) -> Tuple[pd.DataFrame, pd.DataFrame, RetrievalStats, ScoringStats]:
        """
        Retrieve and rank papers for a given query.

        This method performs Steps 4-6 of the pipeline:
        4. Retrieve relevant papers using hybrid search
        5. Score papers for relevance
        6. Select top-k papers

        Args:
            query: Research query/abstract

        Returns:
            Tuple of (top_k_abstracts DataFrame, all_scored_papers DataFrame, RetrievalStats, ScoringStats)

        Raises:
            ProcessingError: If vector store not initialized or abstracts not loaded
        """
        # Ensure vector store is initialized (S3 Vectors)
        logger.debug(f"In retrieve_and_rank_papers, vector_store is initialized: {self.vector_store is not None}")
        if self.vector_store is None:
            # Connect to existing S3 Vectors index
            if not self.config.session_id:
                raise ProcessingError(
                    "session_id not found in config. Cannot connect to existing index without session_id. "
                    "Please run build_index() first or provide session_id in config."
                )

            try:
                logger.info(f"Connecting to existing S3 Vectors index for session: {self.config.session_id}")
                self.vector_store = initialize_vector_store(
                    session_id=self.config.session_id,
                    s3_vector_bucket=self.config.s3_vector_bucket,
                    recreate_index=False,  # Never recreate when retrieving
                    storage_mode=self.config.storage_mode
                )

                # Verify index exists
                if not self.vector_store.index_exists:
                    raise ProcessingError(
                        f"S3 Vectors index '{self.vector_store.index_name}' not found for session '{self.config.session_id}'. "
                        "Please run build_index() first to create the index."
                    )
            except Exception as e:
                raise ProcessingError(
                    f"Failed to connect to S3 Vectors index: {str(e)}. "
                    "Please run build_index() first or verify session_id is correct."
                )

        # Ensure abstracts are loaded
        if self.all_abstracts is None:
            raise ProcessingError(
                "Abstracts not loaded. Please run load_abstracts_from_s3(), load_abstracts_only(), or build_index() first."
            )

        # Log abstracts state before retrieval
        logger.info(f"Abstracts loaded: shape={self.all_abstracts.shape}, columns={list(self.all_abstracts.columns)}")
        logger.info(f"Sample IDs from all_abstracts: {self.all_abstracts['id'].head(5).tolist()}")
        logger.info(f"Has 'title_abstract' column: {'title_abstract' in self.all_abstracts.columns}")

        # Step 4: Retrieve relevant papers
        retrieved_abstracts, retrieval_stats = retrieve_relevant_papers(
            self.vector_store,
            self.all_abstracts,
            query,
            self.config.hybrid_k
        )

        # Log retrieved abstracts state
        logger.info(f"Retrieved abstracts: shape={retrieved_abstracts.shape}, columns={list(retrieved_abstracts.columns)}")
        logger.info(f"Retrieved paper IDs: {retrieved_abstracts['id'].tolist()}")
        logger.info(f"Has 'title_abstract' column: {'title_abstract' in retrieved_abstracts.columns}")

        # Step 5: Score papers for relevance
        relevance_results, scoring_stats = score_papers_relevance(
            retrieved_abstracts,
            query,
            self.config.relevance_model,
            self.config.num_abstracts_to_score
        )

        # Create DataFrame with ALL scored papers
        all_scores = [(abs.id, abs.probability_score) for abs in relevance_results]
        sorted_all_scores = sorted(all_scores, key=lambda x: x[1], reverse=True)

        all_scored_ids = [id for id, score in sorted_all_scores]
        all_scored_papers = retrieved_abstracts[retrieved_abstracts['id'].isin(all_scored_ids)].copy()

        # Add scores to all papers
        score_dict_all = {id: score for id, score in sorted_all_scores}
        all_scored_papers['relevance_score'] = all_scored_papers['id'].map(score_dict_all)
        all_scored_papers = all_scored_papers.sort_values('relevance_score', ascending=False)

        # Step 6: Select top-k papers
        top_k_abstracts = select_top_papers(
            relevance_results,
            retrieved_abstracts,
            self.config.top_k
        )

        return top_k_abstracts, all_scored_papers, retrieval_stats, scoring_stats

    def generate_review(self, query: str) -> PipelineResult:
        """
        Generate a literature review from an existing index.

        This method performs Steps 4-7 of the pipeline:
        4. Retrieve relevant papers using hybrid search
        5. Score papers for relevance
        6. Select top-k papers
        7. Generate related work text

        Requires that build_index() has been called first, or that the
        vector store already exists at the configured persist_directory.

        Args:
            query: Research query/abstract

        Returns:
            PipelineResult with all outputs and metadata

        Raises:
            ProcessingError: If vector store not initialized or generation fails
        """
        # Steps 4-6: Retrieve and rank papers
        top_k_abstracts, _, retrieval_stats, scoring_stats = self.retrieve_and_rank_papers(query)

        # Step 7: Generate related work text
        generated_text, generation_metadata = generate_related_work_text(
            query,
            top_k_abstracts,
            self.config.generation_model,
            stream=False
        )

        # Return complete result
        return PipelineResult(
            query=query,
            generated_text=generated_text,
            top_k_abstracts=top_k_abstracts,
            retrieval_stats=retrieval_stats,
            scoring_stats=scoring_stats,
            generation_metadata=generation_metadata,
            all_abstracts=self.all_abstracts,
            config=self.config
        )

    def generate_review_stream(self, query: str) -> StreamingResponse:
        """
        Generate a literature review with streaming response.

        This method performs Steps 4-7 of the pipeline:
        4. Retrieve relevant papers using hybrid search
        5. Score papers for relevance
        6. Select top-k papers
        7. Generate related work text (streaming)

        Requires that build_index() has been called first, or that the
        vector store already exists at the configured persist_directory.

        Args:
            query: Research query/abstract

        Returns:
            StreamingResponse with SSE events

        Raises:
            ProcessingError: If vector store not initialized or generation fails
        """
        # Steps 4-6: Retrieve and rank papers
        top_k_abstracts, _, retrieval_stats, scoring_stats = self.retrieve_and_rank_papers(query)

        # Step 7: Generate related work text (streaming mode)
        streaming_response = generate_related_work_text(
            query,
            top_k_abstracts,
            self.config.generation_model,
            stream=True
        )

        return streaming_response

    def run(self, csv_path: str, query: str) -> PipelineResult:
        """
        Execute the complete literature review generation pipeline.

        This is a convenience method that combines build_index() and generate_review()
        for backward compatibility and ease of use.

        Args:
            csv_path: Path to CSV file containing abstracts
            query: Research query/abstract

        Returns:
            PipelineResult with all outputs and metadata

        Raises:
            ValidationError: If inputs are invalid
            ProcessingError: If processing fails
        """
        # Build index (Steps 1-3)
        self.build_index(csv_path)

        # Generate review (Steps 4-7)
        return self.generate_review(query)
