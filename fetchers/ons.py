"""
Fetches monthly timeseries from the ONS (Office for National Statistics).
Data is already expressed as YoY % change — no transform needed.

Example: D7G7 — UK CPI All Items, 12-month rate (%)
API: https://www.ons.gov.uk/economy/inflationandpriceindices/timeseries/{code}/data
"""

import logging
from datetime import datetime

import requests
import cache
from config import DEFAULT_START_DATE

logger = logging.getLogger(__name__)

BASE_URL = "https://www.ons.gov.uk/economy/inflationandpriceindices/timeseries/{code}/data"
HEADERS  = {"User-Agent": "Mozilla/5.0 (compatible; cb-api/1.0)"}


def fetch(series_code: str, start_date: str = DEFAULT_START_DATE) -> list[dict]:
    """
    Fetch a monthly ONS inflation timeseries.
    Returns list of {"date": "YYYY-MM-01", "value": float} (value is already YoY %).
    """
    cache_key = f"ons_{series_code}_{start_date[:7]}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    url = BASE_URL.format(code=series_code.lower())
    logger.info("Fetching ONS %s", series_code)
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()

    data   = resp.json()
    cutoff = start_date[:7]   # YYYY-MM

    results = []
    for item in data.get("months", []):
        raw_date  = (item.get("date") or "").strip()   # e.g. "2021 JAN"
        raw_value = (item.get("value") or "").strip()
        if not raw_date or not raw_value:
            continue
        try:
            dt    = datetime.strptime(raw_date, "%Y %b")
            ym    = dt.strftime("%Y-%m")
            if ym < cutoff:
                continue
            value = float(raw_value)
            results.append({"date": dt.strftime("%Y-%m-01"), "value": value})
        except (ValueError, KeyError):
            continue

    results.sort(key=lambda r: r["date"])
    logger.debug("ONS %s: %d monthly observations", series_code, len(results))
    cache.set(cache_key, results)
    return results
