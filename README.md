# PDF Finder

PDF Finder is designed as a robust, end-to-end pipeline for discovering and downloading PDF documents from the public web using the Google Custom Search API. This script creates structured search queries, paginates through configured result sets and extracts full metadata—including titles, snippets and MIME types for every returned item.

By explicitly appending `filetype:pdf` to each query and evaluating MIME responses server-side during download, the script ensures only valid PDF files are processed and stored. This makes it a reliable tool for gathering targeted documents at scale without manual oversight.

A major strength of PDF Finder is its emphasis on safety, traceability and consistency. Every downloaded file is assigned a sanitized, filesystem-safe filename based on its discovered title or URL, with automatic conflict resolution to prevent overwrites.

Comprehensive logging captures each stage of execution providing full transparency for debugging and auditing. Additionally, the deduplication mechanism prevents redundant downloads by removing repeated links across queries, ensuring efficient use of bandwidth and storage.

The Python script also includes a complete manifest system for writing structured results to both JSON and CSV formats, allowing seamless integration with downstream workflows such as data analysis, archival or ingestion into research tools.

Configurable environment variables make it easy to adjust behavior for large or repeated jobs, including pagination depth, request delay, timeout settings, output directories and user-defined search queries. Combined, these features make PDF Finder a flexible and production-ready solution for researchers, analysts and engineers who need an automated method for bulk PDF retrieval and metadata collection.
