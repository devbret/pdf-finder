import os
import re
import json
import csv
import time
import hashlib
import logging
import argparse
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
        fname = Path(urlparse(url).path).name or "document"
        return safe_filename(fname.replace(".pdf", ""))
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
        total=5,
        connect=5,
        read=5,
        backoff_factor=1.0,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
        raise_on_status=False,
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
        fmt = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

        fh = logging.FileHandler(log_path, encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)

        ch = logging.StreamHandler()
        ch.setFormatter(fmt)
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
) -> Tuple[List[SearchResult], List[SearchError]]:
    logger.info("Starting search for query: %s (pages=%d)", query, pages)
    results: List[SearchResult] = []
    errors: List[SearchError] = []
    start = 1

    for page in range(1, pages + 1):
        params = {
            "key": api_key,
            "cx": cx,
            "q": f"{query} filetype:pdf",
            "fileType": "pdf",
            "num": 10,
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
            response = session.get(api_endpoint, params=params, timeout=30)

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
                results.append(
                    SearchResult(
                        query=query,
                        title=item.get("title", ""),
                        link=item.get("link", ""),
                        snippet=item.get("snippet", ""),
                        mime=item.get("mime", ""),
                        page_number=page,
                        search_rank=((page - 1) * 10) + idx,
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

    logger.info("Downloading PDF: url=%s, title_hint=%s, target=%s", url, title_hint, path)

    try:
        with session.get(url, stream=True, timeout=timeout, allow_redirects=True) as response:
            final_url = response.url
            http_status = response.status_code
            content_type = response.headers.get("Content-Type", "")
            content_length_raw = response.headers.get("Content-Length")
            content_length = int(content_length_raw) if content_length_raw and content_length_raw.isdigit() else None

            if http_status != 200:
                msg = f"HTTP {http_status}"
                logger.warning("Download failed (%s) for url=%s", msg, url)
                return False, {
                    "error": msg,
                    "http_status": http_status,
                    "content_type": content_type,
                    "content_length": content_length,
                    "final_url": final_url,
                    "is_valid_pdf": False,
                    "sha256": "",
                    "saved_as": "",
                }

            chunk_iter = response.iter_content(chunk_size=8192)
            first_chunk = next(chunk_iter, b"")

            if not first_chunk:
                msg = "Empty response body"
                logger.warning("Download failed (%s) for url=%s", msg, url)
                return False, {
                    "error": msg,
                    "http_status": http_status,
                    "content_type": content_type,
                    "content_length": content_length,
                    "final_url": final_url,
                    "is_valid_pdf": False,
                    "sha256": "",
                    "saved_as": "",
                }

            header_pdf = is_pdf_by_headers(response)
            magic_pdf = looks_like_pdf_bytes(first_chunk)

            if not header_pdf and not magic_pdf and not final_url.lower().endswith(".pdf"):
                msg = f"Not a PDF (Content-Type={content_type})"
                logger.warning("Download skipped: %s; url=%s", msg, url)
                return False, {
                    "error": msg,
                    "http_status": http_status,
                    "content_type": content_type,
                    "content_length": content_length,
                    "final_url": final_url,
                    "is_valid_pdf": False,
                    "sha256": "",
                    "saved_as": "",
                }

            if not magic_pdf:
                msg = "File does not start with PDF signature"
                logger.warning("Download skipped: %s; url=%s", msg, url)
                return False, {
                    "error": msg,
                    "http_status": http_status,
                    "content_type": content_type,
                    "content_length": content_length,
                    "final_url": final_url,
                    "is_valid_pdf": False,
                    "sha256": "",
                    "saved_as": "",
                }

            hasher = hashlib.sha256()
            with open(path, "wb") as f:
                f.write(first_chunk)
                hasher.update(first_chunk)

                for chunk in chunk_iter:
                    if chunk:
                        f.write(chunk)
                        hasher.update(chunk)

            sha256_hex = hasher.hexdigest()
            logger.info("Download succeeded: %s", path)

            return True, {
                "error": "",
                "http_status": http_status,
                "content_type": content_type,
                "content_length": content_length,
                "final_url": final_url,
                "is_valid_pdf": True,
                "sha256": sha256_hex,
                "saved_as": str(path),
            }

    except Exception as exc:
        logger.error("Exception while downloading url=%s: %s", url, exc)
        return False, {
            "error": str(exc),
            "http_status": None,
            "content_type": "",
            "content_length": None,
            "final_url": "",
            "is_valid_pdf": False,
            "sha256": "",
            "saved_as": "",
        }


def save_manifest(
    manifest_dir: Path,
    logger: logging.Logger,
    data: List[SearchResult],
    search_errors: List[SearchError],
) -> None:
    manifest_dir.mkdir(parents=True, exist_ok=True)

    json_path = manifest_dir / "pdf_results.json"
    csv_path = manifest_dir / "pdf_results.csv"
    errors_path = manifest_dir / "search_errors.json"

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
    print(f"Saved manifest: {json_path}")
    print(f"Saved manifest: {csv_path}")
    print(f"Saved search errors: {errors_path}")


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
    logger.info("Dry run: %s", config["DRY_RUN"])

    all_results: List[SearchResult] = []
    search_errors: List[SearchError] = []

    for q in config["QUERIES"]:
        print(f"[search] {q}")
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
        )

        all_results.extend(hits)
        search_errors.extend(errs)

        print(f"  -> {len(hits)} results")
        if errs:
            for err in errs:
                print(f"  -> search error [{err.error_type}]: {err.message}")

    all_results = dedupe_results(all_results, logger)
    print(f"[dedupe] {len(all_results)} unique links")
    logger.info("Total unique links after dedupe: %d", len(all_results))

    if not config["DRY_RUN"]:
        for i, item in enumerate(all_results, start=1):
            print(f"[{i}/{len(all_results)}] Downloading: {item.link}")
            logger.info("Preparing to download (%d/%d): %s", i, len(all_results), item.link)

            if is_blocked_domain(item.link, config["BLOCKED_DOWNLOAD_DOMAINS"]):
                blocked_host = extract_hostname(item.link)
                item.status = "skipped"
                item.error = f"Blocked domain: {blocked_host}"
                item.downloaded_at = utc_now_iso()
                logger.info("Skipped blocked domain: url=%s, hostname=%s", item.link, blocked_host)
                continue

            ok, info = download_pdf(
                session=session,
                logger=logger,
                out_dir=config["OUT_DIR"],
                timeout=config["TIMEOUT"],
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
                logger.info("Marked as downloaded: url=%s, saved_as=%s", item.link, item.saved_as)
            else:
                item.status = "skipped"
                item.error = info.get("error", "")
                logger.info("Marked as skipped: url=%s, reason=%s", item.link, item.error)
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

    print(f"PDFs saved in: {config['OUT_DIR'].resolve()}")
    print(
        f"Summary: total={len(all_results)}, downloaded={downloaded_count}, "
        f"skipped={skipped_count}, search_errors={len(search_errors)}"
    )


if __name__ == "__main__":
    main()