"""
Fetches daily yield data from the Deutsche Bundesbank statistics API.
API base: https://api.statistiken.bundesbank.de/rest/data/BBSSY/{series}

Key series for ECB chart:
  D.REN.EUR.A630.000000WT1010.A  — Daily yield of the current 10-year Bund
  D.REN.EUR.A610.000000WT0202.A  — Daily yield of the current 2-year Schatz

Data is daily with "." for weekends/holidays (skipped in output).
"""

import csv
import io
import logging
from datetime import datetime, date

import requests
import cache
from config import DEFAULT_START_DATE

logger = logging.getLogger(__name__)

BASE_URL = "https://api.statistiken.bundesbank.de/rest/data/BBSSY"
HEADERS  = {"User-Agent": "Mozilla/5.0 (compatible; cb-api/1.0)"}


def fetch(series_id: str, start_date: str = DEFAULT_START_DATE) -> list[dict]:
    """
    Fetch a single Bundesbank daily yield series.
    Returns list of {"date": "YYYY-MM-DD", "value": float}.
    Weekend/holiday entries ("." values) are skipped.
    """
    cache_key = f"bundesbank_{series_id}_{start_date[:7]}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    today  = date.today().isoformat()
    url    = f"{BASE_URL}/{series_id}"
    params = {
        "format":      "csv",
        "lang":        "en",
        "startPeriod": start_date[:10],
        "endPeriod":   today,
    }

    logger.info("Fetching Bundesbank %s", series_id)
    resp = requests.get(url, params=params, headers=HEADERS, timeout=15)
    resp.raise_for_status()

    results = []
    reader  = csv.reader(io.StringIO(resp.text))
    for row in reader:
        if len(row) < 2:
            continue
        date_str  = row[0].strip()
        value_str = row[1].strip()
        # Skip header/metadata rows (date field not YYYY-MM-DD)
        if len(date_str) != 10 or not date_str[:4].isdigit():
            continue
        # Skip missing-value markers
        if value_str in (".", "", "NaN"):
            continue
        try:
            results.append({"date": date_str, "value": float(value_str)})
        except ValueError:
            pass

    results.sort(key=lambda r: r["date"])
    logger.debug("Bundesbank %s: %d daily observations", series_id, len(results))
    cache.set(cache_key, results)
    return results
