# PDF Finder

A Python-based document discovery and acquisition tool for locating and downloading public PDF files through the Google Custom Search JSON API.

## Application Overview

This application supports CLI arguments and environment defaults, constructs PDF-focused search queries, paginates across result pages, deduplicates URLs and records detailed metadata for every search hit. The tool includes resiliency through a shared requests session with retry handling, clear error classification for search failures such as quota issues.

PDF Finder also has rich manifests which capture search rank, page number, HTTP status, content type, content length, final URL, download timestamps, SHA-256 hashes and PDF validation results. A dry-run mode also allows you to inspect and export results without downloading files.

To improve reliability and traceability, PDF Finder validates downloads beyond file extensions by checking response headers before saving content to disk. Downloaded files are written with sanitized names, while structured JSON, CSV and search-error outputs make the results easy to audit and feed into later workflows.

Comprehensive logging captures the full lifecycle of each run, including query execution, deduplication, download attempts, skips and summary statistics. Together, these features make PDF Finder a dependable and reusable pipeline for researchers, analysts and engineers who need repeatable bulk PDF collection with strong observability and cleaner operational controls.

## Basic Setup Instructions

Below are the required software programs and instructions for installing and using this application on a Linux machine.

### Programs Needed

- [Git](https://git-scm.com/downloads)

- [Python](https://www.python.org/downloads/)

### Steps For Use

1. Install the above programs

2. Open a terminal

3. Clone this repository: `git clone git@github.com:devbret/pdf-finder.git`

4. Navigate to the repo's directory: `cd pdf-finder`

5. Create a virtual environment: `python3 -m venv venv`

6. Activate your virtual environment: `source venv/bin/activate`

7. Install the needed dependencies: `pip install -r requirements.txt`

8. Create your configuration file from the template: `cp .env.template .env`

9. Open `.env` and set values for `API_KEY`, `CX` and so on

10. Run the program: `python3 app.py`

11. Downloaded PDFs will be saved to the `pdf_downloads` directory

12. When finished, exit the virtual environment: `deactivate`

## Other Considerations

This project repo is intended to demonstrate an ability to do the following:

- Search for and download public PDF files at scale using the Google Custom Search JSON API

- Use CLI arguments configuration for constructing queries and paginating across result pages

- Write files with sanitized names, deduplicate URLs and skip blocked domains

- Capture each result's search rank, page number, HTTP status, download timestamp and more in manifest files

- Provide observability through classification of search errors and logging

If you have any questions or would like to collaborate, please reach out either on GitHub or via [my website](https://bretbernhoft.com/).
