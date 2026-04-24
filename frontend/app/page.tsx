"use client"

import { useState, FormEvent, useRef, DragEvent, ChangeEvent, useEffect } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import remarkBreaks from 'remark-breaks';
import { fetchEventSource, EventSourceMessage } from '@microsoft/fetch-event-source';

interface IndexStats {
  total_abstracts: number;
  chunks_created: number;
  total_indexed: number;
}

interface RankedPaper {
  id: number;
  title: string;
  abstract: string;
  relevance_score: number;
}

interface RetrievalStats {
  total_papers_in_corpus: number;
  papers_retrieved: number;
  retrieval_rate: number;
  retrieval_k: number;
}

interface ScoringStats {
  papers_scored: number;
  mean_score: number;
  std_score: number;
  min_score: number;
  max_score: number;
  median_score: number;
}

interface Citation {
  id: number;
  title: string;
  abstract: string;
}

export default function Home() {
  const [researchIdea, setResearchIdea] = useState('');
  const [uploadedFile, setUploadedFile] = useState<File | null>(null);
  const [isDragging, setIsDragging] = useState(false);
  const [fileError, setFileError] = useState<string>('');
  const [isIndexing, setIsIndexing] = useState(false);
  const [indexStats, setIndexStats] = useState<IndexStats | null>(null);
  const [indexError, setIndexError] = useState<string>('');
  const [isRanking, setIsRanking] = useState(false);
  const [rankingLoadingMessage, setRankingLoadingMessage] = useState<string>('Retrieving and ranking papers...');
  const [rankedPapers, setRankedPapers] = useState<RankedPaper[] | null>(null);
  const [allScoredPapers, setAllScoredPapers] = useState<RankedPaper[] | null>(null);
  const [rankingStats, setRankingStats] = useState<{retrieval: RetrievalStats, scoring: ScoringStats} | null>(null);
  const [rankingError, setRankingError] = useState<string>('');
  const [hybridK, setHybridK] = useState<number>(50);
  const [selectionMode, setSelectionMode] = useState<'top_k' | 'min_score'>('top_k');
  const [customTopK, setCustomTopK] = useState<number>(3);
  const [minScore, setMinScore] = useState<number>(0);
  const [selectedPapersForGeneration, setSelectedPapersForGeneration] = useState<RankedPaper[]>([]);
  const fileInputRef = useRef<HTMLInputElement>(null);

  // Generation-related states
  const [generatedText, setGeneratedText] = useState<string>('');
  const [isGenerating, setIsGenerating] = useState<boolean>(false);
  const [generateError, setGenerateError] = useState<string>('');
  const [citations, setCitations] = useState<Citation[]>([]);

  const [sessionId, setSessionId] = useState<string | null>(null);

  const API_URL = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';
  const GENERATE_URL = process.env.NEXT_PUBLIC_GENERATE_URL || `${API_URL}/api/generate`;

  // Filter papers based on selection mode and criteria
  useEffect(() => {
    if (!allScoredPapers || allScoredPapers.length === 0) {
      setSelectedPapersForGeneration([]);
      return;
    }

    if (selectionMode === 'top_k') {
      // Select top N papers
      const selected = allScoredPapers.slice(0, Math.min(customTopK, allScoredPapers.length));
      setSelectedPapersForGeneration(selected);
    } else {
      // Filter papers by minimum score
      const selected = allScoredPapers.filter(paper => paper.relevance_score >= minScore);
      setSelectedPapersForGeneration(selected);
    }
  }, [allScoredPapers, selectionMode, customTopK, minScore]);

  const validateFile = (file: File): boolean => {
    // Reset error
    setFileError('');

    // Check if it's a CSV file
    if (!file.name.toLowerCase().endsWith('.csv')) {
      setFileError('Please upload a CSV file');
      return false;
    }

    // Check file size (max 10MB)
    if (file.size > 10 * 1024 * 1024) {
      setFileError('File size must be less than 10MB');
      return false;
    }

    return true;
  };

  const handleFile = (file: File) => {
    if (validateFile(file)) {
      setUploadedFile(file);
      setFileError('');
    }
  };

  const handleDragOver = (e: DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    e.stopPropagation();
    setIsDragging(true);
  };

  const handleDragLeave = (e: DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    e.stopPropagation();
    setIsDragging(false);
  };

  const handleDrop = (e: DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    e.stopPropagation();
    setIsDragging(false);

    const files = e.dataTransfer.files;
    if (files && files.length > 0) {
      handleFile(files[0]);
    }
  };

  const handleFileInputChange = (e: ChangeEvent<HTMLInputElement>) => {
    const files = e.target.files;
    if (files && files.length > 0) {
      handleFile(files[0]);
    }
  };

  const handleBrowseClick = () => {
    fileInputRef.current?.click();
  };

  const handleRemoveFile = () => {
    setUploadedFile(null);
    setFileError('');
    setIndexStats(null);
    setIndexError('');
    setSessionId(null);
    setRankedPapers(null);
    setAllScoredPapers(null);
    setRankingStats(null);
    setRankingError('');
    setSelectedPapersForGeneration([]);
    setGeneratedText('');
    setCitations([]);
    setGenerateError('');
    if (fileInputRef.current) {
      fileInputRef.current.value = '';
    }
  };

  const handleRetrieveAndRank = async () => {
    if (!researchIdea.trim()) {
      setRankingError('Please enter a research idea first');
      return;
    }

    setIsRanking(true);
    setRankingError('');
    setRankedPapers(null);
    setAllScoredPapers(null);
    setRankingStats(null);
    setRankingLoadingMessage('Retrieving and ranking papers...');

    // Progressive loading messages
    const timer1 = setTimeout(() => {
      setRankingLoadingMessage('Still processing... This may take a moment for large datasets.');
    }, 5000); // After 5 seconds

    const timer2 = setTimeout(() => {
      setRankingLoadingMessage('This is taking longer than expected. Please wait...');
    }, 15000); // After 15 seconds

    try {
      // Step 1: Kick off ranking (returns 202 immediately)
      const response = await fetch(`${API_URL}/api/retrieve-and-rank`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: sessionId, research_idea: researchIdea, hybrid_k: hybridK }),
      });

      if (!response.ok) {
        const error = await response.json();
        throw new Error(error.error || error.detail || 'Failed to retrieve and rank papers');
      }

      // Step 2: Poll status until RANKED or ERROR
      const POLL_INTERVAL = 5000;
      const MAX_WAIT = 10 * 60 * 1000; // 10 min
      const start = Date.now();

      while (Date.now() - start < MAX_WAIT) {
        await new Promise(r => setTimeout(r, POLL_INTERVAL));

        const statusRes = await fetch(`${API_URL}/api/session/${sessionId}/status`);
        if (!statusRes.ok) continue;
        const status = await statusRes.json();

        if (status.status === 'RANKED') {
          // Fetch ranked papers from the session status or a dedicated endpoint
          // The agent stores results in S3; we need to get them back via a session data endpoint
          // For now, poll retrieve-and-rank again which returns the cached result
          const rankRes = await fetch(`${API_URL}/api/session/${sessionId}/ranked-papers`);
          if (rankRes.ok) {
            const rankData = await rankRes.json();
            setRankedPapers(rankData.top_k_papers);
            setAllScoredPapers(rankData.all_scored_papers);
            setRankingStats({ retrieval: rankData.retrieval_stats, scoring: rankData.scoring_stats });
          }
          return;
        }

        if (status.status === 'ERROR') {
          throw new Error(status.error_message || 'Ranking failed');
        }
        // RANKING — keep polling
      }

      throw new Error('Ranking timed out. Please try again.');
    } catch (error) {
      setRankingError(error instanceof Error ? error.message : 'Failed to retrieve and rank papers');
    } finally {
      clearTimeout(timer1);
      clearTimeout(timer2);
      setIsRanking(false);
    }
  };

  const handleUploadAndIndex = async () => {
    if (!uploadedFile) return;

    setIsIndexing(true);
    setIndexError('');

    const formData = new FormData();
    formData.append('file', uploadedFile);

    try {
      // Step 1: Upload — returns 202 immediately with session_id
      const response = await fetch(`${API_URL}/api/upload-and-index`, {
        method: 'POST',
        body: formData,
      });

      if (!response.ok) {
        const error = await response.json();
        throw new Error(error.error || error.detail || 'Failed to index file');
      }

      const result = await response.json();
      const sid = result.session_id;
      setSessionId(sid);

      // Step 2: Poll status until INDEXED or ERROR
      const POLL_INTERVAL = 4000; // 4s
      const MAX_WAIT = 15 * 60 * 1000; // 15 min
      const start = Date.now();

      while (Date.now() - start < MAX_WAIT) {
        await new Promise(r => setTimeout(r, POLL_INTERVAL));

        const statusRes = await fetch(`${API_URL}/api/session/${sid}/status`);
        if (!statusRes.ok) continue;

        const status = await statusRes.json();

        if (status.status === 'INDEXED') {
          setIndexStats({
            total_abstracts: status.total_abstracts ?? 0,
            chunks_created: status.chunks_created ?? 0,
            total_indexed: status.total_abstracts ?? 0,
          });
          return;
        }

        if (status.status === 'ERROR') {
          throw new Error(status.error_message || 'Indexing failed');
        }
        // status is INDEXING — keep polling
      }

      throw new Error('Indexing timed out. Please try again.');
    } catch (error) {
      setIndexError(error instanceof Error ? error.message : 'Failed to upload and index file');
    } finally {
      setIsIndexing(false);
    }
  };

  const formatFileSize = (bytes: number): string => {
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
    return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
  };

  const handleGenerate = async () => {
    setIsGenerating(true);
    setGenerateError('');
    setGeneratedText('');
    setCitations([]);

    const controller = new AbortController();

    try {
      await fetchEventSource(GENERATE_URL, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          session_id: sessionId,
          research_idea: researchIdea,
          selected_paper_ids: selectedPapersForGeneration.map(p => p.id)
        }),
        signal: controller.signal,

        async onopen(response) {
          if (response.ok) {
            return; // Success
          } else if (response.status >= 400 && response.status < 500 && response.status !== 429) {
            // Client error - don't retry
            const errorData = await response.json();
            throw new Error(errorData.detail || 'Failed to generate review');
          } else {
            // Server error or rate limit - could retry
            throw new Error('Server error occurred');
          }
        },

        onmessage(event: EventSourceMessage) {
          const data = event.data;

          // Check for special messages
          if (data.startsWith('[METADATA]')) {
            // Handle metadata and extract citations
            try {
              const metadata = JSON.parse(data.substring(10));
              console.log('Generation metadata:', metadata);
              if (metadata.references && Array.isArray(metadata.references)) {
                setCitations(metadata.references);
              }
            } catch (_e) {
              console.error('Failed to parse metadata:', _e);
            }
          } else if (data === '[DONE]') {
            // Stream complete
            setIsGenerating(false);
          } else if (data.startsWith('[ERROR]')) {
            // Error occurred
            try {
              const errorData = JSON.parse(data.substring(7));
              setGenerateError(errorData.message || 'An error occurred during generation');
            } catch {
              setGenerateError('An error occurred during generation');
            }
            setIsGenerating(false);
          } else {
            // Regular text chunk - append to generatedText
            setGeneratedText(prev => prev + data);
          }
        },

        onerror(err: unknown) {
          setIsGenerating(false);
          const errorMessage = err instanceof Error ? err.message : 'Connection error. Please try again.';
          setGenerateError(errorMessage);
          throw err; // Stop retrying
        },

        onclose() {
          // Stream closed
          setIsGenerating(false);
        }
      });
    } catch (err: unknown) {
      setIsGenerating(false);
      if (err instanceof Error && err.name !== 'AbortError') {
        setGenerateError(err.message || 'Failed to generate review');
      }
    }
  };

  const downloadAsMarkdown = () => {
    // Build markdown content
    const currentDate = new Date().toLocaleDateString('en-US', {
      year: 'numeric',
      month: 'long',
      day: 'numeric'
    });

    let markdown = `# Related Work\n\n`;
    markdown += `Generated on ${currentDate}\n\n`;
    markdown += `${generatedText}\n\n`;

    // Add references section if citations exist
    if (citations.length > 0) {
      markdown += `## References\n\n`;
      citations.forEach((citation) => {
        markdown += `- [${citation.id}] **${citation.title}**\n`;
        markdown += `  ${citation.abstract}\n\n`;
      });
    }

    // Create blob and download
    const blob = new Blob([markdown], { type: 'text/markdown' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `related-work-${new Date().toISOString().split('T')[0]}.md`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  };

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();

    // Check if papers have been ranked and selected
    if (!allScoredPapers || allScoredPapers.length === 0) {
      setRankingError('Please retrieve and rank papers first before generating');
      return;
    }

    if (!selectedPapersForGeneration || selectedPapersForGeneration.length === 0) {
      setRankingError('Please select at least one paper for generation');
      return;
    }

    // Trigger generation
    handleGenerate();
  };

  return (
    <div className="flex flex-col min-h-screen">
      {/* Header */}
      <header className="flex items-center px-6 py-4 border-b border-black/10 dark:border-white/10">
        <div className="flex items-center gap-3">
          <div className="w-8 h-8 text-primary">
            <svg fill="none" viewBox="0 0 48 48" xmlns="http://www.w3.org/2000/svg">
              <path d="M42.4379 44C42.4379 44 36.0744 33.9038 41.1692 24C46.8624 12.9336 42.2078 4 42.2078 4L7.01134 4C7.01134 4 11.6577 12.932 5.96912 23.9969C0.876273 33.9029 7.27094 44 7.27094 44L42.4379 44Z" fill="currentColor"></path>
            </svg>
          </div>
          <h1 className="text-lg font-bold text-black dark:text-white">
            ResearchAI
          </h1>
        </div>
      </header>

      {/* Main Content - Split Panel Layout */}
      <main className="flex-1 flex flex-col lg:flex-row overflow-hidden">
        {/* Left Panel - Input Form */}
        <div className="w-full lg:w-1/2 p-6 overflow-y-auto border-b lg:border-b-0 lg:border-r border-black/10 dark:border-white/10">
          <div className="max-w-2xl mx-auto space-y-8">
            {/* Hero Section */}
            <div className="text-center lg:text-left">
              <h2 className="text-3xl font-bold text-black dark:text-white">
                Generate Related Work
              </h2>
              <p className="mt-2 text-black/60 dark:text-white/60">
                Automatically generate a Related Work section with inline citations from your paper corpus.
              </p>
            </div>

            {/* App Info Panel */}
            <div className="rounded-xl border border-black/10 dark:border-white/10 bg-black/2 dark:bg-white/3 p-5 space-y-4 text-sm">
              {/* Steps */}
              <div>
                <p className="font-semibold text-black dark:text-white mb-2">How it works</p>
                <ol className="space-y-1 text-black/70 dark:text-white/70">
                  <li className="flex items-start gap-2">
                    <span className="font-mono text-xs bg-primary/10 text-primary px-1.5 py-0.5 rounded mt-0.5 flex-shrink-0">1</span>
                    <span><span className="font-medium text-black dark:text-white">Upload &amp; Index</span> — Upload a CSV of papers; they are embedded and indexed for search.</span>
                  </li>
                  <li className="flex items-start gap-2">
                    <span className="font-mono text-xs bg-primary/10 text-primary px-1.5 py-0.5 rounded mt-0.5 flex-shrink-0">2</span>
                    <span><span className="font-medium text-black dark:text-white">Retrieve &amp; Rank</span> — Enter your research idea; relevant papers are retrieved and AI-scored for relevance.</span>
                  </li>
                  <li className="flex items-start gap-2">
                    <span className="font-mono text-xs bg-primary/10 text-primary px-1.5 py-0.5 rounded mt-0.5 flex-shrink-0">3</span>
                    <span><span className="font-medium text-black dark:text-white">Generate</span> — Select papers and generate a cohesive Related Work section with <code className="font-mono text-xs bg-black/5 dark:bg-white/10 px-1 rounded">[id]</code> citations.</span>
                  </li>
                </ol>
              </div>

              {/* CSV format */}
              <div className="border-t border-black/8 dark:border-white/8 pt-4">
                <p className="font-semibold text-black dark:text-white mb-1">CSV format required</p>
                <p className="text-black/60 dark:text-white/60">
                  Your file must have these columns:{' '}
                  <code className="font-mono text-xs bg-black/5 dark:bg-white/10 px-1 py-0.5 rounded">id</code>{' '}
                  <code className="font-mono text-xs bg-black/5 dark:bg-white/10 px-1 py-0.5 rounded">title</code>{' '}
                  <code className="font-mono text-xs bg-black/5 dark:bg-white/10 px-1 py-0.5 rounded">abstract</code>.
                  IDs must be unique integers. Max file size: 50 MB. Max papers: 300.
                </p>
              </div>

              {/* Limitations */}
              <div className="border-t border-black/8 dark:border-white/8 pt-4">
                <p className="font-semibold text-black dark:text-white mb-1">Limitations</p>
                <ul className="space-y-1 text-black/60 dark:text-white/60 list-disc list-inside">
                  <li>Works only from the abstracts you provide — no internet search.</li>
                  <li>Quality depends on the breadth and relevance of your uploaded corpus.</li>
                  <li>Session data is stored for 24 hours and then automatically deleted.</li>
                </ul>
              </div>
            </div>

            {/* Form */}
            <form onSubmit={handleSubmit} className="space-y-6">
            {/* Research Idea Input */}
            <div>
              <label
                className="block text-sm font-medium text-black dark:text-white mb-2"
                htmlFor="research-idea"
              >
                Your Research Idea
              </label>
              <textarea
                className="w-full h-36 p-4 rounded-lg bg-white dark:bg-black/20 border border-black/10 dark:border-white/10 focus:ring-2 focus:ring-primary focus:border-primary transition duration-200 resize-none text-black dark:text-white placeholder:text-black/40 dark:placeholder:text-white/40"
                id="research-idea"
                placeholder="e.g., 'Using large language models to summarize legal documents'"
                value={researchIdea}
                onChange={(e) => setResearchIdea(e.target.value)}
                required
              ></textarea>
            </div>

            {/* File Upload Area */}
            <div>
              <label className="block text-sm font-medium text-black dark:text-white mb-2">
                References (CSV File)
              </label>
              <div
                onDragOver={handleDragOver}
                onDragLeave={handleDragLeave}
                onDrop={handleDrop}
                className={`relative flex flex-col items-center justify-center p-8 border-2 border-dashed rounded-xl text-center transition-all ${
                  isDragging
                    ? 'border-primary bg-primary/5 dark:bg-primary/10'
                    : uploadedFile
                    ? 'border-primary/50 bg-primary/5 dark:bg-primary/10'
                    : 'border-black/20 dark:border-white/20 hover:border-primary/50'
                }`}
              >
                {!uploadedFile ? (
                  <>
                    <span className="material-symbols-outlined text-4xl text-black/40 dark:text-white/40 mb-4">
                      upload_file
                    </span>
                    <h3 className="text-lg font-semibold text-black dark:text-white">
                      {isDragging ? 'Drop your CSV file here' : 'Drag and drop a CSV file'}
                    </h3>
                    <p className="mt-1 text-sm text-black/60 dark:text-white/60">
                      The file should contain columns: id, title, abstract
                    </p>
                    <p className="mt-4 text-sm text-black/60 dark:text-white/60">or</p>
                    <button
                      type="button"
                      onClick={handleBrowseClick}
                      className="mt-4 px-6 py-2.5 text-sm font-semibold text-white rounded-lg transition-colors shadow-sm"
                      style={{ backgroundColor: '#1173d4' }}
                      onMouseEnter={(e) => (e.currentTarget.style.backgroundColor = '#0d5aa8')}
                      onMouseLeave={(e) => (e.currentTarget.style.backgroundColor = '#1173d4')}
                    >
                      Browse Files
                    </button>
                    <input
                      ref={fileInputRef}
                      type="file"
                      accept=".csv"
                      onChange={handleFileInputChange}
                      className="hidden"
                    />
                  </>
                ) : (
                  <div className="w-full space-y-4">
                    <div className="flex items-center justify-between bg-white dark:bg-black/20 rounded-lg p-4 border border-primary/30">
                      <div className="flex items-center gap-3 flex-1 min-w-0">
                        <span className="material-symbols-outlined text-2xl text-primary flex-shrink-0">
                          description
                        </span>
                        <div className="flex-1 min-w-0 text-left">
                          <p className="text-sm font-medium text-black dark:text-white truncate">
                            {uploadedFile.name}
                          </p>
                          <p className="text-xs text-black/60 dark:text-white/60">
                            {formatFileSize(uploadedFile.size)}
                          </p>
                        </div>
                      </div>
                      <button
                        type="button"
                        onClick={handleRemoveFile}
                        disabled={isIndexing}
                        className="ml-3 p-2 text-black/60 dark:text-white/60 hover:text-red-500 dark:hover:text-red-400 transition-colors flex-shrink-0 disabled:opacity-50"
                        title="Remove file"
                      >
                        <span className="material-symbols-outlined text-xl">close</span>
                      </button>
                    </div>

                    {!indexStats && !isIndexing && (
                      <button
                        type="button"
                        onClick={handleUploadAndIndex}
                        disabled={isIndexing}
                        className="w-full px-6 py-3 text-sm font-semibold text-white rounded-lg transition-colors shadow-sm flex items-center justify-center gap-2"
                        style={{ backgroundColor: '#1173d4' }}
                        onMouseEnter={(e) => !isIndexing && (e.currentTarget.style.backgroundColor = '#0d5aa8')}
                        onMouseLeave={(e) => !isIndexing && (e.currentTarget.style.backgroundColor = '#1173d4')}
                      >
                        <span className="material-symbols-outlined">cloud_upload</span>
                        Upload & Index File
                      </button>
                    )}

                    {isIndexing && (
                      <div className="flex items-center justify-center gap-3 p-4 bg-blue-50 dark:bg-blue-900/20 rounded-lg">
                        <div className="w-5 h-5 border-2 border-primary border-t-transparent rounded-full animate-spin"></div>
                        <span className="text-sm font-medium text-primary">Uploading and indexing... this may take a minute.</span>
                      </div>
                    )}

                    {indexStats && (
                      <div className="p-4 bg-green-50 dark:bg-green-900/20 rounded-lg border border-green-200 dark:border-green-800">
                        <div className="flex items-start gap-2">
                          <span className="material-symbols-outlined text-green-600 dark:text-green-400 flex-shrink-0">check_circle</span>
                          <div className="flex-1">
                            <p className="text-sm font-semibold text-green-800 dark:text-green-200 mb-2">
                              Index created successfully!
                            </p>
                            <div className="space-y-1 text-xs text-green-700 dark:text-green-300">
                              <p>Papers indexed: {indexStats.total_abstracts}</p>
                              <p>Chunks created: {indexStats.chunks_created}</p>
                              <p>Total documents: {indexStats.total_indexed}</p>
                            </div>
                          </div>
                        </div>
                      </div>
                    )}

                    <button
                      type="button"
                      onClick={handleBrowseClick}
                      disabled={isIndexing}
                      className="text-sm text-primary hover:text-primary/80 font-medium disabled:opacity-50"
                    >
                      Choose a different file
                    </button>
                    <input
                      ref={fileInputRef}
                      type="file"
                      accept=".csv"
                      onChange={handleFileInputChange}
                      className="hidden"
                    />
                  </div>
                )}
              </div>
              {fileError && (
                <p className="mt-2 text-sm text-red-500 dark:text-red-400 flex items-center gap-1">
                  <span className="material-symbols-outlined text-base">error</span>
                  {fileError}
                </p>
              )}
              {indexError && (
                <p className="mt-2 text-sm text-red-500 dark:text-red-400 flex items-center gap-1">
                  <span className="material-symbols-outlined text-base">error</span>
                  {indexError}
                </p>
              )}
            </div>

            {/* Retrieve & Rank Papers Section */}
            {indexStats && (
              <div>
                <label className="block text-sm font-medium text-black dark:text-white mb-2">
                  Step 2: Retrieve & Rank Relevant Papers
                </label>

                {/* Hybrid K Control */}
                <div className="mb-4 p-4 bg-white dark:bg-black/20 rounded-lg border border-black/10 dark:border-white/10">
                  <label className="block text-sm font-medium text-black dark:text-white mb-2">
                    Number of papers to retrieve (Hybrid K)
                  </label>
                  <input
                    type="number"
                    min="1"
                    max="200"
                    value={hybridK}
                    onChange={(e) => {
                      const value = parseInt(e.target.value);
                      if (!isNaN(value) && value >= 1 && value <= 200) {
                        setHybridK(value);
                      }
                    }}
                    className="w-full px-3 py-2 rounded-lg bg-white dark:bg-black/20 border border-black/10 dark:border-white/10 focus:ring-2 focus:ring-primary focus:border-primary transition duration-200 text-black dark:text-white"
                  />
                  <p className="mt-1 text-xs text-black/60 dark:text-white/60">
                    Controls how many papers are retrieved before ranking (default: 50, range: 1-200)
                  </p>
                </div>

                {!rankedPapers && !isRanking && (
                  <button
                    type="button"
                    onClick={handleRetrieveAndRank}
                    disabled={isRanking || !researchIdea.trim()}
                    className="w-full px-6 py-3 text-sm font-semibold text-white rounded-lg transition-colors shadow-sm flex items-center justify-center gap-2 disabled:opacity-50 disabled:cursor-not-allowed"
                    style={{ backgroundColor: '#1173d4' }}
                    onMouseEnter={(e) => !isRanking && researchIdea.trim() && (e.currentTarget.style.backgroundColor = '#0d5aa8')}
                    onMouseLeave={(e) => !isRanking && researchIdea.trim() && (e.currentTarget.style.backgroundColor = '#1173d4')}
                  >
                    <span className="material-symbols-outlined">search</span>
                    Retrieve & Rank Papers
                  </button>
                )}

                {isRanking && (
                  <div className="flex items-center justify-center gap-3 p-4 bg-blue-50 dark:bg-blue-900/20 rounded-lg">
                    <div className="w-5 h-5 border-2 border-primary border-t-transparent rounded-full animate-spin"></div>
                    <span className="text-sm font-medium text-primary">{rankingLoadingMessage}</span>
                  </div>
                )}

                {allScoredPapers && allScoredPapers.length > 0 && (
                  <div className="space-y-4">
                    <div className="p-4 bg-green-50 dark:bg-green-900/20 rounded-lg border border-green-200 dark:border-green-800">
                      <div className="flex items-start gap-2 mb-3">
                        <span className="material-symbols-outlined text-green-600 dark:text-green-400 flex-shrink-0">check_circle</span>
                        <p className="text-sm font-semibold text-green-800 dark:text-green-200">
                          Papers retrieved and ranked successfully!
                        </p>
                      </div>

                      {rankingStats && (
                        <div className="grid grid-cols-2 gap-3 text-xs text-green-700 dark:text-green-300 mb-3">
                          <div>
                            <p className="font-medium">Retrieval Stats:</p>
                            <p>Retrieved: {rankingStats.retrieval.papers_retrieved} / {rankingStats.retrieval.total_papers_in_corpus}</p>
                            <p>Rate: {rankingStats.retrieval.retrieval_rate.toFixed(1)}%</p>
                          </div>
                          <div>
                            <p className="font-medium">Scoring Stats:</p>
                            <p>Mean: {rankingStats.scoring.mean_score.toFixed(1)}</p>
                            <p>Range: {rankingStats.scoring.min_score.toFixed(1)} - {rankingStats.scoring.max_score.toFixed(1)}</p>
                          </div>
                        </div>
                      )}

                      <div className="space-y-2">
                        {/* Paper Selection Controls */}
                        <div className="p-3 bg-white dark:bg-black/30 rounded-lg border border-green-300/50 dark:border-green-700/50">
                          <p className="text-xs font-semibold text-green-800 dark:text-green-200 mb-3">
                            Select papers for generation:
                          </p>

                          {/* Mode Toggle */}
                          <div className="flex gap-2 mb-3">
                            <button
                              type="button"
                              onClick={() => setSelectionMode('top_k')}
                              className={`flex-1 px-3 py-2 text-xs font-semibold rounded-lg transition-colors ${
                                selectionMode === 'top_k'
                                  ? 'bg-primary text-white'
                                  : 'bg-white dark:bg-black/20 text-black/70 dark:text-white/70 border border-black/10 dark:border-white/10'
                              }`}
                            >
                              Top K Papers
                            </button>
                            <button
                              type="button"
                              onClick={() => setSelectionMode('min_score')}
                              className={`flex-1 px-3 py-2 text-xs font-semibold rounded-lg transition-colors ${
                                selectionMode === 'min_score'
                                  ? 'bg-primary text-white'
                                  : 'bg-white dark:bg-black/20 text-black/70 dark:text-white/70 border border-black/10 dark:border-white/10'
                              }`}
                            >
                              Min Score
                            </button>
                          </div>

                          {/* Top K Slider */}
                          {selectionMode === 'top_k' && (
                            <div className="space-y-2">
                              <div className="flex items-center justify-between">
                                <label className="text-xs font-medium text-green-800 dark:text-green-200">
                                  Number of papers: {customTopK}
                                </label>
                                <span className="text-xs text-green-700 dark:text-green-300">
                                  {selectedPapersForGeneration.length} selected
                                </span>
                              </div>
                              <input
                                type="range"
                                min="1"
                                max={allScoredPapers.length}
                                value={customTopK}
                                onChange={(e) => setCustomTopK(parseInt(e.target.value))}
                                className="w-full h-2 bg-green-200 dark:bg-green-800/50 rounded-lg appearance-none cursor-pointer accent-primary"
                              />
                            </div>
                          )}

                          {/* Min Score Slider */}
                          {selectionMode === 'min_score' && (
                            <div className="space-y-2">
                              <div className="flex items-center justify-between">
                                <label className="text-xs font-medium text-green-800 dark:text-green-200">
                                  Minimum score: {minScore}
                                </label>
                                <span className="text-xs text-green-700 dark:text-green-300">
                                  {selectedPapersForGeneration.length} selected
                                </span>
                              </div>
                              <input
                                type="range"
                                min="0"
                                max="100"
                                value={minScore}
                                onChange={(e) => setMinScore(parseInt(e.target.value))}
                                className="w-full h-2 bg-green-200 dark:bg-green-800/50 rounded-lg appearance-none cursor-pointer accent-primary"
                              />
                            </div>
                          )}
                        </div>

                        <div className="flex items-center justify-between mb-2">
                          <p className="text-xs font-semibold text-green-800 dark:text-green-200">
                            All Retrieved Papers ({allScoredPapers.length} total, {selectedPapersForGeneration.length} selected for generation):
                          </p>
                        </div>

                        {/* Scrollable container for all papers */}
                        <div className="max-h-96 overflow-y-auto space-y-2 pr-2">
                          {allScoredPapers.map((paper, index) => {
                            const isSelected = selectedPapersForGeneration.some(p => p.id === paper.id);
                            return (
                              <div
                                key={paper.id}
                                className={`p-3 rounded-lg border ${
                                  isSelected
                                    ? 'bg-primary/5 dark:bg-primary/10 border-primary/30 ring-1 ring-primary/20'
                                    : 'bg-white dark:bg-black/20 border-green-200/50 dark:border-green-800/50'
                                }`}
                              >
                                <div className="flex items-start gap-2 mb-2">
                                  <div className="flex flex-wrap items-center gap-1.5">
                                    <span className="text-xs font-mono bg-black/5 dark:bg-white/5 text-black/70 dark:text-white/70 px-2 py-0.5 rounded">
                                      ID: {paper.id}
                                    </span>
                                    <span className="text-xs font-mono text-black/50 dark:text-white/50">
                                      Rank #{index + 1}
                                    </span>
                                    {isSelected && (
                                      <span className="text-xs font-semibold bg-primary text-white px-2 py-0.5 rounded">
                                        Selected
                                      </span>
                                    )}
                                  </div>
                                  <span className="text-xs font-mono text-primary whitespace-nowrap ml-auto">
                                    Score: {paper.relevance_score.toFixed(1)}
                                  </span>
                                </div>
                                <p className="text-xs font-semibold text-black dark:text-white mb-1">
                                  {paper.title}
                                </p>
                                <p className="text-xs text-black/60 dark:text-white/60 line-clamp-2">
                                  {paper.abstract}
                                </p>
                              </div>
                            );
                          })}
                        </div>
                      </div>
                    </div>

                    <button
                      type="button"
                      onClick={handleRetrieveAndRank}
                      className="w-full px-6 py-3 text-sm font-semibold text-white rounded-lg transition-colors shadow-sm flex items-center justify-center gap-2"
                      style={{ backgroundColor: '#1173d4' }}
                      onMouseEnter={(e) => (e.currentTarget.style.backgroundColor = '#0d5aa8')}
                      onMouseLeave={(e) => (e.currentTarget.style.backgroundColor = '#1173d4')}
                    >
                      <span className="material-symbols-outlined">refresh</span>
                      Re-rank with Different Query
                    </button>
                  </div>
                )}

                {rankingError && (
                  <p className="mt-2 text-sm text-red-500 dark:text-red-400 flex items-center gap-1">
                    <span className="material-symbols-outlined text-base">error</span>
                    {rankingError}
                  </p>
                )}
              </div>
            )}

            {/* Generate Button */}
            <div className="flex justify-end">
              <button
                type="submit"
                disabled={!selectedPapersForGeneration || selectedPapersForGeneration.length === 0 || isGenerating}
                className="w-full sm:w-auto px-8 py-3 text-base font-bold text-white rounded-lg transition-colors flex items-center justify-center gap-2 disabled:opacity-50 disabled:cursor-not-allowed"
                style={{ backgroundColor: '#1173d4' }}
                onMouseEnter={(e) => selectedPapersForGeneration && selectedPapersForGeneration.length > 0 && !isGenerating && (e.currentTarget.style.backgroundColor = '#0d5aa8')}
                onMouseLeave={(e) => selectedPapersForGeneration && selectedPapersForGeneration.length > 0 && !isGenerating && (e.currentTarget.style.backgroundColor = '#1173d4')}
              >
                {isGenerating ? (
                  <>
                    <div className="w-5 h-5 border-2 border-white border-t-transparent rounded-full animate-spin"></div>
                    Generating...
                  </>
                ) : (
                  <>
                    <span className="material-symbols-outlined">auto_awesome</span>
                    Generate Related Work
                  </>
                )}
              </button>
            </div>
          </form>
          </div>
        </div>

        {/* Right Panel - Generated Content */}
        <div className="w-full lg:w-1/2 p-6 overflow-y-auto bg-gray-50 dark:bg-black/20">
          <div className="max-w-4xl mx-auto">
            {/* Placeholder State - No generation yet */}
            {!generatedText && !isGenerating && !generateError && (
              <div className="flex flex-col items-center justify-center h-full min-h-[400px] text-center">
                <div className="w-16 h-16 mb-4 text-black/20 dark:text-white/20">
                  <svg fill="none" viewBox="0 0 24 24" stroke="currentColor">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
                  </svg>
                </div>
                <h3 className="text-xl font-semibold text-black/60 dark:text-white/60 mb-2">
                  Generated Content Will Appear Here
                </h3>
                <p className="text-sm text-black/40 dark:text-white/40 max-w-md">
                  Complete the steps on the left, then click &ldquo;Generate Related Work&rdquo; to see your literature review section.
                </p>
              </div>
            )}

            {/* Error State */}
            {generateError && (
              <div className="mb-6 p-4 bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-lg">
                <div className="flex items-start gap-3">
                  <span className="material-symbols-outlined text-red-600 dark:text-red-400 flex-shrink-0">error</span>
                  <div>
                    <h3 className="text-sm font-semibold text-red-800 dark:text-red-200 mb-1">Generation Error</h3>
                    <p className="text-sm text-red-700 dark:text-red-300">{generateError}</p>
                  </div>
                </div>
              </div>
            )}

            {/* Loading State */}
            {isGenerating && !generatedText && (
              <div className="flex items-center justify-center gap-3 p-8">
                <div className="w-5 h-5 border-2 border-primary border-t-transparent rounded-full animate-spin"></div>
                <span className="text-sm font-medium text-primary">Generating related work section...</span>
              </div>
            )}

            {/* Generated Content */}
            {generatedText && (
              <div className="space-y-6">
                <div className="flex items-center justify-between mb-4">
                  <div>
                    <h2 className="text-2xl font-bold text-black dark:text-white">Related Work</h2>
                    <p className="text-sm text-black/50 dark:text-white/50 mt-1">
                      Generated on {new Date().toLocaleDateString('en-US', { year: 'numeric', month: 'long', day: 'numeric' })}
                    </p>
                  </div>
                </div>

                <div className="bg-white dark:bg-black/40 border border-black/10 dark:border-white/10 rounded-lg shadow-sm">
                  <div className="p-6">
                    <div className="prose prose-lg max-w-none text-black/80 dark:text-white/80">
                      <ReactMarkdown
                        remarkPlugins={[remarkGfm, remarkBreaks]}
                      >
                        {generatedText}
                      </ReactMarkdown>
                      {isGenerating && (
                        <span className="inline-block w-2 h-4 bg-primary animate-pulse ml-1"></span>
                      )}
                    </div>

                    {citations.length > 0 && (
                      <div className="mt-8 pt-6 border-t border-black/10 dark:border-white/10">
                        <h3 className="text-xl font-bold text-black dark:text-white mb-4">References</h3>
                        <div className="space-y-4">
                          {citations.map((citation) => (
                            <div key={citation.id} className="text-sm">
                              <div className="flex gap-2">
                                <span className="font-mono text-primary flex-shrink-0">[{citation.id}]</span>
                                <div>
                                  <p className="font-semibold text-black dark:text-white mb-1">
                                    {citation.title}
                                  </p>
                                  <p className="text-black/60 dark:text-white/60 text-xs line-clamp-3">
                                    {citation.abstract}
                                  </p>
                                </div>
                              </div>
                            </div>
                          ))}
                        </div>
                      </div>
                    )}
                  </div>

                  <div className="border-t border-black/10 dark:border-white/10 px-6 py-4 flex justify-end items-center gap-3">
                    <button
                      onClick={downloadAsMarkdown}
                      disabled={isGenerating || !generatedText}
                      style={{ backgroundColor: '#1173d4' }}
                      className="inline-flex items-center justify-center h-10 px-4 rounded-lg text-sm font-semibold text-white hover:opacity-90 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                    >
                      Download
                      <span className="material-symbols-outlined text-base ml-1.5 -mr-1">download</span>
                    </button>
                  </div>
                </div>
              </div>
            )}
          </div>
        </div>
      </main>
    </div>
  );
}
