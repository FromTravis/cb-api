"""
Fetches ECB Governing Council monetary policy statements directly from
ecb.europa.eu, summarises each once with Claude Haiku, and persists
summaries in .data/fed_summaries.json (shared summary store).

Links come from the ECB's internal yearly layout pages:
  https://www.ecb.europa.eu/press/govcdec/mopo/{year}/html/index_include.en.html

Each link contains a date-stamp (YYMMDD) which is extracted for exact chart
alignment.  Only statements from DEFAULT_START_DATE onward are returned.
"""

import re
import logging
import concurrent.futures
from datetime import datetime

import anthropic
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin

import cache
import fetchers.summary_store as summary_store
from config import DEFAULT_START_DATE, ANTHROPIC_API_KEY

logger = logging.getLogger(__name__)

BASE_URL        = "https://www.ecb.europa.eu"
HEADERS         = {"User-Agent": "Mozilla/5.0 (compatible; cb-api/1.0)"}
MAX_WORKERS     = 6
SUMMARIZE_CHARS = 8000

SUMMARY_PROMPT = (
    "Summarise this European Central Bank monetary policy statement in 2-3 sentences. "
    "Focus on the key rate decision, inflation outlook, or major policy shift. "
    "Be concise and factual."
)


# ── helpers ───────────────────────────────────────────────────────────────────

def _url_to_date(url: str):
    """
    Extract ISO date from URL like .../ecb.mp260611~....en.html → 2026-06-11.
    Returns (YYYY-MM-DD, YYYY-MM) or (None, None).
    """
    m = re.search(r'ecb\.mp(\d{2})(\d{2})(\d{2})', url)
    if not m:
        return None, None
    yy, mm, dd = m.group(1), m.group(2), m.group(3)
    iso = f"20{yy}-{mm}-{dd}"
    return iso, f"20{yy}-{mm}"


def _make_title(iso_date: str) -> str:
    try:
        dt = datetime.strptime(iso_date, "%Y-%m-%d")
        return f"ECB Statement – {dt.strftime(f'%B {dt.day}, %Y')}"
    except ValueError:
        return "ECB Governing Council Statement"


# ── calendar scrape ───────────────────────────────────────────────────────────

def get_ecb_dynamic_links(cutoff: str = DEFAULT_START_DATE[:7]) -> list[dict]:
    """
    Scrape all ECB monetary policy statement links since cutoff (YYYY-MM).
    Returns list of {url, d (YYYY-MM-DD), ym (YYYY-MM), title}.
    """
    start_year = int(cutoff[:4])
    current_year = datetime.now().year

    seen = set()
    links = []

    for year in range(start_year, current_year + 1):
        inc_url = (
            f"{BASE_URL}/press/govcdec/mopo/{year}/html/index_include.en.html"
        )
        try:
            r = requests.get(inc_url, headers=HEADERS, timeout=10)
            if r.status_code != 200:
                continue
        except requests.RequestException as e:
            logger.warning("ECB calendar %d unreachable: %s", year, e)
            continue

        soup = BeautifulSoup(r.text, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "html/ecb.mp" not in href or ".en.html" not in href:
                continue

            full_url = urljoin(BASE_URL, href)
            iso, ym = _url_to_date(full_url)
            if not iso or ym < cutoff or full_url in seen:
                continue

            seen.add(full_url)
            links.append({
                "url":   full_url,
                "d":     iso,
                "ym":    ym,
                "title": _make_title(iso),
            })

    return sorted(links, key=lambda x: x["d"])


# ── page text extraction ──────────────────────────────────────────────────────

def _fetch_page_text(url: str) -> str:
    """Fetch an ECB statement page and return the policy statement text."""
    r = requests.get(url, headers=HEADERS, timeout=15)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    # Find the first paragraph that opens the statement, then get its container
    anchors = ("The Governing Council", "At today", "Based on its")
    for p in soup.find_all("p"):
        txt = p.get_text(" ", strip=True)
        if any(txt.startswith(a) for a in anchors):
            parent = p.parent
            paras = parent.find_all("p")
            if len(paras) >= 2:
                return " ".join(pp.get_text(" ", strip=True) for pp in paras[:20])
            return txt

    # Fallback: all paragraphs with meaningful length
    all_p = [p.get_text(" ", strip=True) for p in soup.find_all("p") if len(p.get_text()) > 80]
    return " ".join(all_p[:15])


# ── summarisation ─────────────────────────────────────────────────────────────

def _summarize_one(text: str) -> str:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    resp = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=150,
        messages=[{
            "role": "user",
            "content": f"{SUMMARY_PROMPT}\n\n{text[:SUMMARIZE_CHARS]}"
        }]
    )
    result = resp.content[0].text.strip()
    if result.startswith("#"):
        result = result.split("\n", 1)[-1].strip()
    return result


def _fetch_and_summarize(link: dict, db: dict) -> tuple:
    """Fetch + summarise one link. Returns (url_key, content_key, summary)."""
    uk = f"url:{link['url']}"
    if uk in db:
        return uk, db[uk], None   # already stored
    try:
        text    = _fetch_page_text(link["url"])
        ck      = summary_store.key_for(text)
        if ck in db:
            return uk, ck, None   # content cached, just missing URL index
        summary = _summarize_one(text)
        return uk, ck, summary
    except Exception as e:
        logger.error("Failed to process ECB event %s: %s", link["url"], e)
        return None, None, None


# ── main entry point ──────────────────────────────────────────────────────────

def fetch(start_date: str = DEFAULT_START_DATE, force: bool = False) -> list[dict]:
    cache_key = f"ecb_events_{start_date[:7]}"
    if not force:
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

    cutoff = start_date[:7]
    logger.info("Scraping ECB statement links since %s", cutoff)
    links = get_ecb_dynamic_links(cutoff)
    logger.info("Found %d ECB statements", len(links))

    db = summary_store.load_all()
    missing = [lk for lk in links if f"url:{lk['url']}" not in db]

    if missing:
        logger.info("Summarising %d new ECB statements with Claude Haiku…", len(missing))
        new_entries = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {pool.submit(_fetch_and_summarize, lk, db): lk for lk in missing}
            for fut in concurrent.futures.as_completed(futures):
                uk, ck, summary = fut.result()
                if uk and ck:
                    new_entries[uk] = ck
                    if summary:
                        new_entries[ck] = summary
        if new_entries:
            db.update(new_entries)
            summary_store.save_all(db)
            logger.info("Summary store updated (%d total entries)", len(db))

    events = []
    for lk in links:
        uk = f"url:{lk['url']}"
        ck = db.get(uk)
        body = db.get(ck, "") if ck else ""
        events.append({
            "d":     lk["d"],
            "cat":   "cb",
            "title": lk["title"],
            "body":  body or f"[Content unavailable for {lk['title']}]",
        })

    events.sort(key=lambda e: e["d"])
    cache.set(cache_key, events)
    logger.info("ECB events ready: %d items from %s", len(events), cutoff)
    return events
