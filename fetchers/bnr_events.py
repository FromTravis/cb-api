"""
Fetches NBR (National Bank of Romania) Board monetary policy decision events
from local PDF files in the bnr_meetings/ folder.

File naming convention: DD-MM-YYYY.pdf
Each PDF contains the full press release text; pdfplumber extracts it.
Claude Haiku summarises each press release once and persists in the store.
"""

import os
import re
import logging
import concurrent.futures
from datetime import datetime

import anthropic
import requests

import cache
import fetchers.summary_store as summary_store
from config import DEFAULT_START_DATE, ANTHROPIC_API_KEY

logger = logging.getLogger(__name__)

PDF_FOLDER  = os.path.join(os.path.dirname(os.path.dirname(__file__)), "bnr_meetings")
MAX_WORKERS = 4
SUMMARIZE_CHARS = 8000

SUMMARY_PROMPT = (
    "Summarise this National Bank of Romania (NBR) Board monetary policy press release "
    "in 2-3 sentences. Focus on the rate decision (held / raised / cut), the new rate "
    "level, and the key economic reasoning given. Be concise and factual."
)


# ── PDF discovery & parsing ───────────────────────────────────────────────────

def _pdf_to_iso(fname: str):
    """Convert 'DD-MM-YYYY.pdf' → 'YYYY-MM-DD', or None if no match."""
    m = re.match(r'(\d{2})-(\d{2})-(\d{4})\.pdf$', fname)
    if not m:
        return None
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"


def _read_pdfs(cutoff: str) -> list[dict]:
    """Scan the bnr_meetings/ folder and return sorted list of decision dicts."""
    if not os.path.isdir(PDF_FOLDER):
        logger.warning("bnr_meetings/ folder not found at %s", PDF_FOLDER)
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
            "title": f"NBR Board Decision – {dt.strftime(f'%B {dt.day}, %Y')}",
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
    # Key on the PDF path (unique per file)
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
        logger.error("Failed to process BNR PDF %s: %s", item["path"], e)
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
        logger.info("Background: summarising %d BNR PDFs…", len(missing))
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
            logger.info("Background: BNR store updated (%d total)", len(db))
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
    items  = _read_pdfs(cutoff)
    logger.info("BNR events: %d PDFs found since %s", len(items), cutoff)

    db      = summary_store.load_all()
    missing = [i for i in items if f"pdf:{i['path']}" not in db]

    if missing and cache_key not in _bg_running:
        _bg_running.add(cache_key)
        _bg_lock.submit(_generate_and_cache, cache_key, items)
        logger.info("Background BNR PDF summary job started (%d missing)", len(missing))

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
    logger.info("BNR events ready: %d items from %s", len(events), cutoff)
    return events
