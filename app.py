import os
import re
import json
import csv
import time
import hashlib
import logging
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

import requests
from requests import Response, Session
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from dotenv import load_dotenv, find_dotenv

env_path = find_dotenv()
if env_path:
    load_dotenv(dotenv_path=env_path)

RESULTS_PER_PAGE = 10
DEFAULT_SEARCH_TIMEOUT = 30
DEFAULT_CONNECT_TIMEOUT = 10


@dataclass
class SearchResult:
    query: str
    title: str
    link: str
    snippet: str
    mime: str = ""
    page_number: int = 0
    search_rank: int = 0
    source_rank: int = 0
    status: str = ""
    saved_as: str = ""
    error: str = ""
    http_status: Optional[int] = None
    content_type: str = ""
    content_length: Optional[int] = None
    downloaded_at: str = ""
    sha256: str = ""
    final_url: str = ""
    is_valid_pdf: bool = False


@dataclass
class SearchError:
    query: str
    page_number: int
    error_type: str
    message: str
    http_status: Optional[int] = None


def _parse_queries(raw: str) -> List[str]:
    if not raw:
        return []
    raw = raw.strip()
    if raw.startswith("["):
        try:
            arr = json.loads(raw)
            return [str(x).strip() for x in arr if str(x).strip()]
        except Exception:
            pass
    return [q.strip() for q in raw.split(",") if q.strip()]


def _parse_domain_list(raw: str) -> List[str]:
    if not raw:
        return []
    raw = raw.strip()
    if raw.startswith("["):
        try:
            arr = json.loads(raw)
            return [str(x).strip().lower() for x in arr if str(x).strip()]
        except Exception:
            pass
    return [d.strip().lower() for d in raw.split(",") if d.strip()]


def _int_env(name: str, default: int) -> int:
    v = os.getenv(name, "")
    try:
        return int(v) if v.strip() else default
    except Exception:
        return default


def _float_env(name: str, default: float) -> float:
    v = os.getenv(name, "")
    try:
        return float(v) if v.strip() else default
    except Exception:
        return default


def safe_filename(name: str) -> str:
    name = re.sub(r"[^\w\s\-.()]+", "", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:120] or "document"


def short_hash(text: str, length: int = 8) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:length]


def guess_filename_from_url(url: str) -> str:
    try:
        name = Path(urlparse(url).path).name
        return safe_filename(Path(name).stem) or "document"
    except Exception:
        return "document"


def normalize_url(url: str) -> str:
    try:
        parsed = urlparse(url.strip())
        tracking_params = {
            "utm_source",
            "utm_medium",
            "utm_campaign",
            "utm_term",
            "utm_content",
            "gclid",
            "fbclid",
        }
        kept_params = [
            (k, v)
            for k, v in parse_qsl(parsed.query, keep_blank_values=True)
            if k not in tracking_params
        ]
        kept_params.sort()
        normalized = parsed._replace(
            scheme=parsed.scheme.lower(),
            netloc=parsed.netloc.lower(),
            query=urlencode(kept_params),
            fragment="",
        )
        return urlunparse(normalized)
    except Exception:
        return url.strip()


