"""
Fetches MNB (Magyar Nemzeti Bank / National Bank of Hungary) Monetary Council
press releases from mnb.hu.

Only links containing 'press-release-on-the-monetary-council-meeting-of' are used.
Date is extracted directly from the URL slug: meeting-of-23-june-2026 → 2026-06-23.
Page text is extracted via the .content CSS selector.
Summaries are generated once with Claude Haiku and persisted in the shared store.
"""

import re
import logging
import concurrent.futures
from datetime import date, datetime

import anthropic
import requests
from bs4 import BeautifulSoup

import cache
import fetchers.summary_store as summary_store
from config import DEFAULT_START_DATE, ANTHROPIC_API_KEY

logger = logging.getLogger(__name__)

BASE_URL    = "https://www.mnb.hu"
LIST_URL    = f"{BASE_URL}/en/monetary-policy/the-monetary-council/press-releases"
HEADERS     = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
MAX_WORKERS = 6
SUMMARIZE_CHARS = 8000

SUMMARY_PROMPT = (
    "Summarise this Magyar Nemzeti Bank (MNB) Monetary Council press release in "
    "2-3 sentences. Focus on the rate decision, any change in basis points, and "
    "the key economic reasoning. Be concise and factual."
)


# ── date extraction ───────────────────────────────────────────────────────────

def _slug_to_iso(slug: str):
    """Extract ISO date from URL slug like 'meeting-of-23-june-2026' → '2026-06-23'."""
    m = re.search(r'meeting-of-(\d{1,2})-(\w+)-(\d{4})$', slug)
    if not m:
        return None
    try:
        dt = datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", "%d %B %Y")
        return dt.strftime("%Y-%m-%d")
    except ValueError:
        return None


def _make_title(iso: str) -> str:
    try:
        dt = datetime.strptime(iso, "%Y-%m-%d")
        return f"MNB Monetary Council – {dt.strftime(f'%B {dt.day}, %Y')}"
    except ValueError:
        return "MNB Monetary Council"


# ── link discovery ────────────────────────────────────────────────────────────

def _discover_links(cutoff: str) -> list[dict]:
    """
    Scrape all MNB press release links since cutoff (YYYY-MM).
    Checks the main listing page + year-specific archive pages.
    """
    start_year = int(cutoff[:4])
    current_year = date.today().year
    seen = set()
    links = []

    urls_to_check = [LIST_URL] + [
        f"{LIST_URL}/{year}"
        for year in range(start_year, current_year + 1)
    ]

    for url in urls_to_check:
        try:
            r = requests.get(url, headers=HEADERS, timeout=10)
            if r.status_code != 200:
                continue
        except requests.RequestException:
            continue

        for href in re.findall(
            r'href=["\']([^"\']*press-release-on-the-monetary-council-meeting-of[^"\']*)["\']',
            r.text, re.I
        ):
            # Normalise: "//www.mnb.hu/..." → "https://www.mnb.hu/..."
            full_url = ("https:" + href) if href.startswith("//") else href
            if not full_url.startswith("http"):
                full_url = BASE_URL + href

            if full_url in seen:
                continue

            slug = full_url.rstrip("/").split("/")[-1]
            iso = _slug_to_iso(slug)
            if not iso or iso[:7] < cutoff:
                continue

            seen.add(full_url)
            links.append({
                "url":   full_url,
                "d":     iso,
                "cat":   "cb",
                "title": _make_title(iso),
            })

    return sorted(links, key=lambda l: l["d"])


# ── content extraction ────────────────────────────────────────────────────────

def _fetch_page_text(url: str) -> str:
    r = requests.get(url, headers=HEADERS, timeout=15)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    el = soup.select_one(".content")
    if el:
        text = el.get_text(" ", strip=True)
        if len(text) > 200:
            return re.sub(r"\s+", " ", text).strip()

    # Fallback: collect paragraphs with monetary policy keywords
    paras = [
        p.get_text(" ", strip=True)
        for p in soup.find_all("p")
        if len(p.get_text(strip=True)) > 60
        and any(kw in p.get_text() for kw in ["rate", "Council", "MNB", "percent", "basis"])
    ]
    return re.sub(r"\s+", " ", " ".join(paras[:20])).strip()


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
        if not text or len(text) < 100:
            return None, None, None
        ck = summary_store.key_for(text)
        if ck in db:
            return uk, ck, None
        summary = _summarize_one(text)
        return uk, ck, summary
    except Exception as e:
        logger.error("Failed to process MNB event %s: %s", link["url"], e)
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
        logger.info("Background: summarising %d new MNB events…", len(missing))
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
            logger.info("Background: MNB store updated (%d total entries)", len(db))
            cache.invalidate(cache_key)
    finally:
        _bg_running.discard(cache_key)


# ── main entry point ──────────────────────────────────────────────────────────

def fetch(start_date: str = DEFAULT_START_DATE, force: bool = False) -> list[dict]:
    cache_key = f"mnb_events_{start_date[:7]}"
    if not force:
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

    cutoff = start_date[:7]

    links_key = f"mnb_links_{cutoff}"
    links = cache.get(links_key)
    if links is None:
        logger.info("Discovering MNB press release links since %s", cutoff)
        links = _discover_links(cutoff)
        cache.set(links_key, links)
        logger.info("Found %d MNB press releases", len(links))

    db = summary_store.load_all()
    missing = [l for l in links if f"url:{l['url']}" not in db]

    if missing and cache_key not in _bg_running:
        _bg_running.add(cache_key)
        _bg_lock.submit(_generate_and_cache, cache_key, links)
        logger.info("Background MNB summary job started (%d missing)", len(missing))

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
    logger.info("MNB events ready: %d items from %s", len(events), cutoff)
    return events
