"""
Fetches Bank of England MPC Monetary Policy Summary and Minutes from:
  https://www.bankofengland.co.uk/monetary-policy-summary-and-minutes/{year}/{month}-{year}

Meeting discovery: brute-forces month-slugs by checking PDF existence (fast HEAD requests),
then fetches and summarises each HTML page once with Claude Haiku.
Summaries are persisted in the shared summary store (.data/fed_summaries.json).
"""

import re
import logging
import concurrent.futures
from datetime import datetime, date

import anthropic
import requests
from bs4 import BeautifulSoup

import cache
import fetchers.summary_store as summary_store
from config import DEFAULT_START_DATE, ANTHROPIC_API_KEY

logger = logging.getLogger(__name__)

BASE_URL    = "https://www.bankofengland.co.uk"
HEADERS     = {"User-Agent": "Mozilla/5.0 (compatible; cb-api/1.0)"}
MAX_WORKERS = 6
SUMMARIZE_CHARS = 8000

MONTHS = [
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
]

SUMMARY_PROMPT = (
    "Summarise this Bank of England Monetary Policy Committee statement in 2-3 sentences. "
    "Focus on the rate decision, vote split, inflation outlook, or major policy shift. "
    "Be concise and factual."
)


# ── meeting discovery ─────────────────────────────────────────────────────────

def _page_url(year: int, month: str) -> str:
    return f"{BASE_URL}/monetary-policy-summary-and-minutes/{year}/{month}-{year}"


def _exists(year: int, month: str) -> bool:
    """Check the HTML page URL directly — more reliable than PDF (which 302-redirects for older years)."""
    try:
        r = requests.head(_page_url(year, month), headers=HEADERS, timeout=5,
                          allow_redirects=True)
        return r.status_code == 200
    except requests.RequestException:
        return False


def _discover_meetings(cutoff: str = DEFAULT_START_DATE[:7]) -> list[dict]:
    """
    Find all MPC meetings since cutoff (YYYY-MM). Never goes before DEFAULT_START_DATE.
    Returns sorted list of {url, d (YYYY-MM-DD), title}.
    """
    # Floor: never go back further than DEFAULT_START_DATE
    floor = DEFAULT_START_DATE[:7]
    if cutoff < floor:
        cutoff = floor

    start_year = int(cutoff[:4])
    start_month = int(cutoff[5:7])
    current = date.today()

    candidates = []
    for year in range(start_year, current.year + 1):
        for mo_idx, month in enumerate(MONTHS, start=1):
            if year == start_year and mo_idx < start_month:
                continue
            if year == current.year and mo_idx > current.month:
                break
            candidates.append((year, month, mo_idx))

    logger.info("Checking %d month slots for BoE meetings…", len(candidates))

    meetings = []
    # Parallel HEAD checks
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        futures = {pool.submit(_exists, y, m): (y, m, mi) for y, m, mi in candidates}
        for fut in concurrent.futures.as_completed(futures):
            y, month, mi = futures[fut]
            if fut.result():
                dt = datetime(y, mi, 1)
                meetings.append({
                    "url":   _page_url(y, month),
                    "d":     dt.strftime("%Y-%m-01"),   # refined below from page
                    "year":  y,
                    "month": month,
                    "title": f"BoE MPC – {month.capitalize()} {y}",
                })

    return sorted(meetings, key=lambda m: m["d"])


# ── page text extraction ──────────────────────────────────────────────────────

