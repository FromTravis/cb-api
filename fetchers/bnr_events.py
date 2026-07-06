"""
Fetches NBR (National Bank of Romania) Board monetary policy decision events.

The BNR website is a fully JavaScript-rendered SPA — HTML content is not
accessible programmatically. Instead we:
  1. Read decision dates from a curated URL list (bnr_statement_links.txt)
     that the user maintains. Date is embedded in each URL.
  2. Fetch the policy rate on each decision date from the BIS CBPOL API
     (same source used for the Romania chart rate series).
  3. Compare with the previous decision's rate to determine the action
     (raised / cut / held unchanged).
  4. Use Claude Haiku to write a concise 2-3 sentence factual summary.
  5. Persist summaries in the shared store (one Claude call per decision ever).
"""

import os
import re
import logging
import concurrent.futures
from datetime import datetime, date

import anthropic
import requests

import cache
import fetchers.summary_store as summary_store
from config import DEFAULT_START_DATE, ANTHROPIC_API_KEY

logger = logging.getLogger(__name__)

LINKS_FILE  = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                           "bnr_statement_links.txt")
BIS_RATE_URL = "https://stats.bis.org/api/v1/data/BIS,WS_CBPOL,1.0/D.RO/all"
HEADERS      = {
    "User-Agent": "Mozilla/5.0 (compatible; cb-api/1.0)",
    "Accept":     "application/vnd.sdmx.data+json",
}
MAX_WORKERS = 4

SUMMARY_PROMPT = (
    "Write a 2-3 sentence factual summary of this NBR (National Bank of Romania) "
    "Board monetary policy decision. Include: (1) the date, (2) whether the rate "
    "was raised, cut, or held, (3) the new rate level, (4) brief context if available. "
    "Be concise and factual."
)


# ── URL parsing ───────────────────────────────────────────────────────────────

def _parse_url(url: str):
    """
    Extract date and build title from a BNR URL like:
      .../25486-2026-05-15-nbr-board-decisions-on-monetary-policy
    Returns (iso_date, title) or (None, None).
    """
    m = re.search(r'(\d{4}-\d{2}-\d{2})-nbr-board-decisions', url)
    if not m:
        return None, None
    iso = m.group(1)
    try:
        dt = datetime.strptime(iso, "%Y-%m-%d")
        title = f"NBR Board Decision – {dt.strftime(f'%B {dt.day}, %Y')}"
        return iso, title
    except ValueError:
        return None, None


def _read_links(cutoff: str) -> list[dict]:
    """Read and parse bnr_statement_links.txt, filtering to cutoff date."""
    links = []
    try:
        with open(LINKS_FILE) as f:
            for line in f:
                url = line.strip()
                if not url:
                    continue
                iso, title = _parse_url(url)
                if iso and iso[:7] >= cutoff:
                    links.append({"url": url, "d": iso, "cat": "cb", "title": title})
    except FileNotFoundError:
        logger.warning("bnr_statement_links.txt not found")
    return sorted(links, key=lambda l: l["d"])


# ── rate data ─────────────────────────────────────────────────────────────────

def _fetch_bis_rates(start_date: str) -> dict:
    """
    Fetch BIS daily policy rates for Romania.
    Returns {date_str: rate_float}.
    """
    cache_key = f"bis_bnr_rates_{start_date[:7]}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        r = requests.get(
            BIS_RATE_URL,
            params={"startPeriod": start_date[:10]},
            headers=HEADERS,
            timeout=20,
        )
        r.raise_for_status()
        payload   = r.json()
        data      = payload.get("data", payload)
        ds        = data["dataSets"][0]
        structure = data["structure"]
        time_dim  = next(x for x in structure["dimensions"]["observation"] if x["id"] == "TIME_PERIOD")
        tv        = time_dim["values"]
        sk        = next(iter(ds["series"]))
        obs       = ds["series"][sk]["observations"]

        import math
        result = {}
        for idx_str, values in obs.items():
            period = tv[int(idx_str)]["id"]
            raw    = values[0]
            if raw is None:
                continue
            try:
                val = float(raw)
                if not math.isnan(val):
                    result[period] = val
            except (ValueError, TypeError):
                pass
        cache.set(cache_key, result)
        return result
    except Exception as e:
        logger.warning("Could not fetch BIS Romania rates: %s", e)
        return {}


def _rate_on_or_before(rates: dict, iso: str) -> float | None:
    """Return the rate on iso or the most recent prior date."""
    candidates = {d: v for d, v in rates.items() if d <= iso}
    if not candidates:
        return None
    return candidates[max(candidates)]


