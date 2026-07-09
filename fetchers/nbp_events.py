"""
Fetches NBP (National Bank of Poland) MPC events via the WordPress REST API.
The nbp.pl website is behind Imperva WAF, but the WP REST API bypasses it.

Category IDs:
  1656 — mpc-press-release  (Information from MPC meeting, cat="cb")
  1297 — mpc-minutes        (MPC minutes, cat="speech")

Posts include full content in content.rendered — no individual page scraping needed.
Summaries are generated with Claude Haiku and persisted in the shared summary store.
"""

import re
import logging
import concurrent.futures
from datetime import datetime
from html import unescape

import anthropic
import requests

import cache
import fetchers.summary_store as summary_store
from config import DEFAULT_START_DATE, ANTHROPIC_API_KEY

logger = logging.getLogger(__name__)

BASE_URL    = "https://nbp.pl/wp-json/wp/v2"
HEADERS     = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "application/json",
}
MAX_WORKERS = 6
SUMMARIZE_CHARS = 8000

CATEGORIES = {
    1656: {"cat": "cb",     "label": "NBP MPC Press Release"},
    1297: {"cat": "speech", "label": "NBP MPC Minutes"},
}

SUMMARY_PROMPTS = {
    "cb":     ("Summarise this National Bank of Poland (NBP) Monetary Policy Council "
               "press release in 2-3 sentences. Focus on the rate decision, vote outcome, "
               "and key policy reasoning. Be concise and factual."),
    "speech": ("Summarise this NBP Monetary Policy Council minutes in 2-3 sentences. "
               "Focus on the key economic outlook and policy discussion. Be concise."),
}


# ── helpers ───────────────────────────────────────────────────────────────────

def _html_to_text(html: str) -> str:
    """Strip HTML tags and normalise whitespace."""
    text = re.sub(r'<[^>]+>', ' ', html)
    return re.sub(r'\s+', ' ', unescape(text)).strip()


def _extract_date(date_str: str) -> str:
    """Convert WP date '2026-06-02T10:00:00' → 'YYYY-MM-DD'."""
    return date_str[:10]


# ── fetch from WP REST API ────────────────────────────────────────────────────

def _fetch_posts(category_id: int, cutoff: str) -> list[dict]:
    """Fetch all posts in a category published on or after cutoff (YYYY-MM)."""
    posts, page = [], 1
    while True:
        r = requests.get(
            f"{BASE_URL}/posts",
            params={
                "lang":       "en",
                "categories": category_id,
                "per_page":   100,
                "page":       page,
                "orderby":    "date",
                "order":      "desc",
                "_fields":    "id,date,title,content,link",
            },
            headers=HEADERS,
            timeout=15,
        )
        if r.status_code == 400:   # no more pages
            break
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break

        reached_cutoff = False
        for p in batch:
            iso = _extract_date(p["date"])
            if iso[:7] < cutoff:
                reached_cutoff = True
                break
            posts.append(p)

        if reached_cutoff or len(batch) < 100:
            break
        page += 1

    return posts


# ── summarisation ─────────────────────────────────────────────────────────────

def _summarize_one(text: str, cat: str) -> str:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    prompt = SUMMARY_PROMPTS.get(cat, SUMMARY_PROMPTS["cb"])
    resp = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=150,
        messages=[{"role": "user", "content": f"{prompt}\n\n{text[:SUMMARIZE_CHARS]}"}]
    )
    result = resp.content[0].text.strip()
    if result.startswith("#"):
        result = result.split("\n", 1)[-1].strip()
    return result


