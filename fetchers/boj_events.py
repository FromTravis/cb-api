"""
Fetches Bank of Japan monetary policy events from boj.or.jp:

  Statements:          /en/mopo/mpmdeci/mpr_{year}/ — k{YYMMDD}a.pdf (main decision only)
  Summary of Opinions: /en/mopo/mpmsche_minu/opinion_{year}/ — opi{YYMMDD}.htm or .pdf

Date is extracted from the 6-digit code in filenames: YYMMDD → YYYY-MM-DD.
Text is extracted from PDFs (pypdf) or HTML pages (BeautifulSoup).
Summaries are generated with Claude Haiku and persisted in the shared store.
"""

import io
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

BASE_URL    = "https://www.boj.or.jp"
HEADERS     = {"User-Agent": "Mozilla/5.0 (compatible; cb-api/1.0)"}
MAX_WORKERS = 6
SUMMARIZE_CHARS = 8000

SUMMARY_PROMPTS = {
    "cb":     ("Summarise this Bank of Japan monetary policy statement in 2-3 sentences. "
               "Focus on the rate decision, vote split, and key policy guidance. Be concise and factual."),
    "speech": ("Summarise this Bank of Japan Summary of Opinions in 2-3 sentences. "
               "Focus on the key views expressed on the economy and future policy direction. Be concise."),
}


# ── date helpers ──────────────────────────────────────────────────────────────

def _yymmdd_to_iso(code: str) -> str | None:
    """Convert '260616' → '2026-06-16'."""
    try:
        dt = datetime.strptime(code, "%y%m%d")
        return dt.strftime("%Y-%m-%d")
    except ValueError:
        return None


# ── link discovery ────────────────────────────────────────────────────────────

def _fetch_page_links(url: str) -> str:
    r = requests.get(url, headers=HEADERS, timeout=10)
    if r.status_code != 200:
        return ""
    return r.text


def _discover_links(cutoff: str) -> list[dict]:
    """
    Scan BoJ year index pages for opinion files, then construct matching
    statement URLs (k{YYMMDD}a.pdf) — the statements exist for all years
    but are only listed on the main page for recent meetings.
    Returns sorted list of {url, d, cat, title}.
    """
    start_year = int(cutoff[:4])
    current    = date.today().year
    links      = []
    seen_codes = set()

    # ── Main page: captures most recent statement links ───────────────────────
    html_main = _fetch_page_links(f"{BASE_URL}/en/mopo/mpmsche_minu/index.htm")

    for year in range(start_year, current + 1):
        # ── Opinions: opi{YYMMDD}.htm or .pdf ────────────────────────────────
        html_op = _fetch_page_links(
            f"{BASE_URL}/en/mopo/mpmsche_minu/opinion_{year}/index.htm"
        )
        for m in re.finditer(
            rf'/en/mopo/mpmsche_minu/opinion_{year}/(opi(\d{{6}})\.(htm|pdf))',
            html_op, re.I
        ):
            path, code, ext = m.group(1), m.group(2), m.group(3).lower()
            iso = _yymmdd_to_iso(code)
            if not iso or iso[:7] < cutoff or code in seen_codes:
                continue
            seen_codes.add(code)

            dt_str = datetime.strptime(iso, "%Y-%m-%d").strftime("%B %-d, %Y")

            # ── Statement: k{YYMMDD}a.pdf (exists but not always linked) ─────
            stmt_url = f"{BASE_URL}/en/mopo/mpmdeci/mpr_{year}/k{code}a.pdf"
            try:
                rh = requests.head(stmt_url, headers=HEADERS, timeout=5,
                                   allow_redirects=True)
                if rh.status_code == 200:
                    links.append({
                        "url":   stmt_url,
                        "d":     iso,
                        "cat":   "cb",
                        "title": f"BoJ Statement – {dt_str}",
                        "kind":  "pdf",
                    })
            except requests.RequestException:
                pass

            # ── Opinion ───────────────────────────────────────────────────────
            links.append({
                "url":   BASE_URL + f"/en/mopo/mpmsche_minu/opinion_{year}/" + path,
                "d":     iso,
                "cat":   "speech",
                "title": f"BoJ Summary of Opinions – {dt_str}",
                "kind":  ext,
            })

    return sorted(links, key=lambda l: (l["d"], l["cat"]))


# ── content extraction ────────────────────────────────────────────────────────

def _extract_pdf_text(content: bytes) -> str:
    try:
        import pypdf
        reader = pypdf.PdfReader(io.BytesIO(content))
        text = " ".join(p.extract_text() or "" for p in reader.pages[:6])
        return re.sub(r'\s+', ' ', text).strip()
    except Exception as e:
        logger.warning("PDF extraction failed: %s", e)
        return ""


def _extract_htm_text(html: str) -> str:
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, 'html.parser')
    for el in soup.select('header, nav, footer, script, style'):
        el.decompose()
    text = soup.get_text(' ', strip=True)
    text = re.sub(r'\s+', ' ', text)
    # Trim leading boilerplate — find where actual content starts
    for anchor in ['Summary of Opinions', 'Opinions on', 'I.', 'At the Monetary Policy']:
        idx = text.find(anchor)
        if idx > 0:
            return text[idx:].strip()
    return text.strip()


def _fetch_text(link: dict) -> str:
    r = requests.get(link["url"], headers=HEADERS, timeout=15)
    r.raise_for_status()
    if link["kind"] == "pdf":
        return _extract_pdf_text(r.content)
    return _extract_htm_text(r.text)


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


def _process(link: dict, db: dict) -> tuple:
    uk = f"url:{link['url']}"
    if uk in db:
        return uk, db[uk], None
    try:
        text    = _fetch_text(link)
        ck      = summary_store.key_for(text)
        if ck in db:
            return uk, ck, None
        summary = _summarize_one(text, link["cat"])
        return uk, ck, summary
    except Exception as e:
        logger.error("Failed to process BoJ event %s: %s", link["url"], e)
        return None, None, None


# ── main entry point ──────────────────────────────────────────────────────────

def fetch(start_date: str = DEFAULT_START_DATE, force: bool = False) -> list[dict]:
    cache_key = f"boj_events_{start_date[:7]}"
    if not force:
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

    cutoff = start_date[:7]
    logger.info("Discovering BoJ events since %s", cutoff)
    links = _discover_links(cutoff)
    logger.info("Found %d BoJ events (%d statements, %d opinions)",
                len(links),
                sum(1 for l in links if l["cat"] == "cb"),
                sum(1 for l in links if l["cat"] == "speech"))

    db      = summary_store.load_all()
    missing = [l for l in links if f"url:{l['url']}" not in db]

    if missing:
        logger.info("Summarising %d new BoJ events with Claude Haiku…", len(missing))
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
            logger.info("Summary store updated (%d total entries)", len(db))

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

    events.sort(key=lambda e: (e["d"], e["cat"]))
    cache.set(cache_key, events)
    logger.info("BoJ events ready: %d items from %s", len(events), cutoff)
    return events