def _fetch_page_text(url: str) -> tuple[str, str]:
    """
    Fetch a BoE MPC page and extract (text, iso_date).
    Returns (statement_text, YYYY-MM-DD).
    """
    r = requests.get(url, headers=HEADERS, timeout=15)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    # Extract publication date
    iso_date = ""
    date_el = soup.find(string=re.compile(r"\d{1,2}\s+\w+\s+\d{4}"))
    if date_el:
        m = re.search(r"(\d{1,2}\s+\w+\s+\d{4})", date_el)
        if m:
            try:
                iso_date = datetime.strptime(m.group(1).strip(), "%d %B %Y").strftime("%Y-%m-%d")
            except ValueError:
                try:
                    iso_date = datetime.strptime(m.group(1).strip(), "%d %b %Y").strftime("%Y-%m-%d")
                except ValueError:
                    pass

    # Extract statement text from page-section elements
    sections = soup.select(".page-section")
    text_parts = []
    for sec in sections:
        t = sec.get_text(" ", strip=True)
        if any(kw in t for kw in ["Bank Rate", "MPC", "inflation", "Monetary Policy Committee"]):
            text_parts.append(t)
            if sum(len(p) for p in text_parts) > SUMMARIZE_CHARS:
                break

    text = " ".join(text_parts)
    if not text:
        # Fallback: all paragraphs with relevant content
        paras = [p.get_text(" ", strip=True) for p in soup.find_all("p")
                 if len(p.get_text()) > 60]
        text = " ".join(paras[:20])

    return text, iso_date


# ── summarisation ─────────────────────────────────────────────────────────────

def _summarize_one(text: str) -> str:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    resp = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=150,
        messages=[{"role": "user", "content": f"{SUMMARY_PROMPT}\n\n{text[:SUMMARIZE_CHARS]}"}]
    )
    result = resp.content[0].text.strip()
    if result.startswith("#"):
        result = result.split("\n", 1)[-1].strip()
    return result


def _process_meeting(meeting: dict, db: dict) -> tuple:
    """Fetch + summarise one meeting. Returns (url_key, content_key, summary, iso_date)."""
    uk = f"url:{meeting['url']}"
    if uk in db:
        return uk, db[uk], None, None
    try:
        text, iso_date = _fetch_page_text(meeting["url"])
        ck = summary_store.key_for(text)
        if ck in db:
            return uk, ck, None, iso_date
        summary = _summarize_one(text)
        return uk, ck, summary, iso_date
    except Exception as e:
        logger.error("Failed to process BoE meeting %s: %s", meeting["url"], e)
        return None, None, None, None


# ── main entry point ──────────────────────────────────────────────────────────

def fetch(start_date: str = DEFAULT_START_DATE, force: bool = False) -> list[dict]:
    cache_key = f"boe_events_{start_date[:7]}"
    if not force:
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

    cutoff = start_date[:7]
    logger.info("Discovering BoE MPC meetings since %s", cutoff)
    meetings = _discover_meetings(cutoff)
    logger.info("Found %d BoE MPC meetings", len(meetings))

    db = summary_store.load_all()
    missing = [m for m in meetings if f"url:{m['url']}" not in db]

    if missing:
        logger.info("Summarising %d new BoE MPC pages with Claude Haiku…", len(missing))
        new_entries = {}
        iso_dates = {}

        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {pool.submit(_process_meeting, m, db): m for m in missing}
            for fut in concurrent.futures.as_completed(futures):
                mtg = futures[fut]
                uk, ck, summary, iso_date = fut.result()
                if uk and ck:
                    new_entries[uk] = ck
                    if summary:
                        new_entries[ck] = summary
                    if iso_date:
                        iso_dates[uk] = iso_date

        if new_entries:
            db.update(new_entries)
            summary_store.save_all(db)
            logger.info("Summary store updated (%d total entries)", len(db))

        # Store discovered dates for use below
        for m in missing:
            uk = f"url:{m['url']}"
            if uk in iso_dates:
                m["d"] = iso_dates[uk]

    events = []
    for m in meetings:
        uk = f"url:{m['url']}"
        ck = db.get(uk)
        body = db.get(ck, "") if ck else ""

        # Refine date from page if cached as YYYY-MM-01 placeholder
        d = m["d"]

        events.append({
            "d":     d,
            "cat":   "cb",
            "title": m["title"],
            "body":  body or f"[Content unavailable for {m['title']}]",
        })

    events.sort(key=lambda e: e["d"])
    cache.set(cache_key, events)
    logger.info("BoE events ready: %d meetings from %s", len(events), cutoff)
    return events