def _process(post: dict, cat: str, db: dict) -> tuple:
    url   = post["link"]
    uk    = f"url:{url}"
    title = _html_to_text(post.get("title", {}).get("rendered", ""))

    try:
        text = _html_to_text(post.get("content", {}).get("rendered", ""))

        # Minutes posts are notification stubs (< 200 chars) — the full text
        # is a PDF behind a WAF. Use a descriptive template instead of Claude.
        if not text or len(text) < 200:
            if cat == "speech":
                ck = summary_store.key_for(uk)
                if ck in db:
                    return uk, ck, None   # template already stored
                # Extract date from title: "...meeting held on 9 November 2022"
                import re as _re
                date_m = _re.search(r'held on (.+?)$', title, _re.I)
                date_part = date_m.group(1).strip() if date_m else title
                summary = (f"The NBP Monetary Policy Council meeting held on {date_part}. "
                           f"Full minutes available as a PDF on the NBP website.")
                return uk, ck, summary
            else:
                return None, None, None   # press release with no content — skip

        ck = summary_store.key_for(text)
        if ck in db:
            # Only skip if the stored summary looks like a real one (not Claude's refusal)
            existing = db.get(ck, "")
            if existing and "don't have access" not in existing and len(existing) > 50:
                return uk, ck, None
        summary = _summarize_one(text, cat)
        return uk, ck, summary
    except Exception as e:
        logger.error("Failed to process NBP post %s: %s", url, e)
        return None, None, None


# ── background generation ─────────────────────────────────────────────────────

_bg_lock    = concurrent.futures.ThreadPoolExecutor(max_workers=1)
_bg_running: set = set()


def _generate_and_cache(cache_key: str, posts_by_cat: dict) -> None:
    try:
        db = summary_store.load_all()
        missing = [
            (p, cat)
            for cat, posts in posts_by_cat.items()
            for p in posts
            if f"url:{p['link']}" not in db
        ]
        if not missing:
            return

        logger.info("Background: summarising %d new NBP events…", len(missing))
        new_entries = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {pool.submit(_process, p, cat, db): (p, cat) for p, cat in missing}
            for fut in concurrent.futures.as_completed(futures):
                uk, ck, summary = fut.result()
                if uk and ck:
                    new_entries[uk] = ck
                    if summary:
                        new_entries[ck] = summary
        if new_entries:
            db.update(new_entries)
            summary_store.save_all(db)
            logger.info("Background: NBP store updated (%d total entries)", len(db))
            cache.invalidate(cache_key)
    finally:
        _bg_running.discard(cache_key)


# ── main entry point ──────────────────────────────────────────────────────────

def fetch(start_date: str = DEFAULT_START_DATE, force: bool = False) -> list[dict]:
    cache_key = f"nbp_events_v2_{start_date[:7]}"
    if not force:
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

    cutoff = start_date[:7]

    # Cache link list separately (avoids re-fetching paginated API on every request)
    # When force=True (daily refresh), clear it so new meetings are picked up.
    links_key  = f"nbp_links_v2_{cutoff}"
    if force:
        cache.invalidate(links_key)
    posts_by_cat = cache.get(links_key)
    if posts_by_cat is None:
        logger.info("Fetching NBP MPC posts since %s", cutoff)
        posts_by_cat = {}
        for cat_id, info in CATEGORIES.items():
            posts = _fetch_posts(cat_id, cutoff)
            posts_by_cat[info["cat"]] = posts
            logger.info("  %s: %d posts", info["label"], len(posts))
        cache.set(links_key, posts_by_cat)

    # Kick off background generation for any missing summaries
    db = summary_store.load_all()
    all_posts = [p for posts in posts_by_cat.values() for p in posts]
    missing = [p for p in all_posts if f"url:{p['link']}" not in db]

    if missing and cache_key not in _bg_running:
        _bg_running.add(cache_key)
        _bg_lock.submit(_generate_and_cache, cache_key, posts_by_cat)
        logger.info("Background NBP summary job started (%d missing)", len(missing))

    # Build events immediately from whatever is in the store
    events = []
    for cat, posts in posts_by_cat.items():
        for p in posts:
            iso   = _extract_date(p["date"])
            url   = p["link"]
            uk    = f"url:{url}"
            ck    = db.get(uk)
            body  = db.get(ck, "") if ck else ""
            title = _html_to_text(p["title"].get("rendered", ""))
            events.append({
                "d":     iso,
                "cat":   cat,
                "title": title,
                "body":  body or f"[Content unavailable for {title}]",
            })

    events.sort(key=lambda e: (e["d"], e["cat"]))
    cache.set(cache_key, events)
    logger.info("NBP events ready: %d items from %s", len(events), cutoff)
    return events
