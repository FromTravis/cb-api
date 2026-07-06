"""
Fetches Czech National Bank (CNB) Bank Board monetary policy decisions
from https://www.cnb.cz/en/monetary-policy/bank-board-decisions/

Architecture:
  The CNB website uses Apollo/OpenCMS. Individual decision pages are available
  at timestamp-based URLs: /CNB-Board-decisions-{unix_ms}/
  Only 2026+ decisions have individual pages (pre-2026 content is behind a
  JS-rendered archive that is not accessible programmatically).

  Strategy:
  1. Scrape the main page to get the current-year meeting schedule.
  2. Convert each meeting date to a Unix-millisecond timestamp URL.
  3. For past years: discover accessible pages by checking all Thursdays in
     the 48 year/month pairs encoded in the page's JavaScript month selectors.
  4. Fetch the decision text from each accessible page.
  5. Summarise with Claude Haiku and persist in the shared store.
"""

import re
import logging
import calendar
import time
import concurrent.futures
from datetime import datetime, timezone

import anthropic
import requests
from bs4 import BeautifulSoup

import cache
import fetchers.summary_store as summary_store
from config import DEFAULT_START_DATE, ANTHROPIC_API_KEY

logger = logging.getLogger(__name__)

BASE_URL    = "https://www.cnb.cz"
MAIN_URL    = f"{BASE_URL}/en/monetary-policy/bank-board-decisions/"
HEADERS     = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
MAX_WORKERS = 4
SUMMARIZE_CHARS = 8000

MONTHS_EN = {
    'January':1,'February':2,'March':3,'April':4,'May':5,'June':6,
    'July':7,'August':8,'September':9,'October':10,'November':11,'December':12
}

SUMMARY_PROMPT = (
    "Summarise this Czech National Bank (CNB) Bank Board monetary policy decision "
    "in 2-3 sentences. Focus on the rate action, the new rate level, and the "
    "key economic reasoning. Be concise and factual."
)


# ── timezone / URL helpers ────────────────────────────────────────────────────

def _prague_utc_hour(year: int, month: int, day: int) -> int:
    """Return UTC hour for 9:00 AM Prague (CET or CEST)."""
    last_sun_mar = max(d for d in range(25, 32) if datetime(year, 3, d).weekday() == 6)
    last_sun_oct = max(d for d in range(25, 32) if datetime(year, 10, d).weekday() == 6)
    summer = (month > 3 or (month == 3 and day >= last_sun_mar)) and \
             (month < 10 or (month == 10 and day < last_sun_oct))
    return 7 if summer else 8


def _date_to_url(year: int, month: int, day: int) -> str:
    h  = _prague_utc_hour(year, month, day)
    dt = datetime(year, month, day, h, 0, 0, tzinfo=timezone.utc)
    ts = int(dt.timestamp()) * 1000
    return f"{BASE_URL}/en/monetary-policy/bank-board-decisions/CNB-Board-decisions-{ts}/"


# ── meeting discovery ─────────────────────────────────────────────────────────

def _get_year_months(cutoff: str) -> list[tuple[int, int]]:
    """
    Scrape the main page for all year/month pairs available in the archive
    since cutoff (YYYY-MM).
    """
    try:
        r = requests.get(MAIN_URL, headers=HEADERS, timeout=15)
        calls = re.findall(
            r"onMonthSelect\('la_ea6e70dc',\s*'[^']+',\s*'[^']+',\s*'(\d{4})',\s*'(\w+)'\)",
            r.text
        )
        result = []
        for year_str, month_str in calls:
            y, m = int(year_str), MONTHS_EN.get(month_str, 0)
            if m and f"{y:04d}-{m:02d}" >= cutoff:
                result.append((y, m))
        return sorted(set(result))
    except Exception as e:
        logger.warning("Could not scrape CNB main page: %s", e)
        return []