def extract_hostname(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def is_blocked_domain(url: str, blocked_domains: List[str]) -> bool:
    hostname = extract_hostname(url)
    if not hostname:
        return False

    for blocked in blocked_domains:
        blocked = blocked.lower().strip()
        if not blocked:
            continue
        if hostname == blocked or hostname.endswith(f".{blocked}"):
            return True

    return False


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_pdf_by_headers(resp: Response) -> bool:
    return "application/pdf" in resp.headers.get("Content-Type", "").lower()


def looks_like_pdf_bytes(data: bytes) -> bool:
    return data.startswith(b"%PDF-")


def build_session(user_agent: str) -> Session:
    session = requests.Session()
    session.headers.update({"User-Agent": user_agent})

    retry = Retry(
        total=2,
        connect=2,
        read=1,
        backoff_factor=0.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
        raise_on_status=False,
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def setup_logger(log_path: Path) -> logging.Logger:
    log_path.parent.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("pdf_finder")
    logger.setLevel(logging.INFO)

    if not logger.handlers:
        file_fmt = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

        fh = logging.FileHandler(log_path, encoding="utf-8")
        fh.setFormatter(file_fmt)
        logger.addHandler(fh)

        ch = logging.StreamHandler()
        ch.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(ch)

    return logger


def classify_google_error(
    response: Optional[Response], data: Optional[Dict[str, Any]], exc: Optional[Exception]
) -> Tuple[str, str, Optional[int]]:
    if response is not None:
        status = response.status_code
        try:
            err_obj = (data or {}).get("error", {})
            message = err_obj.get("message") or response.text[:500]
        except Exception:
            message = response.text[:500]

        lowered = (message or "").lower()

        if status == 403 and ("quota" in lowered or "limit" in lowered):
            return "quota_exceeded", message, status
        if status == 403 and ("key" in lowered or "credential" in lowered or "access" in lowered):
            return "auth_error", message, status
        if status == 400:
            return "bad_request", message, status
        if status == 429:
            return "rate_limited", message, status
        if 500 <= status <= 599:
            return "server_error", message, status
        return "http_error", message, status

    if exc is not None:
        msg = str(exc)
        lowered = msg.lower()
        if "timeout" in lowered:
            return "timeout", msg, None
        if "connection" in lowered:
            return "connection_error", msg, None
        return "request_error", msg, None

    return "unknown_error", "Unknown error", None


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Search Google Custom Search results for PDFs and download verified PDF files."
    )
    parser.add_argument(
        "--query",
        action="append",
        dest="queries",
        help="Query to search. Can be supplied multiple times.",
    )
    parser.add_argument(
        "--pages",
        type=int,
        default=None,
        help="Maximum number of Google CSE pages to request per query.",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=None,
        help="Delay between search page requests in seconds.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=None,
        help="Request timeout in seconds for PDF downloads.",
    )
    parser.add_argument(
        "--search-timeout",
        type=int,
        default=None,
        help="Request timeout in seconds for Google CSE search calls.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=None,
        help="Number of concurrent download workers.",
    )
    parser.add_argument(
        "--out-dir",
        default=None,
        help="Directory where downloaded PDFs are stored.",
    )
    parser.add_argument(
        "--manifest-dir",
        default=None,
        help="Directory where JSON/CSV manifests are stored.",
    )
    parser.add_argument(
        "--log-file",
        default=None,
        help="Log file name or absolute path.",
    )
    parser.add_argument(
        "--api-endpoint",
        default=None,
        help="Google CSE JSON API endpoint.",
    )
    parser.add_argument(
        "--user-agent",
        default=None,
        help="User-Agent to use for requests.",
    )
    parser.add_argument(
        "--blocked-domain",
        action="append",
        dest="blocked_domains",
        help="Domain to avoid downloading from. Can be supplied multiple times.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Search and write manifests without downloading PDFs.",
    )
    return parser


def load_config(args: argparse.Namespace) -> Dict[str, Any]:
    api_key = os.getenv("API_KEY")
    cx = os.getenv("CX")

    if not api_key or not cx:
        raise SystemExit("Missing required values in .env or environment (API_KEY and CX are mandatory).")

    env_queries = _parse_queries(os.getenv("QUERIES", ""))
    cli_queries = args.queries or []
    queries = cli_queries if cli_queries else env_queries

    if not queries:
        raise SystemExit("No queries provided. Use --query or set QUERIES in .env.")

    env_blocked_domains = _parse_domain_list(os.getenv("BLOCKED_DOWNLOAD_DOMAINS", ""))
    cli_blocked_domains = [d.strip().lower() for d in (args.blocked_domains or []) if d.strip()]
    blocked_domains = cli_blocked_domains if cli_blocked_domains else env_blocked_domains

    pages = args.pages if args.pages is not None else _int_env("PAGES", 10)
    delay = args.delay if args.delay is not None else _float_env("DELAY", 0.0)
    timeout = args.timeout if args.timeout is not None else _int_env("TIMEOUT", 60)
    search_timeout = (
        args.search_timeout
        if args.search_timeout is not None
        else _int_env("SEARCH_TIMEOUT", DEFAULT_SEARCH_TIMEOUT)
    )
    workers = max(1, args.workers if args.workers is not None else _int_env("WORKERS", 4))

    api_endpoint = (args.api_endpoint or os.getenv("API_ENDPOINT", "https://www.googleapis.com/customsearch/v1")).strip()
    out_dir = Path((args.out_dir or os.getenv("OUT_DIR", "pdf_downloads")).strip() or "pdf_downloads")
    manifest_dir = Path((args.manifest_dir or os.getenv("MANIFEST_DIR", "manifests")).strip() or "manifests")
    log_file = (args.log_file or os.getenv("LOG_FILE", "pdf_finder.log")).strip() or "pdf_finder.log"
    user_agent = (args.user_agent or os.getenv("USER_AGENT", "pdf-finder/2.0")).strip()

    log_path = Path(log_file)
    if not log_path.is_absolute():
        log_path = manifest_dir / log_path

    return {
        "API_KEY": api_key,
        "CX": cx,
        "API_ENDPOINT": api_endpoint,
        "OUT_DIR": out_dir,
        "MANIFEST_DIR": manifest_dir,
        "LOG_PATH": log_path,
        "USER_AGENT": user_agent,
        "QUERIES": queries,
        "BLOCKED_DOWNLOAD_DOMAINS": blocked_domains,
        "PAGES": pages,
        "DELAY": delay,
        "TIMEOUT": timeout,
        "SEARCH_TIMEOUT": search_timeout,
        "WORKERS": workers,
        "DRY_RUN": args.dry_run,
    }


def search_pdfs(
    session: Session,
    logger: logging.Logger,
    api_key: str,
    cx: str,
    api_endpoint: str,
    query: str,
    pages: int,
    delay: float,
    search_timeout: int,
) -> Tuple[List[SearchResult], List[SearchError]]:
    logger.info("Starting search for query: %s (pages=%d)", query, pages)
    results: List[SearchResult] = []
    errors: List[SearchError] = []
    start = 1
    rank = 0

    for page in range(1, pages + 1):
        params = {
            "key": api_key,
            "cx": cx,
            "q": f"{query} filetype:pdf",
            "fileType": "pdf",
            "num": RESULTS_PER_PAGE,
            "start": start,
            "safe": "off",
        }

        logger.info(
            "Requesting Google CSE page %d for query '%s' (start=%d)",
            page,
            query,
            start,
        )

        response: Optional[Response] = None
        data: Optional[Dict[str, Any]] = None

        try:
            response = session.get(api_endpoint, params=params, timeout=search_timeout)

            try:
                data = response.json()
            except Exception:
                data = None

            if response.status_code != 200:
                err_type, err_msg, http_status = classify_google_error(response, data, None)
                logger.error(
                    "Search failed for query='%s', page=%d, type=%s, status=%s, message=%s",
                    query,
                    page,
                    err_type,
                    http_status,
                    err_msg,
                )
                errors.append(
                    SearchError(
                        query=query,
                        page_number=page,
                        error_type=err_type,
                        message=err_msg,
                        http_status=http_status,
                    )
                )
                break

            items = (data or {}).get("items", [])
            logger.info("Received %d items for query '%s' on page %d", len(items), query, page)

            for idx, item in enumerate(items, start=1):
                rank += 1
                results.append(
                    SearchResult(
                        query=query,
                        title=item.get("title", ""),
                        link=item.get("link", ""),
                        snippet=item.get("snippet", ""),
                        mime=item.get("mime", ""),
                        page_number=page,
                        search_rank=rank,
                        source_rank=idx,
                    )
                )

            next_page = (data or {}).get("queries", {}).get("nextPage", [{}])[0].get("startIndex")
            if not next_page:
                logger.info("No more pages for query '%s'", query)
                break

            start = next_page
            if delay:
                time.sleep(delay)

        except Exception as exc:
            err_type, err_msg, http_status = classify_google_error(response, data, exc)
            logger.error(
                "Search exception for query='%s', page=%d, type=%s, message=%s",
                query,
                page,
                err_type,
                err_msg,
            )
            errors.append(
                SearchError(
                    query=query,
                    page_number=page,
                    error_type=err_type,
                    message=err_msg,
                    http_status=http_status,
                )
            )
            break

    logger.info("Finished search for query '%s' with %d total items", query, len(results))
    return results, errors


def dedupe_results(results: List[SearchResult], logger: logging.Logger) -> List[SearchResult]:
    logger.info("Deduplicating %d results by normalized link", len(results))
    seen: set[str] = set()
    out: List[SearchResult] = []

    for item in results:
        normalized = normalize_url(item.link)
        if normalized not in seen:
            seen.add(normalized)
            item.link = normalized
            out.append(item)

    logger.info("Deduplication complete: %d unique links", len(out))
    return out


def choose_output_path(out_dir: Path, title_hint: str, url: str) -> Path:
    base = safe_filename(title_hint) or guess_filename_from_url(url)
    suffix = short_hash(url, 8)
    filename = f"{base}_{suffix}.pdf"
    return out_dir / filename


def _download_result(
    error: str = "",
    http_status: Optional[int] = None,
    content_type: str = "",
    content_length: Optional[int] = None,
    final_url: str = "",
    is_valid_pdf: bool = False,
    sha256: str = "",
    saved_as: str = "",
) -> Dict[str, Any]:
    return {
        "error": error,
        "http_status": http_status,
        "content_type": content_type,
        "content_length": content_length,
        "final_url": final_url,
        "is_valid_pdf": is_valid_pdf,
        "sha256": sha256,
        "saved_as": saved_as,
    }


def download_pdf(
    session: Session,
    logger: logging.Logger,
    out_dir: Path,
    timeout: int,
    url: str,
    title_hint: str,
) -> Tuple[bool, Dict[str, Any]]:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = choose_output_path(out_dir, title_hint, url)
    tmp_path = path.with_name(path.name + ".part")

    logger.info("Downloading PDF: url=%s, title_hint=%s, target=%s", url, title_hint, path)

    request_timeout = (min(DEFAULT_CONNECT_TIMEOUT, timeout), timeout)

    try:
        with session.get(url, stream=True, timeout=request_timeout, allow_redirects=True) as response:
            final_url = response.url
            http_status = response.status_code
            content_type = response.headers.get("Content-Type", "")
            content_length_raw = response.headers.get("Content-Length")
            content_length = int(content_length_raw) if content_length_raw and content_length_raw.isdigit() else None

            meta = dict(
                http_status=http_status,
                content_type=content_type,
                content_length=content_length,
                final_url=final_url,
            )

            if http_status != 200:
                msg = f"HTTP {http_status}"
                logger.warning("Download failed (%s) for url=%s", msg, url)
                return False, _download_result(error=msg, **meta)

            chunk_iter = response.iter_content(chunk_size=8192)
            first_chunk = next(chunk_iter, b"")

            if not first_chunk:
                msg = "Empty response body"
                logger.warning("Download failed (%s) for url=%s", msg, url)
                return False, _download_result(error=msg, **meta)

            header_pdf = is_pdf_by_headers(response)
            magic_pdf = looks_like_pdf_bytes(first_chunk)

            if not header_pdf and not magic_pdf and not final_url.lower().endswith(".pdf"):
                msg = f"Not a PDF (Content-Type={content_type})"
                logger.warning("Download skipped: %s; url=%s", msg, url)
                return False, _download_result(error=msg, **meta)

            if not magic_pdf:
                msg = "File does not start with PDF signature"
                logger.warning("Download skipped: %s; url=%s", msg, url)
                return False, _download_result(error=msg, **meta)

            hasher = hashlib.sha256()
            try:
                with open(tmp_path, "wb") as f:
                    f.write(first_chunk)
                    hasher.update(first_chunk)

                    for chunk in chunk_iter:
                        if chunk:
                            f.write(chunk)
                            hasher.update(chunk)

                os.replace(tmp_path, path)
            except BaseException:
                tmp_path.unlink(missing_ok=True)
                raise

            sha256_hex = hasher.hexdigest()
            logger.info("Download succeeded: %s", path)

            return True, _download_result(
                is_valid_pdf=True,
                sha256=sha256_hex,
                saved_as=str(path),
                **meta,
            )

    except Exception as exc:
        logger.error("Exception while downloading url=%s: %s", url, exc)
        return False, _download_result(error=str(exc))


def save_manifest(
    manifest_dir: Path,
    logger: logging.Logger,
    data: List[SearchResult],
    search_errors: List[SearchError],
) -> None:
    manifest_dir.mkdir(parents=True, exist_ok=True)

    run_stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    json_path = manifest_dir / f"pdf_results_{run_stamp}.json"
    csv_path = manifest_dir / f"pdf_results_{run_stamp}.csv"
    errors_path = manifest_dir / f"search_errors_{run_stamp}.json"

    summary = {
        "generated_at": utc_now_iso(),
        "total_results": len(data),
        "downloaded": sum(1 for x in data if x.status == "downloaded"),
        "skipped": sum(1 for x in data if x.status == "skipped"),
        "valid_pdf_count": sum(1 for x in data if x.is_valid_pdf),
        "search_error_count": len(search_errors),
    }

    payload = {
        "summary": summary,
        "results": [asdict(row) for row in data],
    }

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)

    fields = [
        "query",
        "title",
        "link",
        "snippet",
        "mime",
        "page_number",
        "search_rank",
        "source_rank",
        "status",
        "saved_as",
        "error",
        "http_status",
        "content_type",
        "content_length",
        "downloaded_at",
        "sha256",
        "final_url",
        "is_valid_pdf",
    ]

    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in data:
            writer.writerow({k: getattr(row, k) for k in fields})

    with open(errors_path, "w", encoding="utf-8") as f:
        json.dump([asdict(err) for err in search_errors], f, indent=2, ensure_ascii=False)

    logger.info("Saved manifest JSON: %s", json_path)
    logger.info("Saved manifest CSV: %s", csv_path)
    logger.info("Saved search errors JSON: %s", errors_path)