# ── summarisation ─────────────────────────────────────────────────────────────

def _summarize_one(link: dict, rate: float | None, prev_rate: float | None) -> str:
    """Build the fact string and ask Claude Haiku for a polished summary."""
    iso = link["d"]
    dt  = datetime.strptime(iso, "%Y-%m-%d")
    date_str = dt.strftime(f"%B {dt.day}, %Y")

    if rate is None:
        fact = (f"At its {date_str} meeting, the NBR Board reviewed monetary policy. "
                f"Rate data unavailable for this date.")
    elif prev_rate is None or abs(rate - prev_rate) < 0.001:
        fact = (f"At its {date_str} meeting, the NBR Board held the key policy rate "
                f"unchanged at {rate:.2f}%.")
    elif rate > prev_rate:
        change = rate - prev_rate
        fact = (f"At its {date_str} meeting, the NBR Board raised the key policy rate "
                f"by {change:.2f} percentage points to {rate:.2f}%.")
    else:
        change = prev_rate - rate
        fact = (f"At its {date_str} meeting, the NBR Board cut the key policy rate "
                f"by {change:.2f} percentage points to {rate:.2f}%.")

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        resp = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=150,
            messages=[{"role": "user", "content": f"{SUMMARY_PROMPT}\n\nFacts: {fact}"}]
        )
        result = resp.content[0].text.strip()
        if result.startswith("#"):
            result = result.split("\n", 1)[-1].strip()
        return result
    except Exception as e:
        logger.error("Claude failed for BNR %s: %s", iso, e)
        return fact   # fall back to the plain fact string


# ── background generation ─────────────────────────────────────────────────────

_bg_lock    = concurrent.futures.ThreadPoolExecutor(max_workers=1)
_bg_running: set = set()


def _generate_and_cache(cache_key: str, links: list, rates: dict) -> None:
    try:
        db = summary_store.load_all()
        # Sort links by date to compute prev_rate correctly
        sorted_links = sorted(links, key=lambda l: l["d"])
        missing = [l for l in sorted_links if f"url:{l['url']}" not in db]
        if not missing:
            return

        logger.info("Background: summarising %d new NBR decisions…", len(missing))
        new_entries = {}

        # Process sequentially to track prev_rate correctly
        for lk in missing:
            uk = f"url:{lk['url']}"
            try:
                rate = _rate_on_or_before(rates, lk["d"])
                # Find previous decision date (last link before this one)
                prev_links = [l for l in sorted_links if l["d"] < lk["d"]]
                prev_rate  = _rate_on_or_before(rates, prev_links[-1]["d"]) if prev_links else None

                ck      = summary_store.key_for(uk)   # key on URL itself
                summary = _summarize_one(lk, rate, prev_rate)
                new_entries[uk] = ck
                new_entries[ck] = summary
            except Exception as e:
                logger.error("Failed NBR %s: %s", lk["d"], e)

        if new_entries:
            db.update(new_entries)
            summary_store.save_all(db)
            logger.info("Background: NBR store updated (%d total)", len(db))
            cache.invalidate(cache_key)
    finally:
        _bg_running.discard(cache_key)


# ── main entry point ──────────────────────────────────────────────────────────

def fetch(start_date: str = DEFAULT_START_DATE, force: bool = False) -> list[dict]:
    cache_key = f"bnr_events_{start_date[:7]}"
    if not force:
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

    cutoff = start_date[:7]
    links  = _read_links(cutoff)
    logger.info("BNR events: %d links from file since %s", len(links), cutoff)

    if not links:
        return []

    # Fetch rate data to power the summaries
    rates = _fetch_bis_rates(start_date)

    db      = summary_store.load_all()
    missing = [l for l in links if f"url:{l['url']}" not in db]

    if missing and cache_key not in _bg_running:
        _bg_running.add(cache_key)
        _bg_lock.submit(_generate_and_cache, cache_key, links, rates)
        logger.info("Background BNR summary job started (%d missing)", len(missing))

    events = []
    for lk in links:
        uk   = f"url:{lk['url']}"
        ck   = db.get(uk)
        body = db.get(ck, "") if ck else ""
        events.append({
            "d":     lk["d"],
            "cat":   lk["cat"],
            "title": lk["title"],
            "body":  body or f"[Generating summary for {lk['title']}…]",
        })

    events.sort(key=lambda e: e["d"])
    cache.set(cache_key, events)
    logger.info("BNR events ready: %d items from %s", len(events), cutoff)
    return events
