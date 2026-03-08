# PDF Finder

A Python-based document discovery and acquisition tool for locating and downloading public PDF files through the Google Custom Search JSON API.

## Overview

This application supports CLI arguments and environment defaults, constructs PDF-focused search queries, paginates across result pages, deduplicates URLs and records detailed metadata for every search hit. The tool includes resiliency through a shared requests session with retry handling, clear error classification for search failures such as quota issues.

PDF Finder also has rich manifests which capture search rank, page number, HTTP status, content type, content length, final URL, download timestamps, SHA-256 hashes and PDF validation results. A dry-run mode also allows you to inspect and export results without downloading files.

To improve reliability and traceability, PDF Finder validates downloads beyond file extensions by checking response headers before saving content to disk. Downloaded files are written with sanitized names, while structured JSON, CSV and search-error outputs make the results easy to audit and feed into later workflows.

Comprehensive logging captures the full lifecycle of each run, including query execution, deduplication, download attempts, skips and summary statistics. Together, these features make PDF Finder a dependable and reusable pipeline for researchers, analysts and engineers who need repeatable bulk PDF collection with strong observability and cleaner operational controls.
