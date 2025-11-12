# PDF Finder

PDF Finder sends structured queries to the Google Custom Search API, filters the results for PDF documents and saves them locally while maintaining a complete record of all search results in both JSON and CSV formats. It automatically handles duplicate links, generates clean, filesystem-safe filenames and exposes configurable options for pagination, request delay and download timeouts.

Once configured, running the script will search for PDFs that match your keywords, download them into a local directory, and write a manifest of every result for easy tracking, auditing and reuse. This makes PDF Finder especially useful for academic researchers, data collectors, and professionals who need to automate bulk retrieval of documents from public web sources.