def run_searches(
    session: Session,
    logger: logging.Logger,
    config: Dict[str, Any],
) -> Tuple[List[SearchResult], List[SearchError]]:
    all_results: List[SearchResult] = []
    search_errors: List[SearchError] = []

    for q in config["QUERIES"]:
        logger.info("[search] %s", q)

        hits, errs = search_pdfs(
            session=session,
            logger=logger,
            api_key=config["API_KEY"],
            cx=config["CX"],
            api_endpoint=config["API_ENDPOINT"],
            query=q,
            pages=config["PAGES"],
            delay=config["DELAY"],
            search_timeout=config["SEARCH_TIMEOUT"],
        )

        all_results.extend(hits)
        search_errors.extend(errs)

        logger.info("  -> %d results", len(hits))
        for err in errs:
            logger.warning("  -> search error [%s]: %s", err.error_type, err.message)

    return all_results, search_errors


def run_downloads(
    session: Session,
    logger: logging.Logger,
    config: Dict[str, Any],
    all_results: List[SearchResult],
) -> None:
    total = len(all_results)
    out_dir = config["OUT_DIR"]
    timeout = config["TIMEOUT"]
    delay = config["DELAY"]
    workers = config["WORKERS"]
    blocked_domains = config["BLOCKED_DOWNLOAD_DOMAINS"]

    def process(item: SearchResult) -> None:
        if is_blocked_domain(item.link, blocked_domains):
            blocked_host = extract_hostname(item.link)
            item.status = "skipped"
            item.error = f"Blocked domain: {blocked_host}"
            item.downloaded_at = utc_now_iso()
            logger.info("Skipped blocked domain: url=%s, hostname=%s", item.link, blocked_host)
            return

        if delay:
            time.sleep(delay)

        ok, info = download_pdf(
            session=session,
            logger=logger,
            out_dir=out_dir,
            timeout=timeout,
            url=item.link,
            title_hint=item.title,
        )

        item.http_status = info.get("http_status")
        item.content_type = info.get("content_type", "")
        item.content_length = info.get("content_length")
        item.final_url = info.get("final_url", "")
        item.is_valid_pdf = bool(info.get("is_valid_pdf", False))
        item.sha256 = info.get("sha256", "")
        item.downloaded_at = utc_now_iso()

        if ok:
            item.status = "downloaded"
            item.saved_as = info.get("saved_as", "")
        else:
            item.status = "skipped"
            item.error = info.get("error", "")

    def log_progress(done: int, item: SearchResult) -> None:
        detail = item.saved_as if item.status == "downloaded" else item.error
        logger.info("[%d/%d] %s: %s (%s)", done, total, item.status, item.link, detail)

    if workers > 1 and total > 1:
        logger.info("Downloading with %d concurrent workers", workers)
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(process, item): item for item in all_results}
            for done, future in enumerate(as_completed(futures), start=1):
                item = futures[future]
                try:
                    future.result()
                except Exception as exc:
                    item.status = "skipped"
                    item.error = str(exc)
                    item.downloaded_at = utc_now_iso()
                    logger.error("Download task error: url=%s: %s", item.link, exc)
                log_progress(done, item)
    else:
        for done, item in enumerate(all_results, start=1):
            process(item)
            log_progress(done, item)


