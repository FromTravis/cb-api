"""
Fetches Czech National Bank (CNB) Bank Board monetary policy decision events
from local PDF files in the cnb_meetings/ folder.

File naming convention: DD-MM-YYYY.pdf  (same as bnr_events)
pdfplumber extracts full press release text.
Claude Haiku summarises each once and persists in the shared store.
"""

import os
import re
import logging
import concurrent.futures
from datetime import datetime

import anthropic

import cache
import fetchers.summary_store as summary_store
from config import DEFAULT_START_DATE, ANTHROPIC_API_KEY

logger = logging.getLogger(__name__)

PDF_FOLDER  = os.path.join(os.path.dirname(os.path.dirname(__file__)), "cnb_meetings")
MAX_WORKERS = 4
SUMMARIZE_CHARS = 8000

SUMMARY_PROMPT = (
    "Summarise this Czech National Bank (CNB) Bank Board monetary policy statement "
    "in 2-3 sentences. Focus on the rate decision (held / raised / cut), the new "
    "two-week repo rate level, and the key economic reasoning. Be concise and factual."
)


# ── PDF discovery & parsing ───────────────────────────────────────────────────

def _pdf_to_iso(fname: str):
    """Convert 'DD-MM-YYYY.pdf' → 'YYYY-MM-DD'."""
    m = re.match(r'(\d{2})-(\d{2})-(\d{4})\.pdf$', fname)
    if not m:
        return None
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"


def _read_pdfs(cutoff: str) -> list[dict]:
    if not os.path.isdir(PDF_FOLDER):
        logger.warning("cnb_meetings/ folder not found at %s", PDF_FOLDER)
        return []

    items = []
    for fname in os.listdir(PDF_FOLDER):
        iso = _pdf_to_iso(fname)
        if not iso or iso[:7] < cutoff:
            continue
        dt = datetime.strptime(iso, "%Y-%m-%d")
        items.append({
            "path":  os.path.join(PDF_FOLDER, fname),
            "d":     iso,
            "cat":   "cb",
            "title": f"CNB Bank Board – {dt.strftime(f'%B {dt.day}, %Y')}",
        })

    return sorted(items, key=lambda l: l["d"])


# ── text extraction ───────────────────────────────────────────────────────────

def _extract_pdf_text(path: str) -> str:
    try:
        import pdfplumber
        with pdfplumber.open(path) as pdf:
            text = " ".join(p.extract_text() or "" for p in pdf.pages)
        return re.sub(r"\s+", " ", text).strip()
    except Exception as e:
        logger.error("pdfplumber failed for %s: %s", path, e)
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


def _process(item: dict, db: dict) -> tuple:
    uk = f"pdf:{item['path']}"
    if uk in db:
        return uk, db[uk], None
    try:
        text = _extract_pdf_text(item["path"])
        if not text or len(text) < 100:
            return None, None, None
        ck = summary_store.key_for(text)
        if ck in db:
            return uk, ck, None
        summary = _summarize_one(text)
        return uk, ck, summary
    except Exception as e:
        logger.error("Failed to process CNB PDF %s: %s", item["path"], e)
        return None, None, None


# ── background generation ─────────────────────────────────────────────────────

_bg_lock    = concurrent.futures.ThreadPoolExecutor(max_workers=1)
_bg_running: set = set()


def _generate_and_cache(cache_key: str, items: list) -> None:
    try:
        db = summary_store.load_all()
        missing = [i for i in items if f"pdf:{i['path']}" not in db]
        if not missing:
            return
        logger.info("Background: summarising %d CNB PDFs…", len(missing))
        new_entries = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {pool.submit(_process, it, db): it for it in missing}
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
    items  = _read_pdfs(cutoff)
    logger.info("CNB events: %d PDFs found since %s", len(items), cutoff)

    db      = summary_store.load_all()
    missing = [i for i in items if f"pdf:{i['path']}" not in db]

    if missing and cache_key not in _bg_running:
        _bg_running.add(cache_key)
        _bg_lock.submit(_generate_and_cache, cache_key, items)
        logger.info("Background CNB PDF summary job started (%d missing)", len(missing))

    events = []
    for it in items:
        uk   = f"pdf:{it['path']}"
        ck   = db.get(uk)
        body = db.get(ck, "") if ck else ""
        events.append({
            "d":     it["d"],
            "cat":   it["cat"],
            "title": it["title"],
            "body":  body or f"[Generating summary for {it['title']}…]",
        })

    events.sort(key=lambda e: e["d"])
    cache.set(cache_key, events)
    logger.info("CNB events ready: %d items from %s", len(events), cutoff)
    return events