def _check_url(url: str) -> bool:
    """Return True if the decision page exists and has a published decision (not a future placeholder)."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=8)
        if r.status_code != 200 or 'CNB Board decisions' not in r.text:
            return False
        # Reject placeholder pages for future meetings ("To be published on...")
        if 'To be published' in r.text:
            return False
        return True
    except Exception:
        return False


def _discover_decisions(year_months: list[tuple[int, int]]) -> list[dict]:
    """
    For each (year, month) pair, check all Thursdays to find the decision date.
    Returns sorted list of {url, d (YYYY-MM-DD), title}.
    """
    # Build Thursday candidates
    candidates = []
    for year, month in year_months:
        days_in_month = calendar.monthrange(year, month)[1]
        for day in range(1, days_in_month + 1):
            if datetime(year, month, day).weekday() == 3:  # Thursday
                url = _date_to_url(year, month, day)
                candidates.append((year, month, day, url))

    logger.info("Checking %d Thursday candidates for CNB decisions…", len(candidates))

    found = []
    # Sequential with small delay to avoid rate-limiting
    for year, month, day, url in candidates:
        if _check_url(url):
            iso = f"{year}-{month:02d}-{day:02d}"
            dt  = datetime(year, month, day)
            found.append({
                "url":   url,
                "d":     iso,
                "cat":   "cb",
                "title": f"CNB Bank Board – {dt.strftime(f'%B {day}, %Y')}",
            })
        time.sleep(0.3)

    return sorted(found, key=lambda l: l["d"])


# ── content extraction ────────────────────────────────────────────────────────

def _fetch_page_text(url: str) -> str:
    r = requests.get(url, headers=HEADERS, timeout=15)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    main = soup.find("main")
    if main:
        text = re.sub(r"\s+", " ", main.get_text(" ", strip=True)).strip()
        # Strip the repeated page title prefix
        text = re.sub(r"^Board decision[\d\s.]+CNB Board decisions\s*", "", text)
        return text
    return ""


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


def _process(link: dict, db: dict) -> tuple:
    uk = f"url:{link['url']}"
    if uk in db:
        return uk, db[uk], None
    try:
        text = _fetch_page_text(link["url"])
        if not text or len(text) < 50:
            return None, None, None
        ck = summary_store.key_for(text)
        if ck in db:
            return uk, ck, None
        summary = _summarize_one(text)
        return uk, ck, summary
    except Exception as e:
        logger.error("Failed to process CNB decision %s: %s", link["url"], e)
        return None, None, None


# ── background generation ─────────────────────────────────────────────────────

_bg_lock    = concurrent.futures.ThreadPoolExecutor(max_workers=1)
_bg_running: set = set()


def _generate_and_cache(cache_key: str, links: list) -> None:
    try:
        db = summary_store.load_all()
        missing = [l for l in links if f"url:{l['url']}" not in db]
        if not missing:
            return
        logger.info("Background: summarising %d CNB decisions…", len(missing))
        new_entries = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {pool.submit(_process, lk, db): lk for lk in missing}
            for fut in concurrent.futures.as_completed(futures):
                uk, ck, summary = fut.result()
                if uk and ck:
                    new_entries[uk] = ck
                    if summary:
                        new_entries[ck] = summary
        if new_entries:
            db.update(new_entries)
            summary_store.save_all(db)
            logger.info("Background: CNB store updated (%d total)", len(db))
            cache.invalidate(cache_key)
    finally:
        _bg_running.discard(cache_key)


# ── main entry point ──────────────────────────────────────────────────────────

def fetch(start_date: str = DEFAULT_START_DATE, force: bool = False) -> list[dict]:
    cache_key = f"cnb_events_{start_date[:7]}"
    if not force:
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

    cutoff = start_date[:7]

    # Cache the discovered links to avoid re-running slow Thursday checks
    links_key = f"cnb_links_{cutoff}"
    links = cache.get(links_key)
    if links is None:
        year_months = _get_year_months(cutoff)
        logger.info("Discovering CNB decisions for %d year/months…", len(year_months))
        links = _discover_decisions(year_months)
        cache.set(links_key, links)
        logger.info("Found %d accessible CNB decision pages", len(links))

    db = summary_store.load_all()
    missing = [l for l in links if f"url:{l['url']}" not in db]

    if missing and cache_key not in _bg_running:
        _bg_running.add(cache_key)
        _bg_lock.submit(_generate_and_cache, cache_key, links)
        logger.info("Background CNB summary job started (%d missing)", len(missing))

    events = []
    for lk in links:
        uk   = f"url:{lk['url']}"
        ck   = db.get(uk)
        body = db.get(ck, "") if ck else ""
        events.append({
            "d":     lk["d"],
            "cat":   lk["cat"],
            "title": lk["title"],
            "body":  body or f"[Content unavailable for {lk['title']}]",
        })

    events.sort(key=lambda e: e["d"])
    cache.set(cache_key, events)
    logger.info("CNB events ready: %d items from %s", len(events), cutoff)
    return events
