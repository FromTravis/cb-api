"""
Fetches Japanese inflation expectations from Nakajima (2016) estimates.
Source: https://github.com/jouchinakajima/program/raw/main/einf.csv

Quarter column format: YYYYMM (end month of quarter, e.g. 202606 = Q2 2026)
Uses the '1Year' column for 1-year-ahead inflation expectations (%).
Frequency: quarterly.
"""

import csv
import io
import logging

import requests
import cache
from config import DEFAULT_START_DATE

logger = logging.getLogger(__name__)

CSV_URL = "https://github.com/jouchinakajima/program/raw/main/einf.csv"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; cb-api/1.0)"}


def _yyyymm_to_iso(yyyymm: str) -> str | None:
    """Convert '202606' → '2026-06-01'."""
    s = str(yyyymm).strip()
    if len(s) == 6 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-01"
    return None


def fetch(series_id: str = "1Year", start_date: str = DEFAULT_START_DATE) -> list[dict]:
    cache_key = f"nakajima_{series_id}_{start_date[:7]}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    logger.info("Fetching Nakajima inflation expectations CSV")
    resp = requests.get(CSV_URL, headers=HEADERS, timeout=15)
    resp.raise_for_status()

    # Skip the first header line ("Data as of YYYY-MM-DD,,,...") if present
    text = resp.text
    lines = text.strip().split("\n")
    # Find the actual header row (the one with 'Quarter')
    start_line = 0
    for i, line in enumerate(lines):
        if line.strip().startswith("Quarter"):
            start_line = i
            break

    reader = csv.DictReader(io.StringIO("\n".join(lines[start_line:])))

    cutoff  = start_date[:7]
    results = []

    for row in reader:
        quarter = row.get("Quarter", "").strip()
        value   = row.get(series_id, "").strip()
        if not quarter or not value:
            continue
        iso = _yyyymm_to_iso(quarter)
        if not iso or iso[:7] < cutoff:
            continue
        try:
            results.append({"date": iso, "value": round(float(value), 4)})
        except ValueError:
            pass

    results.sort(key=lambda r: r["date"])
    logger.debug("Nakajima %s: %d quarterly observations", series_id, len(results))
    cache.set(cache_key, results)
    return results
