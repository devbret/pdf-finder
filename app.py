import os
import re
import json
import time
import csv
import requests
from pathlib import Path
from dotenv import load_dotenv, find_dotenv

env_path = find_dotenv()
if not env_path:
    raise SystemExit("Missing .env file. Please create one in the project root.")

load_dotenv(dotenv_path=env_path)

def _parse_queries_env(raw: str):
    if not raw:
        return ["keyword searches here"]
    raw = raw.strip()
    if raw.startswith("["):
        try:
            arr = json.loads(raw)
            return [str(x).strip() for x in arr if str(x).strip()]
        except Exception:
            pass
    return [q.strip() for q in raw.split(",") if q.strip()]

API_KEY = os.getenv("API_KEY")
CX = os.getenv("CX")

if not API_KEY or not CX:
    raise SystemExit("Missing required values in .env (API_KEY and CX are mandatory).")

API_ENDPOINT = os.getenv("API_ENDPOINT", "https://www.googleapis.com/customsearch/v1").strip()
OUT_DIR = Path(os.getenv("OUT_DIR", "pdf_downloads").strip() or "pdf_downloads")
MANIFEST_DIR = Path(os.getenv("MANIFEST_DIR", "manifests").strip() or "manifests")
USER_AGENT = os.getenv("USER_AGENT", "pdf-finder/1.0").strip()
QUERIES = _parse_queries_env(os.getenv("QUERIES", ""))

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

PAGES = _int_env("PAGES", 10)
DELAY = _float_env("DELAY", 0.0)
TIMEOUT = _int_env("TIMEOUT", 60)

def safe_filename(name: str) -> str:
    name = re.sub(r"[^\w\s\-.()]+", "", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:150] or "document"

def guess_filename_from_url(url: str) -> str:
    try:
        fname = Path(requests.utils.urlparse(url).path).name or "document"
        return safe_filename(fname.replace(".pdf", ""))
    except Exception:
        return "document"

def is_pdf_response(resp):
    return "application/pdf" in resp.headers.get("Content-Type", "").lower()

def search_pdfs(query, pages=PAGES):
    results = []
    start = 1
    for _ in range(pages):
        params = {
            "key": API_KEY,
            "cx": CX,
            "q": f"{query} filetype:pdf",
            "fileType": "pdf",
            "num": 10,
            "start": start,
            "safe": "off",
        }
        r = requests.get(API_ENDPOINT, params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        for item in data.get("items", []):
            results.append({
                "query": query,
                "title": item.get("title", ""),
                "link": item.get("link", ""),
                "snippet": item.get("snippet", ""),
                "mime": item.get("mime", "")
            })
        next_page = data.get("queries", {}).get("nextPage", [{}])[0].get("startIndex")
        if not next_page:
            break
        start = next_page
        if DELAY:
            time.sleep(DELAY)
    return results

def dedupe(results):
    seen, out = set(), []
    for r in results:
        if r["link"] not in seen:
            seen.add(r["link"])
            out.append(r)
    return out

def download_pdf(url, title_hint):
    filename = safe_filename(title_hint) or guess_filename_from_url(url)
    path = OUT_DIR / f"{filename}.pdf"
    if path.exists():
        for i in range(2, 9999):
            trial = OUT_DIR / f"{filename} ({i}).pdf"
            if not trial.exists():
                path = trial
                break
    try:
        with requests.get(url, stream=True, timeout=TIMEOUT, headers={"User-Agent": USER_AGENT}) as r:
            if r.status_code != 200:
                return False, f"HTTP {r.status_code}"
            if not is_pdf_response(r) and not url.lower().endswith(".pdf"):
                return False, f"Not a PDF ({r.headers.get('Content-Type')})"
            OUT_DIR.mkdir(parents=True, exist_ok=True)
            with open(path, "wb") as f:
                for chunk in r.iter_content(8192):
                    if chunk:
                        f.write(chunk)
        return True, str(path)
    except Exception as e:
        return False, str(e)

def save_manifest(data):
    MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
    json_path = MANIFEST_DIR / "pdf_results.json"
    csv_path = MANIFEST_DIR / "pdf_results.csv"

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    fields = ["query", "title", "link", "snippet", "mime", "status", "saved_as", "error"]
    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for row in data:
            out = {k: row.get(k, "") for k in fields}
            w.writerows([out])

    print(f"Saved manifest: {json_path} and {csv_path}")

def main():
    all_results = []
    for q in QUERIES:
        print(f"[search] {q}")
        try:
            hits = search_pdfs(q)
        except requests.HTTPError as http_err:
            print(f"  -> HTTP error: {http_err}")
            hits = []
        except Exception as e:
            print(f"  -> Error: {e}")
            hits = []
        print(f"  -> {len(hits)} results")
        for h in hits:
            h["status"], h["saved_as"], h["error"] = "", "", ""
        all_results.extend(hits)

    all_results = dedupe(all_results)
    print(f"[dedupe] {len(all_results)} unique links")

    for i, item in enumerate(all_results, 1):
        url = item["link"]
        print(f"[{i}/{len(all_results)}] Downloading: {url}")
        ok, info = download_pdf(url, item["title"])
        if ok:
            item["status"], item["saved_as"] = "downloaded", info
        else:
            item["status"], item["error"] = "skipped", info

    save_manifest(all_results)
    print(f"PDFs saved in: {OUT_DIR.resolve()}")

if __name__ == "__main__":
    main()