def print_summary(
    logger: logging.Logger,
    config: Dict[str, Any],
    all_results: List[SearchResult],
    search_errors: List[SearchError],
) -> None:
    downloaded_count = sum(1 for x in all_results if x.status == "downloaded")
    skipped_count = sum(1 for x in all_results if x.status == "skipped")

    logger.info(
        "Summary: total=%d downloaded=%d skipped=%d search_errors=%d",
        len(all_results),
        downloaded_count,
        skipped_count,
        len(search_errors),
    )
    logger.info("PDFs saved in: %s", config["OUT_DIR"].resolve())
    logger.info("=== Run finished ===\n")


def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()
    config = load_config(args)

    logger = setup_logger(config["LOG_PATH"])
    session = build_session(config["USER_AGENT"])

    logger.info("=== Run started ===")
    logger.info("Queries: %s", config["QUERIES"])
    logger.info("Blocked download domains: %s", config["BLOCKED_DOWNLOAD_DOMAINS"])
    logger.info("Output directory: %s", config["OUT_DIR"].resolve())
    logger.info("Manifest directory: %s", config["MANIFEST_DIR"].resolve())
    logger.info("Log file: %s", config["LOG_PATH"].resolve())
    logger.info("Workers: %d", config["WORKERS"])
    logger.info("Dry run: %s", config["DRY_RUN"])

    all_results, search_errors = run_searches(session, logger, config)

    all_results = dedupe_results(all_results, logger)
    logger.info("[dedupe] %d unique links", len(all_results))

    if not config["DRY_RUN"]:
        run_downloads(session, logger, config, all_results)
    else:
        logger.info("Dry run enabled; skipping downloads.")
        for item in all_results:
            item.status = "not_downloaded"

    save_manifest(
        manifest_dir=config["MANIFEST_DIR"],
        logger=logger,
        data=all_results,
        search_errors=search_errors,
    )

    print_summary(logger, config, all_results, search_errors)


if __name__ == "__main__":
    main()